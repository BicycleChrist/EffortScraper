"""
EffortMLB.py — standalone MLB viewer + central player statistics/data hub
(formerly PropWindowUtils.py). Merges the former mlb_prop_stats.py (data
layer) and prop_player_detail.py (Player Detail panel), plus the matchup
context layer.

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
import math
import os
import time
import unicodedata
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp

# Decoding responses is pure loop-thread time on qasync — and json.loads
# holds the GIL for the whole call, so it can't be pushed to an executor;
# it can only be made cheaper. orjson is ~2x on these payloads and returns
# identical objects. Optional: fall back to the stdlib when it isn't there.
try:
    import orjson as _orjson
    json_loads = _orjson.loads
except ImportError:
    json_loads = json.loads

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
        print(f"EffortMLB: dev cache write failed: {e}")


# --- slate cache: ON BY DEFAULT, unlike the dev cache above.
#
# Almost nothing this app downloads changes during a day. A season of
# play-by-play, the Savant season boards, the umpire assignments — all of it
# is settled by the time the slate is posted. What genuinely moves is the
# LINEUPS and the occasional scratched starter, and those come from one
# schedule call with a 15-minute TTL.
#
# The manager board alone walks ~110 games x 30 clubs of play-by-play on
# every cold start, which is the wait on launch. Keying the cache on the DATE
# means today's entries are reused all day and tomorrow's directory is simply
# a different one — no TTL logic, no staleness, and yesterday's cache never
# masquerades as today's.
SLATE_CACHE_DIR = SAVE_DIR / "slate"
SLATE_CACHE = os.environ.get("EFFORTMLB_NO_SLATE_CACHE") != "1"


def _slate_dir() -> Path:
    return SLATE_CACHE_DIR / datetime.now().strftime("%Y-%m-%d")


def slate_cache_get(key: str) -> Optional[str]:
    if not SLATE_CACHE:
        return None
    p = _slate_dir() / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    try:
        return p.read_text() if p.exists() else None
    except OSError:
        return None


def slate_cache_put(key: str, text: str):
    if not SLATE_CACHE or not text:
        return
    try:
        d = _slate_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / (hashlib.sha1(key.encode()).hexdigest() + ".json")).write_text(text)
    except OSError as e:
        print(f"EffortMLB: slate cache write failed: {e}")


def prune_slate_cache(keep_days: int = 3):
    """Drop slate directories older than `keep_days`. Cheap insurance against
    a season of play-by-play aggregates accumulating on disk."""
    if not SLATE_CACHE_DIR.exists():
        return
    keep = {(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(keep_days)}
    for d in SLATE_CACHE_DIR.iterdir():
        if d.is_dir() and d.name not in keep:
            try:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
            except OSError:
                pass


# --- season play-by-play checkpoint: the ONE cache in this file that is
# deliberately NOT keyed on the date.
#
# The manager board's inputs are derived from every FINAL game of the season,
# and a final game's play-by-play never changes again. Keying its aggregate
# on the slate date therefore re-downloaded the entire season (~1,650 games,
# ~0.115MB each even trimmed) every morning to learn about the ~15 games that
# were played overnight. This file stores the DERIVED accumulators — per-club
# stints/decisions/run-value events/ABS plus the league RE24, win-expectancy,
# TTO and umpire tables — alongside the set of gamePks they were built from,
# so a later run folds in only the gamePks it has never seen.
#
# !! COUPLING !!  The league tables and `games` MUST travel together: the
# tables are sums over exactly those gamePks, so loading tables from one
# source and the game set from another would either double-count games or
# drop them. Bump PBP_CK_VERSION in the SAME edit as any change to what the
# derivation functions produce, or an old file deserialises into the new
# shape and the symptom is silently wrong aggregates, never an error.
#
# EFFORTMLB_NO_SLATE_CACHE=1 turns this off too, which means a full ~1,650
# game walk on every launch — that is the point of the flag, but know what
# you are asking for.
PBP_CK_VERSION = 1


def _pbp_ck_path(season) -> Path:
    return SAVE_DIR / "pbp" / f"season_{season}_v{PBP_CK_VERSION}.json"


def _encode_tuple_keyed(d: dict) -> list:
    """JSON cannot key on tuples, and every league table here does
    (state -> value). Store as [[key_parts...], value] pairs."""
    return [[list(k) if isinstance(k, tuple) else k, v] for k, v in d.items()]


def _decode_tuple_keyed(rows) -> dict:
    return {(tuple(k) if isinstance(k, list) else k): v for k, v in rows}
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
# Fielding OAA with the directional and batter-hand splits — the axes an SP's
# contact profile actually lives on.
# Savant labels its fielding boards by team NICKNAME, and one of them is not a
# suffix of the club's full name — "Arizona Diamondbacks" does not end with
# "D-backs". Same failure mode as FG_TEAM_ALIAS: no error, the club simply
# vanishes from the join. ("---" is the no-team row for traded players and
# correctly matches nothing.)
SAVANT_NICK_ALIAS = {"D-backs": "AZ"}
SAVANT_OAA_URL = (
    "https://baseballsavant.mlb.com/leaderboard/outs_above_average"
    "?type=Fielder&year={year}&csv=true"
)
# Fielding run value — the ONLY source for the two skills OAA cannot see at
# all: outfield/infield ARM (holding the runner) and double plays. OAA is
# catch probability and nothing else, which is why it rates Corbin Carroll
# (+7.7 range, -3.5 arm) as a plain good outfielder. No CSV endpoint: the
# numbers live in a `const data = [...]` literal in the page source.
SAVANT_FRV_URL = "https://baseballsavant.mlb.com/leaderboard/fielding-run-value"
# Runs -> outs, to put arm/DP on the same scale as OAA so they can ride the
# same contact weighting. Roughly 0.8 outs per run; an approximation, and the
# arm/DP terms are small (sd 1.27 and 0.91 runs vs range's 4.25) so the
# conclusion does not hinge on it.
FRV_RUNS_TO_OUTS = 0.8
# FanGraphs positional adjustment, runs per 150 games — the defensive
# spectrum. It exists because the same glove prevents different amounts at
# different positions: the shortstop pool is far stronger than the first-base
# pool, so a competent middle infielder moved across the diamond should be
# expected ABOVE first-base average, not at it. Used to price a fielder
# starting somewhere he has no track record (Nick Sogard, a utility infielder,
# made his first start at 1B and appears on NO Statcast fielding board).
POSITION_ADJ = {"C": 12.5, "SS": 7.5, "2B": 2.5, "3B": 2.5, "CF": 2.5,
                "LF": -7.5, "RF": -7.5, "1B": -12.5, "DH": -17.5}


def surname(name: str) -> str:
    """Savant's fielding boards give "Last, First"; the StatsAPI lineup feed
    gives "First Last". Splitting on the comma alone turned Nick Sogard into
    "Nick So" once an estimated fielder entered the same list."""
    if not name:
        return ""
    if "," in name:
        return name.split(",")[0].strip()
    parts = name.split()
    return parts[-1] if parts else ""


def estimate_fielder(fg_def: Optional[float], fg_pos: Optional[float],
                     games: Optional[float], from_pos: str,
                     to_pos: str) -> Optional[float]:
    """OAA-scale estimate for a glove with no Statcast fielding row.

    `Defense - Pos` strips FanGraphs' positional adjustment back off, leaving
    position-RELATIVE fielding runs; the spectrum shift then re-prices that
    skill against tonight's position's pool. Deliberately crude — there are no
    directional splits behind it, so it is flagged as an estimate everywhere
    it is shown and never mistaken for a measured OAA."""
    if fg_def is None:
        return None
    fld = fg_def - (fg_pos or 0.0)
    shift = (POSITION_ADJ.get(from_pos, 0.0)
             - POSITION_ADJ.get(to_pos, 0.0))
    bonus = shift * (games or 0.0) / 150.0
    # The spectrum adjustment is a full-season VALUE conversion, and applied
    # at face value it flatters anyone moved down the defensive ladder — a -5
    # fielding left fielder came out POSITIVE at first base. Half-weight it
    # and clamp: OAA at 1B has a compressed spread (few hard plays), so a
    # large estimated number there would be a modelling artefact, not a glove.
    est = (fld + 0.5 * bonus) * FRV_RUNS_TO_OUTS
    return max(-4.0, min(4.0, est))
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
# Batter swing-path / attack-angle / intercept / box-position board — feeds
# the combo flight-viewer's batter-in-the-box overlay (keyed by MLBAM id).
SAVANT_SWING_PATH_URL = (
    "https://baseballsavant.mlb.com/leaderboard/bat-tracking/"
    "swing-path-attack-angle?type=batter&year={year}&csv=true"
)
# Catcher receiving boards — framing (strike-rate run value) and blocking
# (runs saved on pitches in the dirt). Keyed by MLBAM id; feed the extra
# stat cells the lineup card shows for catchers.
SAVANT_CATCHER_FRAMING_URL = (
    "https://baseballsavant.mlb.com/leaderboard/catcher-framing"
    "?year={year}&team=&min=0&csv=true"
)
SAVANT_CATCHER_BLOCKING_URL = (
    "https://baseballsavant.mlb.com/leaderboard/catcher-blocking"
    "?year={year}&team=&min=0&csv=true"
)
# The third receiving skill: controlling the running game. Carries pop time,
# exchange, arm strength and caught-stealing above average.
SAVANT_CATCHER_THROWING_URL = (
    "https://baseballsavant.mlb.com/leaderboard/catcher-throwing"
    "?year={year}&team=&min=0&csv=true"
)
# The PITCHER's side of the running game — the half the catcher boards
# cannot see. A catcher's caught-stealing numbers are hostage to how well the
# man in front of him holds runners: `rate_sbx` is attempts per opportunity,
# and the lead distances say whether runners feel free to walk off the bag.
SAVANT_PITCHER_RUNGAME_URL = (
    "https://baseballsavant.mlb.com/leaderboard/pitcher-running-game"
    "?year={year}&team=&min=0&csv=true"
)
# ...and the RUNNER's side, so tonight's threat is priced off the men who
# will actually be on base rather than off a league constant.
SAVANT_BASESTEALING_URL = (
    "https://baseballsavant.mlb.com/leaderboard/basestealing-run-value"
    "?year={year}&team=&min=0&csv=true"
)
# How a HITTER decides. Swing/take run value split by zone: Heart (the part
# of the plate he must not miss), Shadow (the edges, where the at-bat is
# actually decided), Chase, Waste. This is the other half of the Chase%
# percentile already on the SP card — that bar says how often he gets men to
# chase, this says which men are gettable. Batter-only: the board ignores
# every pitcher-perspective parameter and returns hitters regardless.
SAVANT_SWING_TAKE_URL = (
    "https://baseballsavant.mlb.com/leaderboard/swing-take"
    "?year={year}&team=&min=0&csv=true"
)
# ...and what happens when he does swing: ground/air split crossed with
# pull/straight/oppo. `pull_air_rate` is the one that matters for damage —
# pulled air is where home runs live.
SAVANT_BATTED_BALL_URL = (
    "https://baseballsavant.mlb.com/leaderboard/batted-ball"
    "?year={year}&team=&min=0&csv=true"
)
# Throwing arm by position, and the outfield jump that OAA only summarises:
# reaction, burst and route, each as feet vs league.
SAVANT_ARM_STRENGTH_URL = (
    "https://baseballsavant.mlb.com/leaderboard/arm-strength"
    "?year={year}&team=&min=0&csv=true"
)
SAVANT_OF_JUMP_URL = (
    "https://baseballsavant.mlb.com/leaderboard/outfield_jump"
    "?year={year}&team=&min=0&csv=true"
)
# Batting-stance board adds foot separation + open/closed stance angle. The
# league CSV endpoint isn't reachable non-interactively yet, so we read a
# locally-downloaded export for now (TODO: automate the fetch).
BATTING_STANCE_CSV = Path(__file__).resolve().parent / "batting-stance.csv"

# FanGraphs internal leaders API. Cloudflare 403s every non-browser client
# from this network (requests, curl_cffi impersonation, even their HTML), but
# a real headless Firefox passes untouched — so Selenium is used purely as
# transport: load robots.txt for a same-origin context, run fetch() in-page,
# return the JSON. One call returns the FULL stat row per player (Stuff+,
# per-pitch sp_s_XX, FIP/xFIP/SIERA, gmLI, ...) keyed by xMLBAMID.
# FanGraphs PROJECTIONS. Unlike /api/leaders above, this endpoint answers a
# plain request with 200 from this network — the Cloudflare wall is specific
# to the leaders path, so no Selenium transport is needed here. Rows carry
# `xMLBAMID`, which is the key everything else in this file already joins on.
#
# The `r` prefix means REST OF SEASON, which is the only tense that matters
# for tonight: a full-season line is half history we already show elsewhere.
FG_PROJ_URL = ("https://www.fangraphs.com/api/projections"
               "?type={system}&stats={stats}&pos=all&team=0&players=0&lg=all")
FG_PROJ_SYSTEMS = (
    ("Depth Charts (ROS)", "rfangraphsdc"),   # consensus, playing-time aware
    ("ZiPS (ROS)", "rzips"),
    ("Steamer (ROS)", "steamerr"),
    ("THE BAT X", "thebatx"),                 # Statcast-driven, hitters
    ("Depth Charts (full yr)", "fangraphsdc"),
)
FG_API_PATH = ("/api/leaders/major-league/data?age=&pos=all&stats={stats}"
               "&lg=all&qual={qual}&season={season}&season1={season}"
               "&ind=0&type=8&month=0&pageitems={pageitems}&pagenum=1")
FG_CACHE_TTL = 6 * 3600

# ---------------------------------------------------------------------------
# FanGraphs SPLITS leaderboard.
#
# Third FanGraphs endpoint, third Cloudflare posture — do not generalise from
# the other two. `/api/leaders` needs the headless-Firefox transport;
# `/api/projections` answers a plain request but 403s a Chrome UA; this one
# answers a plain POST with 200 either way. Plain `requests`, no UA games.
#
# One POST returns the WHOLE LEAGUE for one split (~230KB, ~600 hitters), so a
# split costs exactly one request no matter how many players are on the slate.
#
# Rows key on FanGraphs' `playerid`, NOT `xMLBAMID` — the leaders board carries
# both, which is the only reason this joins to anything. See `_fg_id_map`.
FG_SPLITS_URL = "https://www.fangraphs.com/api/leaders/splits/splits-leaders"

# Stat sets. The API 500s on any strType above 3.
FG_SPLIT_TYPE_STANDARD = "1"   # G PA AB H 1B 2B 3B HR R RBI BB IBB SO HBP SB..
FG_SPLIT_TYPE_ADVANCED = "2"   # PA BB% K% BB/K AVG OBP SLG OPS ISO BABIP
#                                wRC wRAA wOBA wRC+
FG_SPLIT_TYPE_BATTED = "3"     # PA GB/FB LD% GB% FB% IFFB% HR/FB IFH% BUH%
#                                Pull% Cent% Oppo% Soft% Med% Hard%

# Split ids. FANGRAPHS DOES NOT SERVE AN ID->LABEL MAP — it is rendered
# client-side and appears in no JS chunk, and the legacy leaderboard that used
# to carry it in a <select> is behind Cloudflare. So this table was derived
# EMPIRICALLY and every entry in it is evidenced:
#
#  * per-player PA was pulled for all 88 ids and matched against MLB StatsAPI
#    `statSplits`, which is authoritative AND labelled. Everything below
#    matched its StatsAPI counterpart at r >= 0.996 with a median PA ratio of
#    1.00 (most at r = 1.000, PA equal on every player tested);
#  * every group here then sums to EXACTLY 1.000 of season PA — that is the
#    check that the group is complete and that no id has been mixed up with a
#    neighbour. `verify.py` in the research scratchpad reproduces it.
#
# Ids NOT listed here exist (1..88 all return data) but could not be pinned to
# a labelled counterpart with confidence. DO NOT add one by guessing from its
# league PA share: shares alone are ambiguous — 3/4, 5/6 and 9/10 are three
# different pairs that all split 54/46 or 51/49.
FG_SPLIT_IDS = {
    # handedness — sums to 1.000
    "vs LHP": 1, "vs RHP": 2,
    # venue — sums to 1.000
    "Home": 7, "Away": 8,
    # outs at the start of the PA — sums to 1.000
    "0 out": 54, "1 out": 55, "2 out": 56,
    # base state. "Empty"+"Runners on" sums to 1.000; RISP and Loaded are
    # SUBSETS of "Runners on" and deliberately overlap it.
    "Empty": 57, "Runners on": 58, "RISP": 59, "Loaded": 60,
    # count. Behind/Ahead/Even sums to 1.000; Full count overlaps them.
    "Behind": 62, "Ahead": 63, "Even": 64, "Full count": 71,
    # lineup slot — sums to 1.000
    "Bat 1st": 19, "Bat 2nd": 20, "Bat 3rd": 21, "Bat 4th": 22, "Bat 5th": 23,
    "Bat 6th": 24, "Bat 7th": 25, "Bat 8th": 26, "Bat 9th": 27,
    # inning — sums to 1.000
    "Inn 1": 44, "Inn 2": 45, "Inn 3": 46, "Inn 4": 47, "Inn 5": 48,
    "Inn 6": 49, "Inn 7": 50, "Inn 8": 51, "Inn 9": 52, "Extras": 53,
    # month — sums to 1.000. Id 84 is MARCH+APRIL, not April: it matched
    # StatsAPI's April at r=0.965 but with a PA ratio of 1.20, and the excess
    # is exactly the March games.
    "Mar/Apr": 84, "May": 85, "Jun": 86, "Jul": 87, "Aug": 88,
}

# How long a splits pull stays warm. Splits move once a day at most, and the
# slate cache keys on the DATE, so this only governs an in-session refetch.
FG_SPLITS_TTL = 6 * 3600

# Seconds between batches of split requests. Cloudflare rate-limits the whole
# `/api/*` surface per IP, and a burst here 403s the LEADERS board as well —
# which is far more expensive to lose, since that one needs the headless
# Firefox transport. Only paid on the first pull of the day.
FG_SPLITS_CHUNK_PAUSE = 1.5

# FanGraphs team abbreviations differ from StatsAPI's for seven clubs, which
# silently drops them from any join keyed on team (it cost the manager board
# 7 of 30 pen-quality rows). "2 Tms"/"3 Tms" are FG's combined lines for
# traded players and match no real club by design.
FG_TEAM_ALIAS = {"ARI": "AZ", "CHW": "CWS", "KCR": "KC", "SDP": "SD",
                 "SFG": "SF", "TBR": "TB", "WSN": "WSH"}

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
            print("EffortMLB: insidethepen login OK")
        else:
            print("EffortMLB: insidethepen login FAILED (check creds)")
        return ok
    except Exception as e:
        print(f"EffortMLB: insidethepen login error: {e}")
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
                    print("EffortMLB: insidethepen traits are gated — "
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
        print(f"EffortMLB: reliever page fetch failed ({pid}): {e}")
    return out


def fetch_fg_leaders_sync(stats: str = "pit", qual: str = "1",
                          season: Optional[int] = None,
                          pageitems: int = 3000) -> List[dict]:
    """Fetch a FanGraphs leaders board via headless Firefox (see note above).
    Disk-cached under savedata/ with a 6h TTL. Blocking — call from an
    executor.

    ON FAILURE, FALLS BACK TO THE STALE CACHE rather than returning [].
    This board is the spine of every value stat in the window — the lineup
    rail's wRC+/Def/WPA/BsR, the opponent strip, the SP card's Stuff+ — and
    dropping it turns all of them into dashes at once. A board a few hours
    past its TTL is worth essentially the same as a fresh one (season rates
    do not move in a day), so an expired cache always beats nothing.
    Returns [] only when the fetch fails AND there is no cache at all."""
    season = season or datetime.now().year
    cache = SAVE_DIR / f"fg_{stats}_{season}.json"
    # Read the cache regardless of age; the age only decides whether we try
    # to REFRESH it, never whether it is usable as a fallback below.
    stale: Optional[List[dict]] = None
    if cache.exists():
        try:
            stale = json.loads(cache.read_text())
        except Exception:
            stale = None
        age = time.time() - cache.stat().st_mtime
        # Dev-cache runs never expire the board — no headless FF mid-layout-test
        if stale and (DEV_CACHE or age < FG_CACHE_TTL):
            return stale
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
            # A bare json.loads here reported "Expecting value: line 1
            # column 1" when FanGraphs answered with an HTML challenge page,
            # which says nothing about the actual cause. Show what came back.
            try:
                rows = json.loads(out).get("data", [])
            except ValueError:
                head = str(out)[:200].replace("\n", " ")
                print(f"EffortMLB: FG '{stats}' returned non-JSON "
                      f"({len(str(out))} chars): {head}")
                rows = []
            if rows:
                try:
                    cache.write_text(json.dumps(rows))
                except Exception:
                    pass
        else:
            print(f"EffortMLB: FG api fetch failed: {str(out)[:120]}")
    except Exception as e:
        print(f"EffortMLB: FG selenium fetch failed: {e}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    if not rows and stale:
        hrs = (time.time() - cache.stat().st_mtime) / 3600
        print(f"EffortMLB: FG '{stats}' refresh failed — using the cached "
              f"board ({len(stale)} rows, {hrs:.1f}h old)")
        return stale
    return rows


def fetch_fg_split_sync(split_id: int, stat_type: str = FG_SPLIT_TYPE_ADVANCED,
                        position: str = "B",
                        season: Optional[int] = None) -> Dict[int, dict]:
    """One FanGraphs split for the WHOLE LEAGUE, keyed by FanGraphs playerid.

    Plain `requests` — this endpoint has no Cloudflare wall (see the note on
    FG_SPLITS_URL; the other two FG endpoints in this file each behave
    differently and none of the three generalises to the others).

    Blocking — call from an executor. Returns {} on any failure, which the
    callers treat as "this split is unavailable" rather than "zero PA".
    """
    season = season or datetime.now().year
    ck = f"fgsplit_{position}_{stat_type}_{split_id}_{season}"
    raw = slate_cache_get(ck)
    if raw is None:
        body = {
            "strPlayerId": "all", "strSplitArr": [split_id],
            "strGroup": "season", "strPosition": position,
            "strType": stat_type,
            "strStartDate": f"{season}-03-01", "strEndDate": f"{season}-11-01",
            "strSplitTeams": False, "dctFilters": [],
            "strStatType": "player", "strAutoPt": "false",
            "arrPlayerId": [], "strSplitArrPitch": [],
            "arrWxTemperature": None, "arrWxPressure": None,
            "arrWxAirDensity": None, "arrWxElevation": None,
            "arrWxWindSpeed": None,
        }
        try:
            import requests
            resp = requests.post(FG_SPLITS_URL, json=body, timeout=40)
            if resp.status_code != 200:
                print(f"EffortMLB: FG split {split_id} HTTP {resp.status_code}")
                return {}
            raw = resp.text
            slate_cache_put(ck, raw)
        except Exception as e:
            print(f"EffortMLB: FG split {split_id} fetch failed: {e}")
            return {}
    # The payload is column-oriented ({"k": [names], "v": [[row], ...]}), not
    # a list of dicts like every other FanGraphs endpoint in this file.
    try:
        data = json.loads(raw)
        cols = data["k"]
        i_id = cols.index("playerid")
    except Exception as e:
        print(f"EffortMLB: FG split {split_id} parse failed: {e}")
        return {}
    out: Dict[int, dict] = {}
    for row in data.get("v", []):
        rec = dict(zip(cols, row))
        try:
            out[int(row[i_id])] = rec
        except (IndexError, TypeError, ValueError):
            continue
    return out


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
# League-average BABIP allowed (recent MLB baseline) — reference for the
# pitcher BABIP delta shown in the season line. Pitcher below league = lucky.
LG_BABIP = 0.291

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
# Everything a pitch can be that is NOT a strike. Defined by exclusion on
# purpose: the strike side has a long tail (foul_tip, bunt_foul_tip,
# automatic_strike, hit_into_play...) and a description that goes unlisted
# should default to "strike", which is the far larger class, rather than
# silently deflating the rate the way an inclusion list would.
_NON_STRIKE_DESCS = {"ball", "blocked_ball", "automatic_ball", "hit_by_pitch",
                     "pitchout", "intent_ball"}

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


# League 75th-percentile exit velocity as a function of OPPO-SIGNED spray
# angle (negative = pull, positive = opposite field, both hands), in 5°
# bins. Measured here over 170,456 batted balls, 2024+2025 pooled.
#
# The point: you cannot hit the ball as hard the other way. p75 EV runs
# 101.9 mph pulled down to 94.4 at +42°, so a flat 95 mph "hard hit" cutoff
# charges every hitter the same ~7 mph handicap for going oppo, and rewards
# a pull-only approach for nothing but its direction.
#
# Safe to embed: the 2024 and 2025 curves correlate r=0.9965, mean absolute
# shift 0.50 mph, max 1.05. It drifts up ~+0.5 mph a season with the ball,
# so refresh it every couple of years — the SHAPE is what matters and that
# is essentially fixed.
SPRAY_EV_P75 = {
    -42.5: 101.20, -37.5: 100.90, -32.5: 101.35, -27.5: 101.65,
    -22.5: 101.85, -17.5: 101.50, -12.5: 101.60, -7.5: 101.90,
    -2.5: 101.78, 2.5: 101.30, 7.5: 100.70, 12.5: 100.35,
    17.5: 99.50, 22.5: 99.20, 27.5: 98.25, 32.5: 97.05,
    37.5: 95.80, 42.5: 94.35,
}
_SPRAY_BINS = sorted(SPRAY_EV_P75)


# ---------------------------------------------------------------------------
# BMIELKE — Bat-Motion Index: Estimate from Leading Kinetic Evidence
# ---------------------------------------------------------------------------
# A read on CONTACT QUALITY, built from how a hitter SWINGS rather than from
# what his batted balls have happened to do yet.
#
# RENAMED at v9. It was "Barrel Metric: Indexed Evaluation from Low-sample
# Kinetic Evidence"; the letters are unchanged and three words were wrong:
#   * BAT-MOTION, not Barrel. There is no barrel term in the feature set and
#     never was — naming a metric after an ingredient it does not contain is
#     the exact criticism this project already avoided with spray. What the
#     model actually reads is bat motion: fast-swing rate, attack angle and
#     contact depth are three of its six terms.
#   * LEADING, not Low-sample — and this is the thesis in one word. Swing
#     evidence LEADS batted-ball evidence, which is the entire reason the
#     metric exists. "Low-sample" also undersold it: against plain xwOBAcon
#     it wins SIGNIFICANTLY through 120 batted balls (roughly April to early
#     June for a regular) and merely TIES above that. It is never
#     significantly worse at any sample size, so it is not a thin-sample
#     fallback — it is the better number first and an equal one later.
#   * ESTIMATE, not Evaluation. It estimates a latent quantity; it does not
#     grade an observed one. Refitting against short horizons (the next 5 to
#     100 balls in play) buys 0.000 to -0.010, i.e. the right thing to
#     estimate is the same at every horizon and only the noise on top changes.
# KINETIC survived and is better earned than before.
#
# WHY IT WORKS: swing evidence saturates almost immediately and then caps.
# Swing-only features predict next-season xwOBAcon at r=+0.518 on 80 swings
# and +0.513 on a full season — flat. Batted-ball evidence eventually reaches
# r≈0.80 but needs 200+ BBE to get there. Below the crossover the swing wins.
#
# The outcome term is INCLUDED, shrunk by its own sample size:
#     wshrunk = league + (xwOBAcon - league) * n/(n+150)
# That is what made it beat xwOBAcon at EVERY sample size rather than only
# below a crossover. BMIELKE subsumes the incumbent instead of competing
# with it: at 30 BBE the outcome term is 17% weighted and the swing terms
# carry the estimate; by 250 BBE it is 63% weighted and they refine it.
#
# VALIDATED. Frozen model trained on 2024->2025 (at 30 BBE) and applied
# UNCHANGED to 2025->2026; 20 independent resamples, 95% CI on the gain:
#      N BBE   xwOBAcon   BMIELKE      gain          95% CI
#         30     +0.490    +0.718     +0.228   [+0.153,+0.281]
#         50     +0.557    +0.733     +0.175   [+0.134,+0.222]
#         80     +0.640    +0.753     +0.114   [+0.091,+0.144]
#        150     +0.704    +0.766     +0.062   [+0.038,+0.087]
#        250     +0.753    +0.783     +0.030   [+0.015,+0.044]
# Significant at every sample size, so there is NO cap — earlier versions
# without the shrunk outcome term lost to xwOBAcon above ~175 BBE.
#
# Feature selection was done by cross-validation on the TRAINING pair only,
# then evaluated frozen on a season pair the model never saw. Ridge beat
# gradient boosting and random forest at low N. `dhh`/`evoe` (the
# spray-adjusted terms) were offered to the selector and NOT chosen — the
# spray adjustment is inert here, which is why the name says nothing about
# it. Training the model at each N separately gained <=0.008 and was
# dropped as pointless complexity.
#
# NOT VERIFIED for genuinely thin CAREERS (rookies/call-ups) as opposed to
# thin windows: that test had n=27 and a 95% CI of [-0.102, +0.400], too wide
# to confirm or refute. The component features behave the same in both
# populations, so it is plausible — it is simply not evidence.
#
# It measures CONTACT QUALITY, not hitter value: whiff carries a positive
# weight because swing-and-miss predicts harder contact when they do connect.
# Arraez grades near the bottom, which is right for contact quality and would
# be badly wrong as a verdict on the hitter.
BMIELKE_FEATS = ("fastsw", "evmax", "whiff", "wshrunk", "aa", "depth")
# v9 — three changes, each measured on a season the model never saw.
#
# 1. THE PRIOR IS THE PLAYER, NOT THE LEAGUE. v8 pulled every hitter's
#    xwOBAcon toward .3807, so a man with 450 batted balls last year and a
#    debutant got the same prior. Marcel has regressed to a player-specific
#    prior since 2004 and this now does too:
#        prior   = LG    + (xwOBAcon_prev - LG)    * n_prev/(n_prev+K)
#        wshrunk = prior + (xwOBAcon_now  - prior) * n_now /(n_now +K)
#    This is the bulk of the v9 gain and it lands exactly where it should —
#    largest when the current sample is thinnest, zero once it is thick.
#
# 2. NO MORE RUNGS. v8 carried five fitted models keyed by batted-ball count,
#    which meant four discontinuities: a hitter at 45 batted balls and the
#    same hitter at 46 were scored by different models and the index could
#    jump without a swing being taken. The sample-size dependence is now
#    fitted directly, as an interaction with the same shrinkage weight:
#        y ~ sum_j [ a_j*x_j + b_j*x_j*(w - 0.35) ],   w = n/(n+K)
#    Each coefficient moves smoothly from "trust the swing" to "trust the
#    outcomes". It also beat the five-rung ladder on training CV at EVERY
#    sample size (mean +0.810 -> +0.814), so continuity was free.
#
# 3. TWO NEW FEATURES, from Savant's swing-path family: mean attack angle
#    (`aa`) and mean contact depth (`depth`, the bat-ball intercept relative
#    to the batter). Both were chosen by forward selection on the TRAINING
#    season alone. `depth` takes a NEGATIVE weight — meeting the ball further
#    out front is better, which is what every hitting coach has always said.
#    `sweet` was DROPPED: with `aa` in the model it contributed +0.0009, below
#    the +0.004 bar used to admit anything, and it flipped sign.
#
# WHAT DID NOT SURVIVE (do not re-propose without new evidence):
# * SQUARED-UP RATE and BLAST RATE. Blast has the strongest marginal
#   correlation of any new candidate (+0.613 at 80 batted balls, YoY
#   reliability +0.887) and still fails multivariately (-0.0018) — it is
#   collinear with fast-swing rate and peak EV, which are already in.
#   Squared-up rate alone is NEGATIVELY related to next-season xwOBAcon
#   (-0.12): squaring up is a contact-hitter marker, the same phenomenon
#   that makes whiff rate carry a POSITIVE weight here.
# * AGE (-0.0016), Savant's IDEAL-ATTACK-ANGLE RATE (+0.0016), swing length,
#   swing efficiency, contact-depth variance.
#
# VALIDATED, frozen model trained on 2025 and applied UNCHANGED to 2026,
# predicting each hitter's REST-OF-SEASON xwOBAcon from his first N batted
# balls (see BMIELKE_TARGET below for why that target and not v8's):
#      N BBE   xwOBAcon   v8      v9      v9-v8         95% CI
#         25     +0.482  +0.668  +0.746   +0.077  [+0.035,+0.120]  SIG
#         30     +0.491  +0.691  +0.768   +0.077  [+0.042,+0.113]  SIG
#         50     +0.572  +0.696  +0.753   +0.057  [+0.024,+0.095]  SIG
#         80     +0.607  +0.712  +0.764   +0.052  [+0.014,+0.090]  SIG
#        120     +0.705  +0.753  +0.772   +0.019  [-0.019,+0.055]  n.s.
#        180     +0.778  +0.775  +0.765   -0.011  [-0.046,+0.022]  n.s.
#
# THE GAIN IS TARGET-SPECIFIC AND THAT IS AN HONEST LIMIT. Run against v8's
# original target — NEXT season rather than the rest of this one — v9 is a
# null everywhere (+0.032 to -0.029, no interval excluding zero, and
# significantly WORSE at 250 batted balls). v9 is the better answer to "how
# good is this hitter's contact for the rest of this season"; it is NOT a
# better answer to "how good will he be next year". The chip is shown beside
# tonight's game, so the former is the question it is actually asked.
BMIELKE_TARGET = "rest-of-season xwOBAcon"
# COEFFICIENT LADDER, keyed by batted balls. One fitted model per regime,
# because the right weights genuinely change with sample size:
#
#     trained at | fast-swing weight | outcome weight
#         30 BBE |            0.0181 |         0.0105   <- trust the swing
#    full season |            0.0013 |         0.0280   <- trust the results
#
# Using the 30-BBE model on a full-season hitter over-weights fast-swing rate
# by ~14x, which systematically UNDER-RATES slow-bat contact hitters — the
# people whose value is precisely in their outcomes. Measured residuals of
# next-year xwOBAcon, frozen 30-BBE model applied to full seasons:
# slow bats +0.0020, fast bats −0.0042. Refit in-regime: −0.0002 / −0.0003.
# The bias was a REGIME MISMATCH, not a blind spot in the feature set.
#
# Matching the regime also gains a little accuracy (+0.002 to +0.007 r), but
# calibration is the real reason: Will Smith should be judged on his .422
# xwOBAcon, not on a 0.07 fast-swing rate.
# One model, no rungs. Per feature: (main coef, coef on the sample-size
# interaction, mean/sd of the feature, mean/sd of the interaction column).
# The effective weight on feature j at n batted balls is
#     a_j/sd_j  +  b_j*(w-0.35)/sd_wj      with  w = n/(n+K)
# so it slides continuously from the low-sample regime to the full-season one.
# Read the pair for `fastsw` (+0.0132 main, -0.0040 interaction) against the
# pair for `wshrunk` (+0.0218, +0.0045): trust the swing early, the outcomes
# late. That is the same finding the five-rung ladder encoded, now smooth.
BMIELKE_COEF = (
    # (a_j,       b_j,       mu_j,      sd_j,     mu_wj,    sd_wj)
    (+0.012999, -0.003888,   0.21476,   0.17390, -0.00741,  0.03685),  # fastsw
    (+0.006365, +0.001121, 107.97770,   3.01949, -3.75853, 14.08758),  # evmax
    (+0.005233, -0.000484,   0.22076,   0.06526, -0.00866,  0.03036),  # whiff
    (+0.021603, +0.003564,   0.37898,   0.03943, -0.01330,  0.04979),  # wshrunk
    (+0.011607, +0.000910,   8.91119,   3.78951, -0.32291,  1.27662),  # aa
    (-0.006253, -0.002045,  29.62362,   3.51551, -1.04647,  3.89431),  # depth
)
BMIELKE_INTERCEPT = 0.372098
BMIELKE_WCENTRE = 0.35
# Reference mean/sd for the 100 +/- 10 scale, interpolated in n. A single
# reference would make a 30-BBE and a 400-BBE hitter incommensurable, because
# the model deliberately produces a tighter spread when it has less to go on.
# All anchored on the SAME population (MLB regulars, 2025) truncated to each n.
BMIELKE_REF_N = (25, 30, 40, 50, 65, 80, 100, 120, 150, 200)
BMIELKE_REF_MEAN = (0.374537, 0.371570, 0.370314, 0.370930, 0.372058,
                    0.371883, 0.371788, 0.372409, 0.372838, 0.373805)
BMIELKE_REF_SD = (0.042148, 0.043136, 0.044122, 0.044061, 0.043043,
                  0.043042, 0.044236, 0.044493, 0.044563, 0.044866)
BMIELKE_LG_WOBACON = 0.3807     # league xwOBAcon, the population prior
BMIELKE_SHRINK_K = 150          # BBE at which an outcome term gets half
                                # weight: w = n / (n + K)
# MINIMUMS. These are joint, not independent, and getting that wrong is how
# the v8 build shipped a dead low-sample rung: it demanded 120 swings, but a
# hitter with 25-30 batted balls has only about 65-80. Measured on real
# seasons, the 120-swing gate passed 0.4% of hitters at 25 batted balls and
# 1.3% at 30 — the entire regime the metric exists for was unreachable, and
# the research harness never caught it because it drew batted balls and
# swings INDEPENDENTLY from a full season. A real sample arrives at ~2.7
# swings per batted ball, so the swing floor has to sit under that ratio.
BMIELKE_MIN_BBE = 25
BMIELKE_MIN_SWINGS = 55
BMIELKE_MIN_BATSPEED = 25
# Above this the hitter's own xwOBAcon is the better number and the chip says
# so. Measured on the frozen 2026 test, predicting rest-of-season xwOBAcon:
# at 120 batted balls BMIELKE +0.772 vs xwOBAcon +0.705, at 180 it has
# reversed to +0.763 vs +0.778. The crossover sits between them.
BMIELKE_MAX_BBE = 175


# ---------------------------------------------------------------------------
# Count splits + run value
# ---------------------------------------------------------------------------
# The count is the single biggest context in a plate appearance and none of it
# was on screen. `balls`/`strikes` on a `_get_pitch_detail` row are PRE-pitch
# (verified: 0-0 is 24.7% of all pitches, and no row anywhere carries 4 balls
# or 3 strikes — impossible under a post-pitch reading).
#
# The first four buckets PARTITION every pitch; the last two deliberately
# OVERLAP them, because "what he does with two strikes" and "what he does at
# 3-0" are the two counts anyone actually asks about and neither is a clean
# slice of the first four. The table separates them with a rule.
_COUNT_PARTITION = ("First", "Ahead", "Even", "Behind")
_COUNT_OVERLAP = ("2 strikes", "3 balls")

# Run value per 100 pitches is meaningless against zero. Run expectancy is a
# martingale, so the mean over ALL pitches is ~0 by construction (measured
# +0.120 across 380,534 pitches — that number IS the check that the column
# means what it should). Within a bucket it need not be zero, so each gets a
# real baseline and the cell is coloured against THAT, not against 0.
#
# Measured on 200 hitters x 2025+2026, 380,534 pitches. Caveat worth keeping:
# the pool is established regulars, so this is a "quality regular" baseline
# and sits slightly above true all-MLB average — which is the right
# comparison for this panel, but do not quote it as the league mean.
#
# An earlier 25-hitter read gave First -0.20 and 3 balls +0.75 and looked like
# a real count effect. At 380k pitches the whole spread collapses to
# +0.04..+0.24. That was noise; do not reintroduce a "count shape" story.
_COUNT_RV_BASE = {"First": 0.080, "Ahead": 0.164, "Even": 0.238,
                  "Behind": 0.042, "2 strikes": 0.101, "3 balls": 0.134}


def count_buckets(b: int, s: int, is_pitcher: bool) -> List[str]:
    """Which count buckets a pre-pitch count falls in.

    Ahead/Behind are from the SHOWN player's point of view, so 0-2 is the
    pitcher's "Ahead" and the batter's "Behind". Shared by the count-splits
    table and the SP pitch-mix table so the two can never disagree."""
    out = []
    if b == 0 and s == 0:
        out.append("First")
    ahead = (s > b) if is_pitcher else (b > s)
    behind = (b > s) if is_pitcher else (s > b)
    if ahead:
        out.append("Ahead")
    elif behind:
        out.append("Behind")
    elif not (b == 0 and s == 0):
        out.append("Even")
    if s == 2:
        out.append("2 strikes")
    if b == 3:
        out.append("3 balls")
    return out


def pitch_mix_by_count(rows: List[dict]) -> List[dict]:
    """A pitcher's usage mix per count bucket — what he actually goes to when
    he is ahead, behind, or needs a strike.

    Returns one row per pitch type, each carrying its share WITHIN each
    bucket (so a bucket's column sums to 100%), sorted by overall usage."""
    order = _COUNT_PARTITION + _COUNT_OVERLAP
    tot: Dict[str, int] = {k: 0 for k in order}
    per: Dict[str, Dict[str, int]] = {}
    overall: Dict[str, int] = {}
    n_all = 0
    for r in rows:
        p = r.get("pitch")
        b, s = r.get("balls"), r.get("strikes")
        if not p or b is None or s is None:
            continue
        overall[p] = overall.get(p, 0) + 1
        n_all += 1
        for k in count_buckets(int(b), int(s), True):
            tot[k] += 1
            per.setdefault(p, {k2: 0 for k2 in order})[k] += 1
    out = []
    for p in sorted(overall, key=lambda x: -overall[x]):
        row = {"pitch": p, "all": overall[p] / n_all if n_all else 0.0}
        for k in order:
            row[k] = (per[p][k] / tot[k]) if tot.get(k) else None
        out.append(row)
    return out


def count_splits(rows: List[dict], is_pitcher: bool) -> List[dict]:
    """Per-count aggregates from cached pitch detail.

    "Ahead"/"Behind" are always from the SHOWN player's point of view, so a
    pitcher's "Ahead" is 0-2 and a batter's is 3-1.

    `rv` is Savant's own `delta_run_exp`, summed and put per 100 pitches. It
    ships in the CSV from the BATTING team's perspective, so it is negated for
    a pitcher — on this panel, higher is always better for the man shown."""
    acc: Dict[str, dict] = {k: {"pit": 0, "sw": 0, "whiff": 0, "bbe": 0,
                                "ev": [], "xw": [], "rv": 0.0}
                            for k in _COUNT_PARTITION + _COUNT_OVERLAP}
    for r in rows:
        b, s = r.get("balls"), r.get("strikes")
        if b is None or s is None:
            continue
        d = r.get("desc") or ""
        swung = d in _SWING_DESCS
        whiffed = d in _WHIFF_DESCS
        inplay = (r.get("ev") is not None and r.get("hc_x") is not None
                  and r.get("hc_y") is not None)
        rv = r.get("d_run_exp")
        for k in count_buckets(int(b), int(s), is_pitcher):
            a = acc[k]
            a["pit"] += 1
            a["sw"] += swung
            a["whiff"] += whiffed
            if inplay:
                a["bbe"] += 1
                a["ev"].append(r["ev"])
                a["xw"].append(r.get("xwoba") or 0.0)
            if rv is not None:
                a["rv"] += -rv if is_pitcher else rv
    out = []
    for k in _COUNT_PARTITION + _COUNT_OVERLAP:
        a = acc[k]
        if not a["pit"]:
            continue
        out.append({
            "split": k, "pit": a["pit"],
            "swing": a["sw"] / a["pit"],
            "whiff": (a["whiff"] / a["sw"]) if a["sw"] else None,
            "bbe": a["bbe"],
            "ev": (sum(a["ev"]) / len(a["ev"])) if a["ev"] else None,
            "xw": (sum(a["xw"]) / len(a["xw"])) if a["xw"] else None,
            "rv100": 100.0 * a["rv"] / a["pit"],
            # Edge over this bucket's league mark. The baseline is measured
            # from the BATTER's side, so it flips with the value itself for a
            # pitcher — comparing a negated RV against an un-negated baseline
            # would double the error and paint every pitcher green.
            "edge": (100.0 * a["rv"] / a["pit"])
                    - (-1 if is_pitcher else 1) * _COUNT_RV_BASE.get(k, 0.0),
            "base": (-1 if is_pitcher else 1) * _COUNT_RV_BASE.get(k, 0.0),
            "overlap": k in _COUNT_OVERLAP,
        })
    return out


# ---------------------------------------------------------------------------
# Batted-ball profile
# ---------------------------------------------------------------------------
# Trajectory mix, spray mix, and how often the defence bothers to move for
# him. `if_fielding_alignment` has been parsed into the pitch detail all along
# and never read (doc item 8). Values in the post-ban era are Standard /
# Infield shade / Strategic — "Infield shift" itself is effectively gone, so
# the useful question is simply how often the alignment is NOT standard.
_BB_SPLITS = ("All", "vL", "vR")


def batted_ball_profile(rows: List[dict]) -> List[dict]:
    """Trajectory + spray + alignment mix, overall and by opposing hand.

    Trajectory cuts are Statcast's: ground ball under 10 degrees, line drive
    to 25, fly ball to 50, pop-up above. Spray is oppo-SIGNED (negative pull,
    positive opposite field, for both hands) so one set of thresholds serves
    left and right without a flip at the call site."""
    acc = {k: {"gb": 0, "ld": 0, "fb": 0, "pu": 0, "n": 0,
               "pull": 0, "cent": 0, "oppo": 0, "spray_n": 0,
               "shift": 0, "align_n": 0} for k in _BB_SPLITS}
    for r in rows:
        if r.get("ev") is None or r.get("hc_x") is None or r.get("hc_y") is None:
            continue
        keys = ["All"]
        pt = r.get("p_throws")
        if pt == "L":
            keys.append("vL")
        elif pt == "R":
            keys.append("vR")
        la = r.get("la")
        ang = _spray_angle(r.get("hc_x"), r.get("hc_y"))
        if ang is not None and r.get("stand") in ("L", "R"):
            ang *= 1.0 if r["stand"] == "R" else -1.0
        else:
            ang = None
        al = r.get("if_align") or ""
        for k in keys:
            a = acc[k]
            a["n"] += 1
            if la is not None:
                a["gb" if la < 10 else "ld" if la < 25
                  else "fb" if la < 50 else "pu"] += 1
            if ang is not None:
                a["spray_n"] += 1
                a["pull" if ang < -15 else "oppo" if ang > 15 else "cent"] += 1
            if al:
                a["align_n"] += 1
                a["shift"] += al != "Standard"
    out = []
    for k in _BB_SPLITS:
        a = acc[k]
        if a["n"] < 10:
            continue
        p = lambda c, d: (a[c] / d) if d else None
        out.append({
            "split": k, "n": a["n"],
            "gb": p("gb", a["n"]), "ld": p("ld", a["n"]),
            "fb": p("fb", a["n"]), "pu": p("pu", a["n"]),
            "pull": p("pull", a["spray_n"]), "cent": p("cent", a["spray_n"]),
            "oppo": p("oppo", a["spray_n"]), "shift": p("shift", a["align_n"]),
        })
    return out


# ---------------------------------------------------------------------------
# Zone profile
# ---------------------------------------------------------------------------
# Where a hitter is vulnerable, crossed with where tonight's starter actually
# lives. WHIFF RATE is the metric, not xwOBA: 143 batted balls spread over
# nine cells is ~16 a cell and hopeless, while the same hitter has ~480 swings
# behind the same grid. Per-zone xwOBA would be mostly noise dressed as a
# heat map.
#
# Horizontal bins are BATTER-RELATIVE (inside / middle / outside) rather than
# catcher-view left-right, so a lefty and a righty read the same. `plate_x`
# flips between hands — higher is outside for a RHB and inside for a LHB — so
# it is signed by `stand` before binning. Vertical bins are thirds of THIS
# batter's own strike zone (`sz_top`/`sz_bot` come per pitch), not fixed
# heights, because a 6'7" hitter's letters are not a 5'8" hitter's.
_ZONE_HALF_W = 0.83          # ft: half the plate plus a ball's radius
_ZONE_COLS = ("In", "Mid", "Out")
_ZONE_ROWS = ("Up", "Mid", "Dn")


def _zone_cell() -> dict:
    return {"pit": 0, "sw": 0, "whiff": 0, "bbe": 0, "xw": 0.0}


def zone_cells(rows: List[dict], stand_filter: Optional[str] = None) -> dict:
    """3x3 in-zone grid plus an out-of-zone bucket.

    `stand_filter` restricts to one batter hand — used for the STARTER's grid,
    so his locations are the ones he actually uses against this hitter's side
    rather than a blend of both."""
    grid = [[_zone_cell() for _ in range(3)] for _ in range(3)]
    oz = _zone_cell()
    for r in rows:
        st = r.get("stand")
        if stand_filter and st != stand_filter:
            continue
        px, pz = r.get("plate_x"), r.get("plate_z")
        top, bot = r.get("sz_top"), r.get("sz_bot")
        if px is None or pz is None or not top or not bot or top <= bot:
            continue
        x = px * (1.0 if st == "R" else -1.0)      # higher = outside, both hands
        zf = (pz - bot) / (top - bot)
        if abs(x) <= _ZONE_HALF_W and 0.0 <= zf <= 1.0:
            c = 0 if x < -_ZONE_HALF_W / 3 else 2 if x > _ZONE_HALF_W / 3 else 1
            rw = 0 if zf > 2 / 3 else 2 if zf < 1 / 3 else 1
            cell = grid[rw][c]
        else:
            cell = oz
        cell["pit"] += 1
        d = r.get("desc") or ""
        if d in _SWING_DESCS:
            cell["sw"] += 1
            if d in _WHIFF_DESCS:
                cell["whiff"] += 1
        if r.get("ev") is not None and r.get("hc_x") is not None:
            cell["bbe"] += 1
            cell["xw"] += r.get("xwoba") or 0.0
    in_zone = sum(c["pit"] for row in grid for c in row)
    return {"grid": grid, "oz": oz, "total": in_zone + oz["pit"],
            "in_zone": in_zone}


def zone_matchup(bat: dict, sp: dict) -> Optional[dict]:
    """How much of the starter's work lands in the hitter's worst zones.

    Ranks the hitter's nine in-zone cells by whiff rate (cells with under 15
    swings are not ranked — a 2-for-3 cell is not a hole), takes the worst
    three, and reports the share of the starter's pitches that go there.
    1/3 of his in-zone pitches would be neutral, so that is the reference."""
    cells = []
    for r in range(3):
        for c in range(3):
            b = bat["grid"][r][c]
            if b["sw"] >= 15:
                cells.append((b["whiff"] / b["sw"], r, c))
    if len(cells) < 5:
        return None
    cells.sort(reverse=True)
    worst = {(r, c) for _, r, c in cells[:3]}
    sp_in = sum(sp["grid"][r][c]["pit"] for r in range(3) for c in range(3))
    if not sp_in:
        return None
    hit = sum(sp["grid"][r][c]["pit"] for (r, c) in worst)
    return {"share": hit / sp_in, "neutral": 1 / 3.0,
            "zones": sorted(worst),
            "whiff": sum(w for w, _, _ in cells[:3]) / 3.0}


# ---------------------------------------------------------------------------
# Plate discipline
# ---------------------------------------------------------------------------
# The classic complement to the batted-ball profile: what he swings at, and
# what he hits when he does. Uses the same zone geometry as the Zone tab
# (|plate_x| <= 0.83 ft, and this batter's own sz_bot..sz_top), so the two
# views can never disagree about what "in the zone" means.
#
# The header grid already carries FanGraphs' Z-Con%/Con%/SwStr%/Chase%. These
# will not match to the decimal and should not: FanGraphs uses its own zone
# definition and its own plate-appearance filters. They land within a couple
# of points, which is the check that this is computing the right thing.
def plate_discipline(rows: List[dict]) -> List[dict]:
    acc = {k: {"pit": 0, "iz": 0, "izsw": 0, "izwh": 0,
               "oz": 0, "ozsw": 0, "ozwh": 0, "wh": 0,
               "first": 0, "firstsw": 0} for k in _BB_SPLITS}
    for r in rows:
        px, pz = r.get("plate_x"), r.get("plate_z")
        top, bot = r.get("sz_top"), r.get("sz_bot")
        if px is None or pz is None or not top or not bot or top <= bot:
            continue
        keys = ["All"]
        pt = r.get("p_throws")
        if pt == "L":
            keys.append("vL")
        elif pt == "R":
            keys.append("vR")
        d = r.get("desc") or ""
        swung = d in _SWING_DESCS
        whiffed = d in _WHIFF_DESCS
        inzone = abs(px) <= _ZONE_HALF_W and bot <= pz <= top
        first = r.get("balls") == 0 and r.get("strikes") == 0
        for k in keys:
            a = acc[k]
            a["pit"] += 1
            a["wh"] += whiffed
            if inzone:
                a["iz"] += 1; a["izsw"] += swung; a["izwh"] += whiffed
            else:
                a["oz"] += 1; a["ozsw"] += swung; a["ozwh"] += whiffed
            if first:
                a["first"] += 1; a["firstsw"] += swung
    out = []
    for k in _BB_SPLITS:
        a = acc[k]
        if a["pit"] < 50:
            continue
        div = lambda n, d: (n / d) if d else None
        out.append({
            "split": k, "pit": a["pit"],
            "zone": div(a["iz"], a["pit"]),
            "zsw": div(a["izsw"], a["iz"]),
            "zcon": div(a["izsw"] - a["izwh"], a["izsw"]),
            "osw": div(a["ozsw"], a["oz"]),
            "ocon": div(a["ozsw"] - a["ozwh"], a["ozsw"]),
            "swstr": div(a["wh"], a["pit"]),
            "fsw": div(a["firstsw"], a["first"]),
        })
    return out


# ---------------------------------------------------------------------------
# How he is being ATTACKED lately
# ---------------------------------------------------------------------------
# The trend plot says a hitter has gone cold; it cannot say why. One ordinary
# cause is that pitchers changed their approach — more spin, fewer strikes,
# working further out of the zone. This contrasts a recent window against the
# rest of the season on the things a pitching staff actually controls, so a
# slump that is just variance looks flat here and a slump with a cause does
# not.
def attack_profile(rows: List[dict], recent_dates: set) -> List[dict]:
    """Pitch mix + zone/first-pitch-strike rates, recent window vs the rest.

    `recent_dates` is the set of game dates in the recent window; everything
    else in `rows` is the comparison. Splitting on dates rather than on a
    pitch count keeps it aligned with the games shown in the plot above."""
    def blank():
        return {"pit": 0, "fb": 0, "brk": 0, "off": 0, "iz": 0, "wh": 0,
                "sw": 0, "first": 0, "fstr": 0}
    acc = {"recent": blank(), "rest": blank()}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        a = acc["recent" if d in recent_dates else "rest"]
        a["pit"] += 1
        p = r.get("pitch") or ""
        if p in _FB_TYPES:
            a["fb"] += 1
        elif p in _BRK_TYPES:
            a["brk"] += 1
        elif p in _OFF_TYPES:
            a["off"] += 1
        px, pz = r.get("plate_x"), r.get("plate_z")
        top, bot = r.get("sz_top"), r.get("sz_bot")
        if px is not None and pz is not None and top and bot and top > bot:
            if abs(px) <= _ZONE_HALF_W and bot <= pz <= top:
                a["iz"] += 1
        desc = r.get("desc") or ""
        if desc in _SWING_DESCS:
            a["sw"] += 1
            if desc in _WHIFF_DESCS:
                a["wh"] += 1
        if r.get("balls") == 0 and r.get("strikes") == 0:
            a["first"] += 1
            a["fstr"] += desc not in _NON_STRIKE_DESCS
    out = []
    for key, label in (("recent", "Recent"), ("rest", "Season")):
        a = acc[key]
        if a["pit"] < 60:
            continue
        div = lambda n, d: (n / d) if d else None
        out.append({"split": label, "pit": a["pit"],
                    "fb": div(a["fb"], a["pit"]), "brk": div(a["brk"], a["pit"]),
                    "off": div(a["off"], a["pit"]), "zone": div(a["iz"], a["pit"]),
                    "fstr": div(a["fstr"], a["first"]),
                    "whiff": div(a["wh"], a["sw"])})
    return out


# ---------------------------------------------------------------------------
# Spray direction BY PITCH LOCATION
# ---------------------------------------------------------------------------
# Pull rate over time on its own is close to meaningless, because direction is
# mostly dictated by where the ball was pitched — you pull the inside one and
# serve the outside one. A hitter whose pull rate drops may have changed
# nothing at all; he may simply be getting worked away. (This project already
# has the sharp end of that finding: going oppo on outer-third pitches
# predicts WORSE future production there, i.e. it is the signature of being
# beaten by the pitch rather than of choosing to use the field.)
#
# So direction is reported WITHIN each location band — that part is the
# hitter — alongside how the share of pitches to that band has MOVED lately,
# which is the pitcher. Read together they separate his doing from theirs.
_LOC_BANDS = ("In", "Mid", "Out")


def spray_by_location(rows: List[dict], recent_dates: set) -> List[dict]:
    """Pull/centre/oppo split by inside-middle-outside, plus the change in how
    often he is pitched there.

    Horizontal bands are BATTER-RELATIVE (plate_x signed by `stand`), the same
    convention as the Zone tab. Spray is oppo-signed, so the +-15 degree cuts
    mean the same thing for a lefty and a righty."""
    edge = _ZONE_HALF_W / 3
    acc = {b: {"bbe": 0, "pull": 0, "cent": 0, "oppo": 0,
               "seen": 0, "seen_recent": 0} for b in _LOC_BANDS}
    n_all = n_recent = 0
    for r in rows:
        px, st = r.get("plate_x"), r.get("stand")
        if px is None or st not in ("L", "R"):
            continue
        x = px * (1.0 if st == "R" else -1.0)
        band = "In" if x < -edge else "Out" if x > edge else "Mid"
        a = acc[band]
        a["seen"] += 1; n_all += 1
        if r.get("date") in recent_dates:
            a["seen_recent"] += 1; n_recent += 1
        if r.get("ev") is None or r.get("hc_x") is None:
            continue
        ang = _spray_angle(r.get("hc_x"), r.get("hc_y"))
        if ang is None:
            continue
        ang *= 1.0 if st == "R" else -1.0
        a["bbe"] += 1
        a["pull" if ang < -15 else "oppo" if ang > 15 else "cent"] += 1
    out = []
    for b in _LOC_BANDS:
        a = acc[b]
        if a["bbe"] < 8:
            continue
        div = lambda n, d: (n / d) if d else None
        seen = div(a["seen"], n_all)
        seen_r = div(a["seen_recent"], n_recent) if n_recent else None
        out.append({"band": b, "bbe": a["bbe"],
                    "pull": div(a["pull"], a["bbe"]),
                    "cent": div(a["cent"], a["bbe"]),
                    "oppo": div(a["oppo"], a["bbe"]),
                    "seen": seen,
                    "dseen": (seen_r - seen) if (seen_r is not None
                                                 and seen is not None) else None})
    return out


# ---------------------------------------------------------------------------
# Performance by days of rest
# ---------------------------------------------------------------------------
# The schedule half of "form". `batter_days_since_prev_game` has been in the
# Savant CSV all along with nothing reading it.
#
# MIND THE OFF-BY-ONE: the column counts days SINCE the last game, so 1 means
# he played yesterday — the ordinary daily schedule, not a back-to-back. Days
# OFF is therefore rest-1, and that is what the labels say. A value of 0 is
# the second game of a doubleheader and folds into "0 off".
_REST_BUCKETS = (("0 off", "0 off"), ("1 off", "1 off"), ("2+ off", "2+ off"))


def rest_splits(rows: List[dict]) -> List[dict]:
    """Contact quality by days off before the game.

    Aggregated per PITCH and then pooled, not averaged per game, so a
    one-batted-ball afternoon cannot outvote a full one."""
    acc = {lab: {"g": set(), "bbe": 0, "ev": 0.0, "xw": 0.0, "brl": 0,
                 "sw": 0, "wh": 0} for _, lab in _REST_BUCKETS}
    for r in rows:
        rest = r.get("rest")
        if rest is None:
            continue
        off = max(0, int(rest) - 1)
        lab = "0 off" if off == 0 else "1 off" if off == 1 else "2+ off"
        a = acc[lab]
        if r.get("date"):
            a["g"].add(r["date"])
        d = r.get("desc") or ""
        if d in _SWING_DESCS:
            a["sw"] += 1
            if d in _WHIFF_DESCS:
                a["wh"] += 1
        ev = r.get("ev")
        if ev is not None and r.get("hc_x") is not None:
            a["bbe"] += 1
            a["ev"] += ev
            a["xw"] += r.get("xwoba") or 0.0
            la = r.get("la")
            if la is not None and _is_barrel(ev, la):
                a["brl"] += 1
    out = []
    for _, lab in _REST_BUCKETS:
        a = acc[lab]
        if a["bbe"] < 10:
            continue
        out.append({"split": lab, "g": len(a["g"]), "bbe": a["bbe"],
                    "ev": a["ev"] / a["bbe"], "xw": a["xw"] / a["bbe"],
                    "brl": a["brl"] / a["bbe"],
                    "whiff": (a["wh"] / a["sw"]) if a["sw"] else None})
    return out


def spray_hard_hit(ev: Optional[float], spray_oppo_deg: Optional[float]
                   ) -> Optional[bool]:
    """Spray-adjusted hard hit (DHH): is this ball hard FOR ITS DIRECTION?

    `spray_oppo_deg` must be oppo-signed — negative pull, positive opposite
    field, for BOTH hands (multiply a raw catcher-view spray angle by +1 for
    a RHB and -1 for a LHB).

    Validated against plain HardHit% on 193 hitters with full 2024 and 2025
    seasons: year-over-year stickiness .831 -> .884, next-year wOBAcon
    .726 -> .749. That replicates the published FanGraphs "Spray DHH%"
    result (.674 -> .728 and .328 -> .339) on an independent sample —
    the same +0.05 gain in stickiness. Same-year wOBAcon is fractionally
    WORSE (.868 vs .852), which is the honest trade: this is a better read
    on the hitter, not a better description of what already happened."""
    if ev is None or spray_oppo_deg is None:
        return None
    s = max(_SPRAY_BINS[0], min(_SPRAY_BINS[-1], float(spray_oppo_deg)))
    b = min(_SPRAY_BINS, key=lambda c: abs(c - s))
    return ev >= SPRAY_EV_P75[b]


# ---------------------------------------------------------------------------
# Split reliability.
#
# THE POINT OF THIS BLOCK: a split table that prints rates and nothing else is
# actively misleading, because the whole spread between its rows is usually
# sampling noise. Measured below — a 50-PA split carries a 1-sigma band of
# +-46 wRC+ points, so a hitter reading 145 with RISP against a 100 season
# line is an ENTIRELY ORDINARY hitter having an ordinary 50 PAs.
#
# SPLIT_NOISE_S is the sampling-noise scale in  sd(split wRC+) = sqrt(S/PA).
# Estimated directly as mean(d^2 * PA) over the splits that CANNOT carry a
# true effect — month and inning — where d is the split's wRC+ minus the
# player's own season wRC+. (Month splits are safe to use for this because
# this project has already established there is no hot hand; inning splits
# because there is no mechanism by which a hitter is truly better in the 4th
# than the 5th.) The two groups are independent samples and agree to 8%:
# innings 109,657 (n=2,131) vs months 101,694 (n=943).
#
# A one-parameter moment estimator was used ON PURPOSE. Fitting S and a true
# variance T jointly per split — regressing d^2 on 1/PA — is UNIDENTIFIABLE on
# a single season: it returned a negative T for 19 of 36 splits (including
# vs LHP, which certainly is real) and bootstrap CIs on K spanning [3, 731]
# PA. Do not re-propose a per-split fitted K without multi-season data.
SPLIT_NOISE_S = 107200.0

# The platoon differential IS real, and year-over-year is what shows it when a
# single season cannot. Correlation of (wRC+ vs LHP - wRC+ vs RHP) between
# consecutive seasons, hitters with >=60 PA vs LHP and >=150 vs RHP in both:
#
#   2022->23 +0.262 | 2023->24 +0.151 | 2024->25 +0.305 | 2025->26 +0.172
#   POOLED n=806    +0.229   95% CI [+0.160, +0.293]
#
# Positive in all four independent pairs. True variance follows as
# r * var(observed diff) = 0.229 * 1850 = 424; the independent moment estimate
# var(obs) - var(noise) = 1850 - 1290 = 560 agrees, which also cross-checks
# SPLIT_NOISE_S since the noise term is computed from it.
#
# CONSEQUENCE, and it is the number worth knowing: at a typical full season
# (100 PA vs LHP, 240 vs RHP) only **22%** of a hitter's observed platoon gap
# is real. Four fifths of every platoon split ever quoted is noise.
SPLIT_PLATOON_T = 420.0

# Mean platoon differential across 1,414 player-seasons: +0.6 wRC+, i.e. zero.
# The population a hitter's platoon gap should be shrunk TOWARD is "no gap".
SPLIT_PLATOON_MEAN = 0.0


def split_noise_sd(pa: Optional[float]) -> Optional[float]:
    """1-sigma sampling band on a split's wRC+, in wRC+ points.

    30 PA -> 60 | 50 -> 46 | 100 -> 33 | 200 -> 23 | 300 -> 19."""
    try:
        pa = float(pa)
    except (TypeError, ValueError):
        return None
    if pa <= 0:
        return None
    return math.sqrt(SPLIT_NOISE_S / pa)


def split_z(split_wrc: Optional[float], season_wrc: Optional[float],
            pa: Optional[float]) -> Optional[float]:
    """How many sampling sigmas a split sits from the player's own season
    line. This is the number the colour scale should key on — NOT the raw
    gap, which is dominated by whichever split happens to be smallest."""
    sd = split_noise_sd(pa)
    if sd is None or split_wrc is None or season_wrc is None:
        return None
    try:
        return (float(split_wrc) - float(season_wrc)) / sd
    except (TypeError, ValueError):
        return None


def shrink_platoon(wrc_vs: Optional[float], pa_vs: Optional[float],
                   wrc_other: Optional[float], pa_other: Optional[float]
                   ) -> Optional[dict]:
    """Regress an observed platoon gap to its believable size.

    Returns {'obs', 'true', 'weight', 'sd'} where `true` is the shrunk gap
    (vs-side minus other-side) and `weight` the fraction of the observed gap
    kept. Weight is T / (T + S/PA_vs + S/PA_other) — the standard
    signal-over-signal-plus-noise, with both sides' noise counted because the
    DIFFERENCE carries both.

    Shrinks toward SPLIT_PLATOON_MEAN (zero), not toward a league platoon
    constant: the mean differential over 1,414 player-seasons is +0.6 wRC+."""
    try:
        wrc_vs = float(wrc_vs); wrc_other = float(wrc_other)
        pa_vs = float(pa_vs); pa_other = float(pa_other)
    except (TypeError, ValueError):
        return None
    if pa_vs <= 0 or pa_other <= 0:
        return None
    noise = SPLIT_NOISE_S / pa_vs + SPLIT_NOISE_S / pa_other
    w = SPLIT_PLATOON_T / (SPLIT_PLATOON_T + noise)
    obs = wrc_vs - wrc_other
    return {"obs": obs, "weight": w,
            "true": SPLIT_PLATOON_MEAN + (obs - SPLIT_PLATOON_MEAN) * w,
            "sd": math.sqrt(noise)}


# Split display groups. Order is the order they render. Each group is a set of
# rows that belong on screen together; the ones marked `partition` sum to the
# season and so their PA column is worth reading as a share.
SPLIT_GROUPS = (
    ("Platoon",  ("vs LHP", "vs RHP"), True),
    ("Venue",    ("Home", "Away"), True),
    ("Base",     ("Empty", "Runners on", "RISP", "Loaded"), False),
    ("Outs",     ("0 out", "1 out", "2 out"), True),
    ("Count",    ("Ahead", "Even", "Behind", "Full count"), False),
    ("Slot",     ("Bat 1st", "Bat 2nd", "Bat 3rd", "Bat 4th", "Bat 5th",
                  "Bat 6th", "Bat 7th", "Bat 8th", "Bat 9th"), True),
    ("Inning",   ("Inn 1", "Inn 2", "Inn 3", "Inn 4", "Inn 5", "Inn 6",
                  "Inn 7", "Inn 8", "Inn 9", "Extras"), True),
    ("Month",    ("Mar/Apr", "May", "Jun", "Jul", "Aug"), True),
)

# Minimum PA before a split row is worth drawing at all. At 15 PA the noise
# band is +-85 wRC+, which is not a measurement of anything.
SPLIT_MIN_PA = 15


def split_profile(splits: Dict[str, Dict[int, dict]], pid: int,
                  season_wrc: Optional[float] = None,
                  min_pa: float = SPLIT_MIN_PA) -> List[dict]:
    """Flatten the league-wide split boards into this hitter's rows.

    `splits` is get_fg_splits output — {label: {mlbam: row}}. Returns one dict
    per available split with the rates FanGraphs gives plus the two things it
    does not: the sampling band on the split, and how many sigmas the split
    sits from the player's own season line.

    `season_wrc` anchors the z-scores. Passed in rather than derived from the
    splits because the season line belongs to the leaders board, and deriving
    it by PA-weighting the splits would make every partition's z sum to zero
    by construction.

    `min_pa` gates thin rows. Pass 0 to keep everything — the UI's "all"
    toggle does, and a row that thin is still a real observation, just one
    whose band is wider than the chart. The default only decides what shows
    by DEFAULT; it is not a claim that thinner splits should be unavailable.
    """
    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Fall back to the PA-weighted mean over a partition when no season line
    # was supplied. Less correct than the board's own figure (see above) but
    # better than dropping the column entirely.
    if season_wrc is None:
        num = den = 0.0
        for lab in ("Home", "Away"):
            row = (splits.get(lab) or {}).get(pid)
            if not row:
                continue
            pa, w = f(row.get("PA")), f(row.get("wRC+"))
            if pa and w is not None:
                num += pa * w
                den += pa
        season_wrc = (num / den) if den else None

    out: List[dict] = []
    for group, labels, partition in SPLIT_GROUPS:
        total = 0.0
        rows = []
        for lab in labels:
            row = (splits.get(lab) or {}).get(pid)
            if not row:
                continue
            pa = f(row.get("PA"))
            if pa is None or pa < min_pa:
                continue
            wrc = f(row.get("wRC+"))
            rows.append({
                "group": group, "split": lab, "partition": partition,
                "pa": pa, "wrc": wrc,
                "woba": f(row.get("wOBA")), "iso": f(row.get("ISO")),
                "babip": f(row.get("BABIP")), "avg": f(row.get("AVG")),
                "obp": f(row.get("OBP")), "slg": f(row.get("SLG")),
                "bb": f(row.get("BB%")), "k": f(row.get("K%")),
                "sd": split_noise_sd(pa),
                "z": split_z(wrc, season_wrc, pa),
            })
            total += pa
        for r in rows:
            r["share"] = (r["pa"] / total) if (partition and total) else None
        out.extend(rows)
    return out


def _bmielke_ref(n: float) -> tuple:
    """Reference mean/sd at n batted balls, linearly interpolated and flat
    outside the fitted range (the model is greyed above the cap anyway)."""
    ns = BMIELKE_REF_N
    if n <= ns[0]:
        return BMIELKE_REF_MEAN[0], BMIELKE_REF_SD[0]
    if n >= ns[-1]:
        return BMIELKE_REF_MEAN[-1], BMIELKE_REF_SD[-1]
    i = next(k for k in range(1, len(ns)) if n <= ns[k])
    t = (n - ns[i - 1]) / (ns[i] - ns[i - 1])
    return (BMIELKE_REF_MEAN[i - 1] + t * (BMIELKE_REF_MEAN[i] - BMIELKE_REF_MEAN[i - 1]),
            BMIELKE_REF_SD[i - 1] + t * (BMIELKE_REF_SD[i] - BMIELKE_REF_SD[i - 1]))


def bmielke(rows: List[dict], prior_wobacon: Optional[float] = None,
            prior_bbe: Optional[int] = None) -> Optional[dict]:
    """BMIELKE v9 from a hitter's cached pitch detail.

    `rows` are `_get_pitch_detail(pid, "batter")` records. `prior_wobacon` /
    `prior_bbe` are LAST season's xwOBAcon and batted-ball count; supplying
    them is what makes the early-season read work, and omitting them degrades
    gracefully to the league prior (which is exactly what v8 did).

    Returns {index, raw, bbe, swings, wobacon, outcome_weight, prior,
    prior_weight} or None when there is not enough to say anything."""
    ev, xw, bat, aa, depth = [], [], [], [], []
    swings = whiffs = 0
    for r in rows:
        d = r.get("desc") or ""
        if d in _SWING_DESCS:
            swings += 1
            whiffs += d in _WHIFF_DESCS
            # the swing-path pair is measured on SWINGS, not just contact —
            # that is most of why they stabilise faster than any outcome
            if r.get("attack_angle") is not None:
                aa.append(r["attack_angle"])
            if r.get("icept_y") is not None:
                depth.append(r["icept_y"])
        if r.get("bat_speed") is not None:
            bat.append(r["bat_speed"])
        e = r.get("ev")
        # BALLS IN PLAY only. An exit velocity alone is not enough — fouls
        # carry one too, and counting them doubled the sample and dragged
        # xwOBAcon from .38 to .28. `hc_x/hc_y` exist only for a ball that
        # landed, which is exactly the population the model was fitted on.
        if e is None or r.get("hc_x") is None or r.get("hc_y") is None:
            continue
        ev.append(e)
        xw.append(r.get("xwoba") or 0.0)
    if (len(ev) < BMIELKE_MIN_BBE or swings < BMIELKE_MIN_SWINGS
            or len(bat) < BMIELKE_MIN_BATSPEED
            or len(aa) < 25 or len(depth) < 25):
        return None
    n = len(ev)
    # Peak power, not average: the 98th percentile is what separates hitters.
    # LINEARLY INTERPOLATED, matching numpy's default — the model was fitted
    # that way, and rounding to the nearest index instead moved the index by
    # up to 0.3 points against the coefficients it is paired with.
    srt = sorted(ev)
    pos = 0.98 * (n - 1)
    lo = int(pos)
    evmax = srt[lo] + (pos - lo) * (srt[min(lo + 1, n - 1)] - srt[lo])
    fastsw = sum(1 for b in bat if b >= 75.0) / len(bat)
    wobacon = sum(xw) / n
    # TWO-STAGE SHRINKAGE. His own history sets the prior (itself regressed to
    # the league by how much of it there is); this season's balls then move him
    # off it in proportion to how many of them there are.
    if prior_wobacon is not None and prior_bbe:
        pw = prior_bbe / (prior_bbe + BMIELKE_SHRINK_K)
        base = BMIELKE_LG_WOBACON + (prior_wobacon - BMIELKE_LG_WOBACON) * pw
    else:
        pw, base = 0.0, BMIELKE_LG_WOBACON
    w = n / (n + BMIELKE_SHRINK_K)
    wshrunk = base + (wobacon - base) * w
    vals = (fastsw, evmax, whiffs / swings, wshrunk,
            sum(aa) / len(aa), sum(depth) / len(depth))
    wc = w - BMIELKE_WCENTRE
    raw = BMIELKE_INTERCEPT + sum(
        a * ((v - m) / s) + b * ((v * wc - mw) / sw)
        for v, (a, b, m, s, mw, sw) in zip(vals, BMIELKE_COEF))
    ref_mean, ref_sd = _bmielke_ref(n)
    return {"index": 100.0 + 10.0 * (raw - ref_mean) / ref_sd, "raw": raw,
            "bbe": n, "swings": swings, "wobacon": wobacon,
            "outcome_weight": w, "prior": base, "prior_weight": pw}


# ---------------------------------------------------------------------------
# Pitch tunneling
# ---------------------------------------------------------------------------
# The hitter's commit point: ~23.8ft from the plate, the last moment he can
# start a swing and still get the barrel there. Two pitches still on top of
# each other HERE but far apart at the plate are "tunneled" — he has to
# decide before he can tell them apart.
TUNNEL_COMMIT_Y_FT = 23.8
# Savant reports plate_x/plate_z at the FRONT edge of the plate, so the
# arrival comparison uses the same reference the zone squares draw against
PLATE_FRONT_Y_FT = 17.0 / 12.0
# Savant measures vx0/vy0/vz0 at 50ft (mirrors pitch_sim.MEASUREMENT_Y_FT)
_KIN_MEASURE_Y_FT = 50.0
# Half the plate (17" / 2) in feet — the horizontal edge of the strike zone,
# shared by the zone squares and the plate-discipline rates
_ZONE_HALF_W_FT = 0.708


def pitch_state_at_y(kin: dict, y_ft: float) -> Optional[tuple]:
    """(x, z, vx, vy, vz) in Savant coords where a pitch crosses `y_ft` from
    the plate.

    `kin` is the mean Hawk-Eye state get_sp_movement keeps per pitch type:
    velocity/acceleration measured at y=50ft plus the release point. Same
    two-step reconstruction pitch_sim.savant_pitch_trajectory uses — back the
    velocity out to release, then integrate forward under constant accel.
    Returns None when the kinematics are missing or degenerate."""
    try:
        rx, ry, rz = (kin["release_pos_x"], kin["release_pos_y"],
                      kin["release_pos_z"])
        vx0, vy0, vz0 = kin["vx0"], kin["vy0"], kin["vz0"]
        ax, ay, az = kin["ax"], kin["ay"], kin["az"]
    except (KeyError, TypeError):
        return None
    if not vy0 or not ay or ry <= y_ft:
        return None

    # Step 1: measurement point (y=50) back to release. vy0 is negative and
    # ay positive (drag), so this root comes out negative — time runs
    # backwards from the measurement point to the hand.
    disc = vy0 * vy0 + 2.0 * ay * (ry - _KIN_MEASURE_Y_FT)
    if disc < 0:
        return None
    t_back = (-vy0 - math.sqrt(disc)) / ay
    vx_r, vy_r, vz_r = (vx0 + ax * t_back, vy0 + ay * t_back,
                        vz0 + az * t_back)

    # Step 2: forward from release to the requested y. Smaller root = the
    # first (only) crossing on the way to the plate.
    disc2 = vy_r * vy_r - 2.0 * ay * (ry - y_ft)
    if disc2 < 0:
        return None
    t = (-vy_r - math.sqrt(disc2)) / ay
    if t <= 0:
        return None
    return (rx + vx_r * t + 0.5 * ax * t * t,
            rz + vz_r * t + 0.5 * az * t * t,
            vx_r + ax * t, vy_r + ay * t, vz_r + az * t)


def pitch_xz_at_y(kin: dict, y_ft: float) -> Optional[tuple]:
    """Position only — see pitch_state_at_y."""
    st = pitch_state_at_y(kin, y_ft)
    return None if st is None else (st[0], st[1])


def approach_angles(kin: dict) -> tuple:
    """(VAA, HAA) in degrees at the front of the plate. VAA is negative — the
    ball is always descending — so a FLATTER (nearer zero) angle is the
    'rides through the zone' look that plays up off a low slot."""
    st = pitch_state_at_y(kin, PLATE_FRONT_Y_FT)
    if st is None:
        return None, None
    _x, _z, vx, vy, vz = st
    if abs(vy) < 1:
        return None, None
    return (math.degrees(math.atan2(vz, abs(vy))),
            math.degrees(math.atan2(vx, abs(vy))))


# --- pitch uniqueness ------------------------------------------------------
# How far a pitch's SHAPE and APPROACH sit from what its arm slot, velocity,
# extension, release point and target height predict. Coefficients are an OLS
# fit over the 2026 league (per pitch type) done offline — the alternative is
# fetching 400 pitchers' pitch-by-pitch data at runtime.
#
# WHAT IT IS WORTH (3-season study, 2024-26): the z-score predicts whiff rate
# on SLIDERS (r=+.23 pooled, significant every season) and CHANGEUPS (r=+.16),
# and predicts NOTHING on four-seamers (r=-.06) or sweepers/curves. It is a
# stable trait — same pitcher+pitch year over year is r=+.65 — but it is a
# modest edge, not a headline number. Shown for SL/CH/FS/FC/SI; suppressed on
# FF/ST/CU where it has no demonstrated relationship to results.
UNIQ_FEATURES = ("velo", "arm_angle", "ext", "rel_z", "rel_x_arm", "plate_z")
UNIQ_PREDICTIVE = {"SL", "CH", "FS", "FC", "SI"}
PITCH_UNIQ_MODEL = {
    "CH": {"n": 200,
           "ivb": ([-19.6225, -0.146951, 0.110677, 1.0182, 2.68753, 0.323888, 5.65925], 2.8662),
           "hb_arm": ([8.62749, 0.075563, -0.066112, -0.532374, 0.3406, 0.005078, 1.55375], 2.2564),
           "vaa": ([-12.6144, 0.069537, 0.01052, 0.079679, -0.792019, 0.027579, 1.58798], 0.285),
           },
    "CU": {"n": 137,
           "ivb": ([-34.4048, 0.270352, -0.108352, 0.326465, -0.196376, 1.38144, 2.01253], 3.4486),
           "hb_arm": ([-51.4929, 0.529621, 0.176814, -1.7293, 0.165206, 0.477763, 1.36013], 3.2751),
           "vaa": ([-16.5568, 0.137635, -0.011488, 0.016131, -1.03885, 0.1433, 1.23539], 0.381),
           },
    "FC": {"n": 128,
           "ivb": ([-15.7841, 0.190071, 0.019808, -0.094343, -0.055281, -0.399472, 3.13935], 2.068),
           "hb_arm": ([-9.83085, 0.199096, -0.05275, -1.03509, -0.700057, 0.115103, 1.03647], 1.7802),
           "vaa": ([-11.1555, 0.091928, 0.00144, -0.047477, -1.06959, -0.040938, 1.37126], 0.2058),
           },
    "FF": {"n": 260,
           "ivb": ([7.50147, -0.016065, 0.115389, 0.805081, 0.531833, -0.276676, -1.00246], 1.643),
           "hb_arm": ([0.651313, 0.199129, -0.186045, -0.552671, 0.26651, 0.266349, -1.04044], 2.3789),
           "vaa": ([-7.94249, 0.062069, 0.010732, 0.040175, -1.01252, -0.030058, 0.936915], 0.1571),
           },
    "FS": {"n": 45,
           "ivb": ([10.212, 0.167143, 0.14715, -3.35648, -1.06591, -2.7e-05, 0.684686], 2.3547),
           "hb_arm": ([5.83929, 0.364277, -0.144062, -1.95145, -1.61787, -1.18616, 2.41467], 2.5986),
           "vaa": ([-9.45735, 0.100072, 0.016757, -0.363839, -1.20394, 0.008163, 1.10909], 0.2455),
           },
    "SI": {"n": 223,
           "ivb": ([-19.0326, -0.025627, 0.184151, 1.74545, 1.03531, 0.034761, 2.69165], 2.2993),
           "hb_arm": ([-10.6767, 0.278326, -0.08906, -0.39116, 1.20556, 0.439511, -1.03905], 1.8859),
           "vaa": ([-11.0429, 0.06369, 0.017916, 0.147625, -0.94384, 0.010262, 1.3012], 0.2296),
           },
    "SL": {"n": 182,
           "ivb": ([-20.5208, 0.186743, -0.010834, -0.203023, -0.414619, 0.332061, 5.23101], 2.9965),
           "hb_arm": ([-39.3005, 0.456921, -0.054686, -0.926252, 1.2849, -0.133885, -1.68614], 2.5074),
           "vaa": ([-13.2384, 0.108071, -0.000205, -0.046756, -1.08323, 0.032154, 1.53536], 0.2907),
           },
    "ST": {"n": 127,
           "ivb": ([-6.83167, -0.055155, -0.098059, 1.9567, -0.314274, -0.037673, 2.39807], 3.2269),
           "hb_arm": ([-46.803, 0.300409, -0.116224, 0.160225, 2.76625, -0.523977, -1.35821], 2.4527),
           "vaa": ([-12.8146, 0.094668, -0.009364, 0.184353, -1.08753, -0.000791, 1.25606], 0.3232),
           },
}


def pitch_uniqueness(pt: dict, throws: Optional[str],
                     arm_angle: Optional[float]) -> Optional[dict]:
    """How atypical one pitch's shape and approach are for its arm slot.

    `pt` is a get_sp_movement()['pitches'] entry (velo, mean_ivb/mean_hb,
    mean_rel_x/z, ext, loc, kin). Returns {'uniq': rms z across iVB / arm-side
    break / approach angle, 'z_ivb', 'z_hb_arm', 'z_vaa'}, or None when the
    model has no entry for the pitch type or an input is missing.

    hb and rel_x arrive in Savant's catcher's view; they are mirrored so
    "arm side" is positive for both hands, matching the fit."""
    code = PITCH_ABBREV.get(pt.get("pitch", ""), "")
    m = PITCH_UNIQ_MODEL.get(code)
    if m is None or throws not in ("R", "L"):
        return None
    loc = pt.get("loc") or []
    if not loc or not pt.get("kin"):
        return None
    vaa, _haa = approach_angles(pt["kin"])
    plate_z = _avg([p[1] for p in loc])
    feats = (pt.get("velo"), arm_angle, pt.get("ext"), pt.get("mean_rel_z"),
             pt.get("mean_rel_x"), plate_z)
    actuals = (pt.get("mean_ivb"), pt.get("mean_hb"), vaa)
    if any(v is None for v in feats + actuals):
        return None
    mirror = -1.0 if throws == "R" else 1.0
    velo, arm, ext, rel_z, rel_x, pz = feats
    x = (1.0, velo, arm, ext, rel_z, rel_x * mirror, pz)
    out = {}
    for key, actual in (("ivb", pt["mean_ivb"]),
                        ("hb_arm", pt["mean_hb"] * mirror),
                        ("vaa", vaa)):
        coef, sd = m[key]
        pred = sum(c * v for c, v in zip(coef, x))
        out[f"z_{key}"] = (actual - pred) / sd if sd else 0.0
    zs = (out["z_ivb"], out["z_hb_arm"], out["z_vaa"])
    out["uniq"] = math.sqrt(sum(z * z for z in zs) / len(zs))
    return out


def compute_tunnels(pitches: List[dict], max_pitches: int = 4) -> List[dict]:
    """Pairwise tunnel metrics across a pitcher's most-used pitches.

    Takes get_sp_movement()['pitches'] (usage-sorted). Per pair returns the
    separation in INCHES at the commit point ('tunnel' — smaller is better,
    the pitches look alike while the decision is made) and at the plate
    ('plate' — bigger is better, they finish apart), plus 'ratio' =
    plate/tunnel, the standard break-to-tunnel read, and the release
    separation for context. Sorted best ratio first."""
    usable = [p for p in pitches[:max_pitches] if p.get("kin")]
    out = []
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            ca = pitch_xz_at_y(a["kin"], TUNNEL_COMMIT_Y_FT)
            cb = pitch_xz_at_y(b["kin"], TUNNEL_COMMIT_Y_FT)
            pa = pitch_xz_at_y(a["kin"], PLATE_FRONT_Y_FT)
            pb = pitch_xz_at_y(b["kin"], PLATE_FRONT_Y_FT)
            if not (ca and cb and pa and pb):
                continue
            tunnel = 12.0 * math.hypot(ca[0] - cb[0], ca[1] - cb[1])
            plate = 12.0 * math.hypot(pa[0] - pb[0], pa[1] - pb[1])
            rel = None
            if (a.get("mean_rel_x") is not None
                    and b.get("mean_rel_x") is not None):
                rel = 12.0 * math.hypot(a["mean_rel_x"] - b["mean_rel_x"],
                                        a["mean_rel_z"] - b["mean_rel_z"])
            # Velocity and break separation belong on the same card: a pair
            # can tunnel perfectly and still be readable if they arrive at the
            # same speed, and the ratio alone cannot tell you which lever the
            # pitcher is actually pulling.
            dv = (abs(a["velo"] - b["velo"])
                  if a.get("velo") is not None and b.get("velo") is not None
                  else None)
            dbrk = None
            if (a.get("mean_hb") is not None and b.get("mean_hb") is not None
                    and a.get("mean_ivb") is not None
                    and b.get("mean_ivb") is not None):
                dbrk = math.hypot(a["mean_hb"] - b["mean_hb"],
                                  a["mean_ivb"] - b["mean_ivb"])
            out.append({
                "a": a["pitch"], "b": b["pitch"],
                "tunnel": tunnel, "plate": plate,
                "dvelo": dv, "dbreak": dbrk,
                # Guard a near-zero denominator: two pitches that genuinely
                # overlap at the commit point would divide to infinity
                "ratio": (plate / tunnel) if tunnel >= 0.5 else None,
                "release": rel,
                "n": min(a["n"], b["n"]),
            })
    out.sort(key=lambda d: -(d["ratio"] or 0))
    return out


# ---------------------------------------------------------------------------
# Bullpen availability
# ---------------------------------------------------------------------------
# A pen is only worth what it can throw TODAY. A strong pen missing its two
# best arms can be worse than a middling pen with everyone on call, and the
# roster-level rankings never show that.
PEN_AVAIL_WEIGHT = {"UNAVAIL": 0.0, "TAXED": 0.0, "DOUBTFUL": 0.5}
# Fallback expected leverage when the FG gmLI is missing — the roles exist
# precisely to describe which innings a man gets.
PEN_ROLE_LEVERAGE = {"CL": 1.8, "SU": 1.3, "MID": 1.0, "LG": 0.6}


# Leverage belongs to the SLOT, not the man: the 9th of a one-run game is
# high leverage whoever throws it. Arms fill these slots in the manager's
# preference order, so losing the closer shifts everyone up a rung and losing
# the mop-up man changes almost nothing.
PEN_LEVERAGE_LADDER = (1.8, 1.4, 1.15, 0.95, 0.8, 0.65)


def pen_strength(rows: List[dict]) -> Optional[dict]:
    """What a bullpen is worth on paper vs what it can field today.

    Quality is SIERA (lower is better; the best-persisting run estimator in
    the 2024-26 check). Arms are ranked in the order a manager would reach
    for them — gmLI where the FG board has it, else the inferred role — and
    poured into a FIXED leverage ladder.

    An earlier version weighted each arm by his own gmLI and renormalised
    over whoever was left. That scored a pen BETTER when arms went down
    (ATH -0.31 with three out), because dropping a low-leverage bad arm
    hands his weight to the good ones. The ladder fixes it: quality is
    measured at the slots the innings actually come from.

    Returns None when too little of the pen has a usable SIERA."""
    used = []
    for rec in rows:
        fg = rec.get("fg") or {}
        siera = fg.get("siera")
        if not isinstance(siera, (int, float)):
            continue
        # Rotation arms land on the bullpen-usage page after any relief
        # outing (Senga, Bello, Houser all showed up as "key arms out"),
        # and a pen metric that scores the rotation isn't a pen metric.
        gs, g = fg.get("gs"), fg.get("g")
        if (isinstance(gs, (int, float)) and isinstance(g, (int, float))
                and g and gs / g >= 0.5):
            continue
        lev = fg.get("gmli")
        if not isinstance(lev, (int, float)) or lev <= 0:
            lev = PEN_ROLE_LEVERAGE.get(rec.get("role"), 1.0)
        used.append({"rec": rec, "siera": siera, "lev": float(lev),
                     "stuff": fg.get("stuff"),
                     "w": PEN_AVAIL_WEIGHT.get(rec.get("status"), 1.0)})
    if len(used) < 3:
        return None

    def ladder(arms, key="siera"):
        """Weighted quality of the top arms poured into the fixed ladder."""
        vals = [(a[key], w) for a, w in zip(arms, PEN_LEVERAGE_LADDER)
                if isinstance(a.get(key), (int, float))]
        tot = sum(w for _v, w in vals)
        return (sum(v * w for v, w in vals) / tot) if tot > 0 else None

    # paper order: best leverage first. today's order: fully-available arms
    # first, doubtful ones behind them (a manager reaches for them last),
    # unavailable dropped entirely.
    by_lev = sorted(used, key=lambda a: -a["lev"])
    today = sorted([a for a in by_lev if a["w"] > 0],
                   key=lambda a: (a["w"] < 1.0, -a["lev"]))
    if len(today) < 3:
        return None
    full, avail = ladder(by_lev), ladder(today)
    # only losses that would have been IN the ladder actually cost anything
    top_ids = {id(a) for a in by_lev[:len(PEN_LEVERAGE_LADDER)]}
    out = [a for a in by_lev if a["w"] <= 0]
    return {
        "full": full, "avail": avail,
        # positive = the pen is WORSE today than on paper
        "delta": (avail - full) if (avail is not None and full is not None)
                 else None,
        "stuff_full": ladder(by_lev, "stuff"),
        "stuff_avail": ladder(today, "stuff"),
        "n": len(used), "n_out": len(out),
        "out": [a["rec"].get("name", "?") for a in out],
        # the ones that matter — a lost mop-up man is not a story
        "out_key": [a["rec"].get("name", "?") for a in out
                    if id(a) in top_ids],
    }


# StatsAPI `fields=` whitelist for the season player list — see the coupling
# note on PBP_FIELDS below; the same whitelist semantics (and the same silent
# failure mode) apply. Keep in step with `_build_roster_maps`.
ROSTER_FIELDS = ",".join((
    "people", "id", "fullName", "currentTeam",
    "primaryPosition", "abbreviation", "batSide", "pitchHand", "code",
))


# StatsAPI `fields=` whitelist for the SEASON schedule walks that drive the
# manager board. Those callers keep only (gamePk, home id, away id) for games
# that are Final; the untrimmed season is 3.2MB of venue/broadcast/records
# metadata against 0.30MB here. NOT used for the TODAY schedule
# (`_get_schedule`), which is deliberately hydrated with probable pitchers,
# lineups and officials that half the window reads.
SEASON_SCHED_FIELDS = ",".join((
    "dates", "games", "gamePk",
    "status", "abstractGameState",
    "teams", "home", "away", "team", "id",
))


def _final_games(sched: Optional[dict]) -> List[tuple]:
    """[(gamePk, home id, away id)] for the FINAL games of a season schedule,
    DEDUPED by gamePk.

    Only finals: an in-progress game's play-by-play is still growing, and the
    checkpoint would freeze a half-played game forever.

    !! The dedupe is load-bearing !!  The season schedule lists ~22 gamePks
    TWICE — a suspended game appears under both the date it started and the
    date it resumed. Left in, both copies clear the `already seen` check
    before either fetch completes, so those games are downloaded twice AND
    folded into the league tables twice. That is a non-uniform double count,
    unlike the old per-club pass whose double-count was uniform and cancelled
    out of every mean. It also inflated the progress readout to 1,777 games
    against a real 1,755."""
    out = []
    seen = set()
    for d in (sched or {}).get("dates") or []:
        for g in d.get("games") or []:
            if (g.get("status") or {}).get("abstractGameState") != "Final":
                continue
            try:
                pk = g["gamePk"]
                if pk in seen:
                    continue
                seen.add(pk)
                out.append((pk,
                            g["teams"]["home"]["team"]["id"],
                            g["teams"]["away"]["team"]["id"]))
            except KeyError:
                continue
    return out


# StatsAPI `fields=` whitelist for the season-long officials schedule that
# builds {gamePk: home-plate umpire}.
UMPIRE_SCHED_FIELDS = ",".join((
    "dates", "games", "gamePk",
    "officials", "officialType", "official", "fullName",
))


# StatsAPI `fields=` whitelist for the playByPlay feed.
#
# The full document is ~0.58MB per game and the league walk covers ~1,650 of
# them, so a cold Managers start downloaded the better part of a gigabyte and
# spent ~9s of the qasync loop thread in json.loads alone (that parse holds
# the GIL, so it CANNOT be offloaded to an executor — it has to shrink).
# Every key below is one the analysis functions in this section actually
# read; the trimmed document is ~0.115MB, ~79% smaller and ~75% faster to
# parse, and was verified to produce byte-identical output from all nine
# consumers across 52 games (including the rare ABS-challenge K/BB flips,
# inherited-runner scoring, IBBs, sac bunts and extra innings).
#
# !! COUPLING !!  `fields` is a whitelist — anything not named here simply
# ISN'T IN THE RESPONSE, and the consumers all use .get() with defaults, so a
# missing key reads as "this never happened" rather than raising. If you make
# any function below read a NEW key, add it here in the SAME edit or the
# feature will silently compute zeros. Ancestor keys must be listed too
# (`details` as well as `code`), and the filter applies at every depth, so a
# generic name like `id` or `type` covers all of its nestings.
PBP_FIELDS = ",".join((
    "allPlays",
    "about", "inning", "isTopInning",
    "matchup", "pitcher", "batter",
    "postOnFirst", "postOnSecond", "postOnThird", "id",
    "result", "homeScore", "awayScore", "eventType",
    "count", "outs", "balls", "strikes",
    "playEvents", "isPitch", "type", "details", "description", "code",
    "reviewDetails", "reviewType", "challengeTeamId", "isOverturned",
    "player",
    "runners", "movement", "end", "runner",
))


def pitching_stints(pbp: dict, home_id: int, away_id: int) -> List[dict]:
    """Rebuild every pitching stint in a game from StatsAPI play-by-play.

    Walks the plays in order tracking each side's current pitcher; a change
    starts a new stint, stamped with the state it inherited (inning, outs
    already recorded, run differential from the PITCHING team's view).
    `order` 1 is the starter. Returns one record per stint, both teams."""
    cur: Dict[str, dict] = {}
    order: Dict[str, int] = {"home": 0, "away": 0}
    prev_outs = {"home": 0, "away": 0}
    prev_inning: Dict[str, Optional[int]] = {"home": None, "away": None}
    # runners on base as the previous play ended, and which stint is on the
    # hook for each of them (inherited-runner accounting)
    prev_runners: Dict[str, List[int]] = {"home": [], "away": []}
    prev_base: Dict[str, int] = {"home": 0, "away": 0}
    pending: Dict[str, Dict[int, dict]] = {"home": {}, "away": {}}
    out: List[dict] = []
    for p in (pbp.get("allPlays") or []):
        about = p.get("about") or {}
        pid = ((p.get("matchup") or {}).get("pitcher") or {}).get("id")
        if pid is None:
            continue
        # isTopInning -> away team batting -> HOME team is on the mound
        side = "home" if about.get("isTopInning") else "away"
        inning = about.get("inning")
        if prev_inning[side] != inning:
            prev_outs[side] = 0
            prev_inning[side] = inning
            prev_runners[side] = []
            prev_base[side] = 0
            pending[side] = {}
        res = p.get("result") or {}
        us = res.get("homeScore") if side == "home" else res.get("awayScore")
        them = res.get("awayScore") if side == "home" else res.get("homeScore")

        st = cur.get(side)
        if st is None or st["pid"] != pid:
            if st is not None:
                out.append(st)
            order[side] += 1
            st = {
                "team": home_id if side == "home" else away_id,
                "pid": pid, "inning": inning, "outs_in": prev_outs[side],
                "order": order[side],
                # entered with the inning already under way
                "mid_inning": prev_outs[side] > 0,
                "score_diff": (us - them) if (us is not None
                                              and them is not None) else None,
                "pitches": 0, "bf": 0, "outs_rec": 0,
                # runners already on when he arrived, and how many of THOSE
                # men came round to score (charged to the pitcher who left
                # them, not to him — see MLB's inherited/bequeathed pair)
                "inh": 0, "inh_scored": 0, "bequeathed": 0,
                # state he walked into, for the Leverage Index lookup
                "base_in": prev_base[side],
                "is_top": about.get("isTopInning"),
            }
            cur[side] = st
            if st["mid_inning"] and prev_runners[side]:
                st["inh"] = len(prev_runners[side])
                if out:
                    out[-1]["bequeathed"] += len(prev_runners[side])
                for rid in prev_runners[side]:
                    pending[side][rid] = st
        st["bf"] += 1
        st["pitches"] += sum(1 for e in (p.get("playEvents") or [])
                             if e.get("isPitch"))
        # did any runner someone else left on base come home on this play?
        for r in (p.get("runners") or []):
            mv = r.get("movement") or {}
            rid = ((r.get("details") or {}).get("runner") or {}).get("id")
            if mv.get("end") == "score" and rid in pending[side]:
                pending[side].pop(rid)["inh_scored"] += 1
        end_outs = (p.get("count") or {}).get("outs")
        if isinstance(end_outs, int):
            st["outs_rec"] += max(0, end_outs - prev_outs[side])
            prev_outs[side] = end_outs
        mu = p.get("matchup") or {}
        prev_runners[side] = [
            (mu.get(k) or {}).get("id")
            for k in ("postOnFirst", "postOnSecond", "postOnThird")
            if (mu.get(k) or {}).get("id")]
        prev_base[side] = _base_code(mu)
    out.extend(cur.values())
    return out


def _base_code(mu: dict) -> int:
    """3-bit occupancy of the bases AFTER a play (1B=1, 2B=2, 3B=4)."""
    return ((1 if mu.get("postOnFirst") else 0)
            | (2 if mu.get("postOnSecond") else 0)
            | (4 if mu.get("postOnThird") else 0))


def walk_half_innings(pbp: dict):
    """Yield (play, base_before, outs_before, runs_on_play, is_top) for every
    play, plus the runs scored from that play to the end of the half-inning.

    Base state before a play is the post-state of the play before it (empty
    to start the half), and outs before is the previous play's end count.
    Only half-innings that finished with three outs are yielded — a walk-off
    or a called game would drag the run-expectancy estimate down."""
    halves: Dict[tuple, List[dict]] = {}
    for p in (pbp.get("allPlays") or []):
        a = p.get("about") or {}
        halves.setdefault((a.get("inning"), a.get("isTopInning")),
                          []).append(p)
    for (inning, is_top), plays in halves.items():
        if not plays:
            continue
        if ((plays[-1].get("count") or {}).get("outs")) != 3:
            continue
        base, outs = 0, 0
        rows = []
        prev_score = None
        for p in plays:
            res = p.get("result") or {}
            score = ((res.get("awayScore") if is_top
                      else res.get("homeScore")) or 0)
            if prev_score is None:
                # runs before the first play of the half are unknown, so the
                # first play's runs are measured from its own end-score back
                prev_score = score - 0
                runs = 0
            else:
                runs = max(0, score - prev_score)
            rows.append([p, base, outs, runs])
            prev_score = score
            base = _base_code(p.get("matchup") or {})
            end_outs = (p.get("count") or {}).get("outs")
            if isinstance(end_outs, int):
                outs = end_outs
        # runs from each play to the end of the half
        tail = 0
        for r in reversed(rows):
            tail += r[3]
            r.append(tail)
        for p, b, o, runs, rest in rows:
            if o < 3:
                yield p, b, o, runs, is_top, rest


# Terminal states. A game that has ended has a win expectancy of exactly
# 1.0 or 0.0 — it is not a state to be estimated from samples, and routing
# the final play into a notional next half-inning instead is what made the
# decisive swings read LOW (9th-tied-with-a-runner-on scored under bases
# empty, because the "after" state it transitioned to was another live
# state near 0.5 rather than a settled result).
TERM_HOME_WIN = ("TERM", True)
TERM_AWAY_WIN = ("TERM", False)
TERMINAL_WE = {TERM_HOME_WIN: 1.0, TERM_AWAY_WIN: 0.0}


def we_key(inning, is_top, lead, base, outs) -> tuple:
    """Win-expectancy state, deliberately COARSE.

    A single season is not enough data for the full
    inning x half x lead x 8-base-codes x outs grid: that left ~3 transitions
    per state pair, the win rates were noise, and the Leverage Index came out
    nonsense (a tied 9th scored 0.31 against a league average of 1.00).
    Collapsing the eight base codes to a runner COUNT and clipping the lead
    to +-4 cuts the space ~3x and makes the cells estimable. Published LI
    tables are built from decades of games or a Markov model — with one
    season, coarse and correct beats fine and wrong."""
    n_on = bin(int(base)).count("1")
    # extras get their own bucket rather than folding into the 9th: they are
    # the highest-leverage innings in baseball and lumping them in inflated
    # every 9th-inning cell (it put the top LI at ~16 against a real ceiling
    # near 10). One bucket, not one per inning — the 13th is rare.
    return (min(int(inning or 1), 10), bool(is_top),
            max(-4, min(4, int(lead))), n_on, int(outs))


def we_from_pbp(pbp: dict, acc: Dict[tuple, List[float]],
                trans: Dict[tuple, int]):
    """Accumulate win-expectancy samples and state-to-state transitions.

    `acc[state] = [home wins, n]` over every state the game passed through;
    `trans[(before, after)] += 1` feeds the Leverage Index later — LI is the
    average |win-probability swing| a plate appearance produces from a state,
    so it needs the transition distribution, not just the win rate.

    A play that records the third out moves to the top of the next half with
    the bases clear, which is where much of the real leverage sits, so those
    transitions are followed rather than dropped."""
    plays = pbp.get("allPlays") or []
    if not plays:
        return
    last = (plays[-1].get("result") or {})
    fh, fa = last.get("homeScore"), last.get("awayScore")
    if fh is None or fa is None or fh == fa:
        return                       # tie/suspended: no outcome to learn from
    home_won = 1.0 if fh > fa else 0.0

    # NOT walk_half_innings: that keeps only halves ending in three outs,
    # which is right for run expectancy but throws away every walk-off — i.e.
    # exactly the highest-leverage state in baseball. Walk the plays raw.
    seq = []
    base, outs = 0, 0
    prev_h, prev_a = 0, 0
    cur_half = None
    last_i = len(plays) - 1
    for i, p in enumerate(plays):
        about = p.get("about") or {}
        is_top = about.get("isTopInning")
        half = (about.get("inning"), is_top)
        if half != cur_half:
            cur_half = half
            base, outs = 0, 0
        res = p.get("result") or {}
        h, a = res.get("homeScore"), res.get("awayScore")
        if h is None or a is None:
            continue
        inning = about.get("inning")
        # the score carried in from the previous play is the state before
        kb = we_key(inning, is_top, prev_h - prev_a, base, outs)
        end_outs = (p.get("count") or {}).get("outs")
        mu = p.get("matchup") or {}
        if i == last_i:
            # the game is over: the only honest "after" state is the result
            ka = TERM_HOME_WIN if home_won else TERM_AWAY_WIN
        elif isinstance(end_outs, int) and end_outs >= 3:
            nxt_inning = inning if is_top else (inning or 1) + 1
            ka = we_key(nxt_inning, not is_top, h - a, 0, 0)
        else:
            ka = we_key(inning, is_top, h - a, _base_code(mu),
                        end_outs if isinstance(end_outs, int) else outs)
        seq.append((kb, ka))
        prev_h, prev_a = h, a
        base = _base_code(mu)
        outs = end_outs if isinstance(end_outs, int) else outs
    for kb, ka in seq:
        cell = acc.setdefault(kb, [0.0, 0])
        cell[0] += home_won
        cell[1] += 1
        trans[(kb, ka)] = trans.get((kb, ka), 0) + 1


def _we_features(state: tuple) -> List[float]:
    """State -> logistic design row.

    A lead is worth more the fewer half-innings are left to erase it, so
    `lead` has to interact with time remaining; runners and outs matter only
    through the runs they are likely to produce, which is also time-scaled."""
    inn, is_top, lead, non, outs = state
    half_left = max(0.0, (9 - inn) * 2 + (1 if is_top else 0))
    urgency = 1.0 / (1.0 + half_left)          # -> 1 at the very end
    return [lead, lead * urgency, lead * urgency ** 2, urgency,
            half_left, non, outs, 1.0 if is_top else 0.0,
            non * urgency, outs * urgency, lead * lead]


def smooth_win_expectancy(acc: Dict[tuple, List[float]],
                          states, min_cell: int = 5) -> Dict[tuple, float]:
    """Fit WE as a smooth function of the game state and evaluate it.

    Differencing two SAMPLED cell means — which is what this used to do —
    carries twice a cell's sampling noise, and that noise is largest exactly
    where the cell is thin. It put a 1st-inning and a 4th-inning state at
    LI ~4 on a 500-game sample. Fitting the surface removes the noise
    instead of averaging it: afterwards the eight highest-leverage states
    are all 9th-inning one-run games, and the occurrence-weighted LI
    distribution lands on the published marks (24.5% of PAs over 1.5 vs a
    real ~24%, 5.6% over 3.0 vs ~5%, 0.99% over 5.0 vs ~1%).

    KNOWN LIMIT — the extreme tail still overshoots: the top state (bottom
    9th, down one, two on, two out) prices at ~17.6 against a real ceiling
    near 10. Smoothing removes WITHIN-cell variation from ordinary
    transitions while game-ending ones keep their full 0-to-1 magnitude, and
    the state key counts runners rather than coding the bases, so a single
    with men on first and third swings the same as with men on first and
    second. Use LI ordinally (ranking deployment) — do not read the top
    values as calibrated.

    Returns {} if sklearn is unavailable, which sends callers back to the
    raw empirical table rather than failing."""
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except Exception:
        return {}
    X, wins, losses = [], [], []
    for k, (w, n) in acc.items():
        if k in TERMINAL_WE or n < min_cell:
            continue
        X.append(_we_features(k))
        wins.append(w)
        losses.append(n - w)
    if len(X) < 50:
        return {}
    try:
        X = np.asarray(X, float)
        # one +ve and one -ve row per cell, weighted by wins / losses
        Xe = np.vstack([X, X])
        ye = np.concatenate([np.ones(len(X)), np.zeros(len(X))])
        sw = np.concatenate([np.asarray(wins, float),
                             np.asarray(losses, float)])
        keep = sw > 0
        model = LogisticRegression(max_iter=2000)
        model.fit(Xe[keep], ye[keep], sample_weight=sw[keep])
        live = [s for s in states if s not in TERMINAL_WE]
        if not live:
            return {}
        pred = model.predict_proba(
            np.asarray([_we_features(s) for s in live], float))[:, 1]
        out = dict(zip(live, (float(p) for p in pred)))
    except Exception as e:
        print(f"EffortMLB: WE smoothing failed ({e}); using raw cells")
        return {}
    out.update(TERMINAL_WE)
    return out


def build_leverage(acc: Dict[tuple, List[float]], trans: Dict[tuple, int],
                   min_n: int = 30) -> Dict[tuple, float]:
    """Leverage Index per state: the mean |win-probability swing| a plate
    appearance produces there, scaled so the league average is 1.0 —
    Tango's definition, estimated from this season's own games."""
    we = smooth_win_expectancy(
        acc, {k for pair in trans for k in pair})
    if not we:                       # sklearn missing: raw cells, noisier
        we = {k: v[0] / v[1] for k, v in acc.items() if v[1] >= min_n}
    # terminal states are definitional, not sampled — a decided game is
    # 1.0/0.0 no matter how few of them landed in this particular cell
    we.update(TERMINAL_WE)
    swing: Dict[tuple, List[float]] = {}
    for (kb, ka), n in trans.items():
        a, b = we.get(kb), we.get(ka)
        if a is None or b is None:
            continue
        s = swing.setdefault(kb, [0.0, 0.0])
        s[0] += abs(b - a) * n
        s[1] += n
    means = {k: v[0] / v[1] for k, v in swing.items() if v[1] >= min_n}
    if not means:
        return {}
    total_w = sum(swing[k][1] for k in means)
    league = sum(means[k] * swing[k][1] for k in means) / total_w
    if league <= 0:
        return {}
    return {k: v / league for k, v in means.items()}


def re24_from_pbp(pbp: dict, acc: Dict[tuple, List[float]]):
    """Accumulate [sum, n] of runs-to-end-of-inning per (bases, outs)."""
    for _p, base, outs, _runs, _top, rest in walk_half_innings(pbp):
        cell = acc.setdefault((base, outs), [0.0, 0])
        cell[0] += rest
        cell[1] += 1


def tto_from_pbp(pbp: dict, acc: Dict[int, Dict[tuple, List[float]]]):
    """Bucket every STARTER plate appearance by times-through-the-order and
    bank its state transition, so the TTO penalty can be priced in runs off
    our own RE24 rather than imported from a paper.

    A lineup turns over every nine batters, so batters 1-9 are the first
    look, 10-18 the second, 19+ the third."""
    seen: Dict[bool, dict] = {}
    count: Dict[bool, int] = {}
    for p, base, outs, runs, is_top, _rest in walk_half_innings(pbp):
        pid = ((p.get("matchup") or {}).get("pitcher") or {}).get("id")
        if pid is None:
            continue
        side = not is_top          # True when HOME team pitches
        if side not in seen:
            seen[side] = pid
            count[side] = 0
        if seen[side] != pid:
            continue               # starter has been removed
        count[side] += 1
        bucket = min(3, (count[side] - 1) // 9 + 1)
        end_outs = (p.get("count") or {}).get("outs")
        o1 = end_outs if isinstance(end_outs, int) else outs
        key = (base, outs, _base_code(p.get("matchup") or {}), o1)
        cell = acc.setdefault(bucket, {}).setdefault(key, [0.0, 0.0])
        cell[0] += runs
        cell[1] += 1


def tto_penalty(acc: Dict[int, Dict[tuple, List[float]]],
                re24: Dict[tuple, float]) -> Dict[int, float]:
    """Run value allowed per batter faced, by times through the order."""
    out = {}
    for bucket, states in acc.items():
        tot_rv, tot_n = 0.0, 0.0
        for (b0, o0, b1, o1), (runs, n) in states.items():
            before = re24.get((b0, o0))
            if before is None:
                continue
            after = 0.0 if o1 >= 3 else re24.get((b1, o1))
            if after is None:
                continue
            tot_rv += runs + (after - before) * n
            tot_n += n
        if tot_n >= 500:
            out[bucket] = tot_rv / tot_n
    return out


def rv_events_from_pbp(pbp: dict, home_id: int, away_id: int
                       ) -> Dict[int, List[dict]]:
    """State transitions for the decisions we can price with RE24.

    Sac bunts and intentional walks are at-bat RESULTS, so the real before /
    after states are both observable and the run value is measured, not
    modelled. (Steals are handled separately — they fire mid-at-bat, where
    the play's post-state doesn't isolate them.)"""
    out: Dict[int, List[dict]] = {home_id: [], away_id: []}
    for p, base, outs, runs, is_top, _rest in walk_half_innings(pbp):
        ev = (p.get("result") or {}).get("eventType") or ""
        if ev.startswith("sac_bunt"):
            kind = "bunt"
            team = away_id if is_top else home_id      # batting side
        elif ev == "intent_walk":
            kind = "ibb"
            team = home_id if is_top else away_id      # pitching side
        else:
            continue
        end_outs = (p.get("count") or {}).get("outs")
        out[team].append({
            "kind": kind, "b0": base, "o0": outs,
            "b1": _base_code(p.get("matchup") or {}),
            "o1": end_outs if isinstance(end_outs, int) else outs,
            "runs": runs,
        })
    return out


def decision_counts(pbp: dict, home_id: int, away_id: int) -> Dict[int, dict]:
    """Per-team managerial decision tallies for one game.

    Steals and sac bunts belong to the BATTING side; intentional walks and
    mound visits to the side on the mound. Steals land in two different
    places depending on when they happen — as a play RESULT when the runner
    is thrown out to end the at-bat, and as a mid-at-bat ACTION otherwise —
    so both are counted (they're disjoint, never the same event twice)."""
    out = {home_id: defaultdict(float), away_id: defaultdict(float)}
    for p in (pbp.get("allPlays") or []):
        about = p.get("about") or {}
        top = about.get("isTopInning")
        bat = away_id if top else home_id
        pit = home_id if top else away_id
        ev = (p.get("result") or {}).get("eventType") or ""
        if ev.startswith("sac_bunt"):
            out[bat]["sac_bunt"] += 1
        elif ev.startswith("stolen_base"):
            out[bat]["sb_att"] += 1
            out[bat]["sb_ok"] += 1
            out[bat]["sb_" + ev.rsplit("_", 1)[-1]] += 1
        elif ev.startswith("caught_stealing"):
            out[bat]["sb_att"] += 1
            out[bat]["cs_" + ev.rsplit("_", 1)[-1]] += 1
        elif ev == "intent_walk":
            out[pit]["ibb"] += 1
        for e in (p.get("playEvents") or []):
            if e.get("type") != "action":
                continue
            d = e.get("details") or {}
            aev = d.get("eventType") or ""
            if aev.startswith("stolen_base"):
                out[bat]["sb_att"] += 1
                out[bat]["sb_ok"] += 1
                out[bat]["sb_" + aev.rsplit("_", 1)[-1]] += 1
            elif aev.startswith("caught_stealing"):
                out[bat]["sb_att"] += 1
                out[bat]["cs_" + aev.rsplit("_", 1)[-1]] += 1
            elif aev == "mound_visit":
                out[pit]["mound_visit"] += 1
            elif aev == "intent_walk":
                out[pit]["ibb"] += 1
            elif aev == "offensive_substitution":
                # the same action covers pinch-runners; only the description
                # distinguishes them
                if "pinch-hitter" in (d.get("description") or "").lower():
                    out[bat]["pinch_hit"] += 1
            elif aev in ("defensive_substitution", "defensive_switch"):
                out[pit]["def_move"] += 1
    for tid in out:
        out[tid]["games"] = 1
    return out


# --- defence vs the contact a starter actually allows -----------------------
# Savant's spray pixel grid: home plate at (125.42, 198.27), +x toward RF,
# +y toward CF (the same anchor the HR-widget spray chart uses). Angle 0 is
# dead centre, -45 the LF line, +45 the RF line.
_SPRAY_HP = (125.42, 198.27)
# feet per grid pixel. The replay's hitData conversion and the pitch-detail
# spray chart below both key off this; reconstructing a feed-stated 397 ft
# home run through it lands at 398.2 ft.
_SPRAY_SCALE = 2.51
# Six 15-degree sectors from the LF line (-45) to the RF line (+45). Finer
# than thirds because the fielder AND the direction he has to move both change
# inside a third — the 3B hole and the line are the same "left" third but they
# are different plays for different gloves.
_SECTORS = ("LL", "L3", "LM", "RM", "R1", "RR")


def _sector(ang: float) -> str:
    return _SECTORS[min(5, max(0, int((ang + 45.0) // 15.0)))]


# Ground balls: which glove, and WHICH WAY he has to move. This is the whole
# point of using the directional OAA splits — a shortstop's range into the
# 3B hole ("to3b") is a different skill from his range up the middle
# ("to1b"), and a sinkerballer who runs everything to the left side is buying
# one of them and not the other. Holes between two fielders list both.
_GB_ZONE = {
    "LL": (("3B", "to3b"),),
    "L3": (("3B", "to1b"), ("SS", "to3b")),      # the 5-6 hole
    "LM": (("SS", "to1b"),),
    "RM": (("2B", "to3b"),),
    "R1": (("2B", "to1b"), ("1B", "to3b")),      # the 3-4 hole
    "RR": (("1B", "to1b"),),
}
# Air balls: outfielder plus whether he is coming in or going back, which is
# what separates a shallow-fly pitcher from one who lives on the track.
_OF_BY_SECTOR = {"LL": "LF", "L3": "LF", "LM": "CF",
                 "RM": "CF", "R1": "RF", "RR": "RF"}
# Savant hit distance in feet; beyond this the outfielder is going back.
_DEEP_FT = 300.0
# League ground-ball share, used only to say whether THIS pitcher is ground or
# air heavy relative to the league when weighting the park read.
_LG_GB_RATE = 0.43


def _spray_angle(hc_x, hc_y) -> Optional[float]:
    if hc_x is None or hc_y is None:
        return None
    dx = hc_x - _SPRAY_HP[0]
    dy = _SPRAY_HP[1] - hc_y
    if dy <= 0:
        return None                      # behind the plate: not a ball in play
    ang = math.degrees(math.atan2(dx, dy))
    return ang if -45.0 <= ang <= 45.0 else None


def sp_contact_profile(rows: List[dict]) -> Optional[dict]:
    """Where the contact this pitcher allows actually goes.

    Built from the per-pitcher Savant detail already cached for the SP form
    panel, so it costs nothing new: `bb_type` gives ground vs air, `hc_x/hc_y`
    the spray angle, `hit_distance_sc` shallow vs deep, `stand` the hand.

    Zones are (gb, sector) and (air, sector, depth); shares are also kept
    split by batter hand, because the fielding boards carry vs-RHH and vs-LHH
    splits and a pitcher's platoon mix decides which of them applies."""
    zones: Dict[tuple, int] = defaultdict(int)
    by_hand: Dict[str, Dict[tuple, int]] = {"R": defaultdict(int),
                                            "L": defaultdict(int)}
    bb = defaultdict(int)
    hands = defaultdict(int)
    n = 0
    for r in rows:
        t = r.get("bb_type") or ""
        if not t:
            continue
        ang = _spray_angle(r.get("hc_x"), r.get("hc_y"))
        if ang is None:
            continue
        n += 1
        bb[t] += 1
        hand = r.get("stand") or "?"
        hands[hand] += 1
        sec = _sector(ang)
        if t == "ground_ball":
            z = ("gb", sec, "")
        else:
            d = r.get("hit_distance") or r.get("hit_distance_sc")
            depth = "deep" if (d is not None and d >= _DEEP_FT) else "shallow"
            z = ("air", sec, depth)
        zones[z] += 1
        if hand in by_hand:
            by_hand[hand][z] += 1
    if n < 50:                           # too little contact to characterise
        return None
    gb = bb.get("ground_ball", 0) / n
    return {
        "n_bip": n,
        "shares": {k: v / n for k, v in zones.items()},
        "by_hand": {h: {k: v / n for k, v in z.items()}
                    for h, z in by_hand.items()},
        "gb_rate": gb,
        "air_rate": 1.0 - gb,
        "gb_vs_league": gb - _LG_GB_RATE,
        "bb_type": {k: v / n for k, v in bb.items()},
        "vs_rhh": hands.get("R", 0) / n,
        "vs_lhh": hands.get("L", 0) / n,
        # pull/oppo is only meaningful once the batter's hand is known, so it
        # is expressed as share hit to the LEFT and RIGHT sides of the field
        "to_left": sum(v for k, v in zones.items()
                       if k[1] in ("LL", "L3")) / n,
        "to_right": sum(v for k, v in zones.items()
                        if k[1] in ("R1", "RR")) / n,
    }


def _zone_defense(zone: tuple, defense: Dict[str, dict],
                  hand_mix: tuple) -> Optional[float]:
    """Defensive value for one contact zone, using the DIRECTIONAL split that
    the play actually calls for and blended to the pitcher's platoon mix.

    The handed blend is written as a delta off the fielder's total rather than
    a ratio: OAA components go negative, so `rhh / (rhh + lhh)` is unstable
    and occasionally explodes. `(share - league) * component` stays sane."""
    kind, sec, depth = zone
    pR, pL = hand_mix
    parts = []
    if kind == "gb":
        for pos, direction in _GB_ZONE.get(sec, ()):
            d = defense.get(pos)
            if d and d.get(direction) is not None:
                parts.append((d, d[direction]))
    else:
        pos = _OF_BY_SECTOR.get(sec)
        d = defense.get(pos)
        if d:
            # in/behind for centre, lateral for the gaps and lines
            key = ("back" if depth == "deep" else "in") if sec in (
                "LM", "RM") else ("to3b" if sec in ("LL", "L3") else "to1b")
            if d.get(key) is not None:
                parts.append((d, d[key]))
    if not parts:
        return None
    vals = []
    for d, base in parts:
        adj = 0.0
        if d.get("rhh") is not None and d.get("lhh") is not None:
            adj = ((pR - _LG_RHH_SHARE) * d["rhh"]
                   + (pL - (1 - _LG_RHH_SHARE)) * d["lhh"])
        # arm + double plays, which OAA does not measure at all. Not
        # directional, so the same term rides every zone this glove covers.
        vals.append(base + adj + (d.get("aux") or 0.0))
    return sum(vals) / len(vals)


# Right-handed batters take roughly 57% of plate appearances league-wide; used
# only as the baseline the handed adjustment is measured against.
_LG_RHH_SHARE = 0.57


def defense_advantage(profile: dict, defense: Dict[str, dict],
                      lg_def: Optional[Dict[tuple, float]] = None,
                      park: Optional[dict] = None) -> Optional[dict]:
    """Given the contact he generates, is this pitcher better or worse off
    than he would be with a LEAGUE-AVERAGE defence behind him?

        adv = SUM over zones of  share_z * (defence_z - league_defence_z)

    This replaced an earlier "fit" metric that differenced out the defence's
    quality LEVEL and kept only "is his contact mix unusual" — which answers
    a question nobody asks, and backtested at exactly zero.

    VALIDATED (2026-08-01), 2,652 starts / 42,319 balls in play, defence taken
    from the actual boxscore lineup, outcome = actual wOBA-on-contact minus
    xwOBA-on-contact (which controls for the quality of contact he allowed and
    weights a double above a single):

        corr(adv, wOBA gap) = -0.039, 95% CI [-0.077, -0.001]
        worst quartile +0.0183 vs best quartile +0.0040  (z = 2.23)

    i.e. ~14 points of wOBA-on-contact, about 0.2 runs a start, between the
    extremes. Small, correctly signed, and the confidence interval excludes
    zero. Two honest caveats: raw defensive LEVEL scores nearly as well
    (-0.035), so most of this is "the defence is good" rather than anything
    clever about matching; and the same quantity measured against BABIP is
    flat at zero, so the outcome variable is doing much of the work.

    League zone defence is NOT zero — it runs -0.93 for grounders up the
    middle to +1.15 in the 5-6 hole — so the subtraction is load bearing."""
    if not profile or not defense or not lg_def:
        return None
    mix = (profile.get("vs_rhh") or 0.0, profile.get("vs_lhh") or 0.0)
    shares = profile["shares"]
    zvals: Dict[tuple, float] = {}
    for zone in shares:
        v = _zone_defense(zone, defense, mix)
        if v is not None:
            zvals[zone] = v
    if len(zvals) < 4:                   # too many holes to compare
        return None
    tot = sum(shares[z] for z in zvals)
    if tot <= 0:
        return None
    adv = sum(shares[z] * (v - lg_def.get(z, 0.0)) for z, v in zvals.items())
    priced = sum(1 for d in defense.values()
                 if d.get("oaa") is not None and not d.get("est"))
    estimated = sum(1 for d in defense.values() if d.get("est"))
    out = {
        "adv": adv / tot,
        "adv_raw": adv,
        # how much of the alignment actually carries a number: a starter who
        # clears no board drops his zones from the weighted average, so the
        # rank can be built on six gloves while looking like seven
        "priced": priced,
        "estimated": estimated,
        "gloves": len(defense),
        "covered": tot,
        # plain quality of the gloves, contact-weighted — reported alongside
        # because it carries most of the signal on its own
        "level": sum(shares[z] * v for z, v in zvals.items()) / tot,
        "n_zones": len(zvals),
        "zones": zvals,
    }
    if park:
        out["park"] = park_fit(profile, park)
    return out


def defense_rank(profile: dict, all_defense: Dict[str, Dict[str, dict]],
                 lg_def: Dict[tuple, float], team: str) -> Optional[dict]:
    """Where this club's gloves rank among all 30 FOR THIS PITCHER's contact.

    `adv` is in season-scale OAA units weighted by contact share, which is
    close to unreadable on a card — "-1.18" invites being read as runs, or as
    outs per start, and it is neither. Scoring the same pitcher behind all 30
    alignments turns it into an ordinal that means exactly what it says."""
    if not profile or not all_defense or not lg_def:
        return None
    scores = {}
    for abbr, dfn in all_defense.items():
        a = defense_advantage(profile, dfn, lg_def)
        if a:
            scores[abbr] = a["adv"]
    if len(scores) < 20 or team not in scores:
        return None
    order = sorted(scores, key=lambda k: -scores[k])
    rank = order.index(team) + 1
    return {"rank": rank, "of": len(order), "scores": scores,
            "pct": 1.0 - (rank - 1) / (len(order) - 1),
            "best": order[0], "worst": order[-1]}


def park_fit(profile: dict, park: dict) -> Optional[dict]:
    """What this venue does to the contact he allows.

    Park indices are 100-centred, so `index - 100` is the percentage swing.
    Weighted to the hands he actually faces (the handed split is large — Great
    American is 113 for right-handed bats and 123 for left-handed) and scaled
    by how air-heavy he is relative to the league, since a ground-ball pitcher
    barely rents the seats."""
    if not profile or not park:
        return None
    pR, pL = profile.get("vs_rhh") or 0.0, profile.get("vs_lhh") or 0.0
    out = {}
    for stat in ("HR", "2B", "3B", "wOBACon"):
        vals = []
        for hand, w in (("R", pR), ("L", pL)):
            idx = ((park.get(hand) or {}).get(stat)
                   or (park.get("All") or {}).get(stat))
            if idx is not None and w:
                vals.append(w * (idx - 100.0))
        if vals:
            out[stat] = sum(vals)
    # an air-heavy pitcher is more exposed to the park than a sinkerballer
    air_lean = (profile.get("air_rate") or 0.0) / (1.0 - _LG_GB_RATE)
    if "HR" in out:
        out["HR_exposed"] = out["HR"] * air_lean
    out["air_lean"] = air_lean
    return out


def abs_challenges(pbp: dict, home_id: int, away_id: int) -> Dict[int, dict]:
    """ABS (robot-zone) challenge tallies per team for one game — new in 2026.

    Rides the play-by-play feed the manager tendencies already download, so
    this costs no extra requests. Only `reviewType == "MJ"` on a PITCH is an
    ABS challenge; `MA` is an ordinary replay review of a batted-ball play.

    TWO conventions had to be established from the data rather than assumed:

    * `playEvents[].count` is the count AFTER the pitch (the first pitch of an
      at-bat called Ball already reads 1-0).
    * `details.description` is the call AFTER the review. A batter therefore
      appears to "challenge a Ball", which only makes sense once you see that
      the ball IS the corrected call — he erased a called strike.

    So the original call is the description when the challenge failed, and its
    opposite when it succeeded. That is what makes strikeout/walk flips
    computable: a corrected strike that makes strikes 3 is a strikeout the
    pitching side GAINED, and a corrected ball that makes balls 4 is a walk
    the batting side gained. The mirror cases — where the corrected call
    ERASED a strikeout or a walk that would otherwise have stood — are the
    ones that actually decide at-bats, so both are counted."""
    out: Dict[int, dict] = {home_id: defaultdict(float),
                            away_id: defaultdict(float)}
    for p in (pbp.get("allPlays") or []):
        about = p.get("about") or {}
        top = about.get("isTopInning")
        inning = about.get("inning") or 0
        mu = p.get("matchup") or {}
        batter_id = (mu.get("batter") or {}).get("id")
        pitcher_id = (mu.get("pitcher") or {}).get("id")
        res = p.get("result") or {}
        h_sc, a_sc = res.get("homeScore"), res.get("awayScore")
        for e in (p.get("playEvents") or []):
            rd = e.get("reviewDetails")
            if not rd or not e.get("isPitch"):
                continue
            if rd.get("reviewType") != "MJ":
                continue
            tid = rd.get("challengeTeamId")
            if tid not in out:
                continue
            d = out[tid]
            opp = home_id if tid == away_id else away_id
            ok = bool(rd.get("isOverturned"))
            d["n"] += 1
            d["ovr"] += ok
            out[opp]["n_against"] += 1
            out[opp]["ovr_against"] += ok

            # Who spent it. Only batter/pitcher/catcher may challenge, and the
            # first two are identifiable from the matchup; anyone else on the
            # challenging side is the catcher.
            pid = ((rd.get("player") or {}).get("id"))
            role = ("bat" if pid == batter_id else
                    "pit" if pid == pitcher_id else "cat")
            d[f"n_{role}"] += 1
            d[f"ovr_{role}"] += ok

            desc = (e.get("details") or {}).get("description") or ""
            final_strike = "Strike" in desc
            cnt = e.get("count") or {}
            balls, strikes = cnt.get("balls"), cnt.get("strikes")
            if ok and isinstance(balls, int) and isinstance(strikes, int):
                if final_strike:
                    # original was a ball; the correction is a strike
                    if strikes >= 3:
                        d["k_gained"] += 1        # punched him out on appeal
                    if balls + 1 >= 4:
                        d["bb_erased"] += 1       # wiped out ball four
                else:
                    if balls >= 4:
                        d["bb_gained"] += 1
                    if strikes + 1 >= 3:
                        d["k_erased"] += 1        # wiped out strike three
            # Spending discipline: a challenge in the 7th or later of a
            # one-run game is worth more than one in the 2nd.
            if inning >= 7 and h_sc is not None and a_sc is not None \
                    and abs(h_sc - a_sc) <= 2:
                d["late_close"] += 1
    for tid in out:
        out[tid]["games"] = 1
    return out


def summarize_abs(acc: Dict[str, float]) -> dict:
    """Season ABS rates from the accumulated per-game tallies."""
    n = acc.get("n") or 0
    g = acc.get("games") or 0
    na = acc.get("n_against") or 0
    out = {
        "abs_n": n,
        "abs_per_game": (n / g) if g else None,
        "abs_rate": (acc.get("ovr", 0) / n) if n else None,
        "abs_rate_against": (acc.get("ovr_against", 0) / na) if na else None,
        "abs_late_close": (acc.get("late_close", 0) / n) if n else None,
        # net at-bat-deciding calls swung this club's way
        "abs_net_flips": (acc.get("k_gained", 0) + acc.get("bb_gained", 0)
                          + acc.get("k_erased", 0) + acc.get("bb_erased", 0)),
    }
    for role in ("bat", "cat", "pit"):
        rn = acc.get(f"n_{role}") or 0
        out[f"abs_{role}_share"] = (rn / n) if n else None
        out[f"abs_{role}_rate"] = (acc.get(f"ovr_{role}", 0) / rn) if rn else None
    return out


def umpire_game_stats(pbp: dict, game_pk: int, acc: Dict[int, dict]):
    """Per-GAME called-pitch tallies, keyed by gamePk.

    Keyed by game rather than by umpire because the play-by-play feed does
    not name the officials — the schedule feed does, and the join happens in
    `build_umpire_profiles`. Rides the same download the manager tendencies
    already make, so it costs no extra requests.

    Only TAKEN pitches are counted. A swinging strike says nothing about the
    umpire's zone, and including swings would mostly measure the hitters."""
    if game_pk in acc:
        return                       # each game arrives once per club
    d = {"taken": 0, "csx": 0, "pitches": 0, "k": 0, "bb": 0, "pa": 0,
         "chal": 0, "ovr": 0}
    for p in (pbp.get("allPlays") or []):
        ev = (p.get("result") or {}).get("eventType") or ""
        d["pa"] += 1
        if ev == "strikeout":
            d["k"] += 1
        elif ev in ("walk", "intent_walk"):
            d["bb"] += 1
        for e in (p.get("playEvents") or []):
            if not e.get("isPitch"):
                continue
            d["pitches"] += 1
            rd = e.get("reviewDetails")
            if rd and rd.get("reviewType") == "MJ":
                d["chal"] += 1
                d["ovr"] += bool(rd.get("isOverturned"))
            code = ((e.get("details") or {}).get("code") or "")
            # B = ball, C = called strike; everything else involved a swing
            if code == "C":
                d["taken"] += 1
                d["csx"] += 1
            elif code == "B":
                d["taken"] += 1
    if d["pa"]:
        acc[game_pk] = d


def build_umpire_profiles(acc: Dict[int, dict],
                          ump_by_game: Dict[int, str],
                          min_games: int = 4) -> Dict[str, dict]:
    """Per-umpire zone profile, shrunk toward the league.

    Three numbers, in increasing order of how directly they measure the man:

    * `csr`  — called-strike rate on TAKEN pitches. Direct, but mixed with
      whichever clubs he happened to draw.
    * `k_pct` / `bb_pct` — the downstream effect, more confounded still.
    * `ovr_against` — the share of ABS challenges in his games that were
      OVERTURNED. This is the only one that is a measurement of the umpire
      rather than of the baseball around him: the challenge system tests his
      calls directly, and a challenge is only spent on a pitch a player
      believes was missed.

    An umpire works ~18 plate games a season, so every rate is regressed
    toward the league mean with a prior worth `PRIOR` events. Without it the
    leaderboard is sorted by sample size."""
    PRIOR_TAKEN, PRIOR_PA, PRIOR_CHAL = 2000, 800, 25
    tot = {k: 0 for k in ("taken", "csx", "k", "bb", "pa", "chal", "ovr")}
    by_ump: Dict[str, dict] = {}
    for gp, d in acc.items():
        name = ump_by_game.get(gp)
        for k in tot:
            tot[k] += d.get(k, 0)
        if not name:
            continue
        u = by_ump.setdefault(name, {k: 0 for k in tot} | {"games": 0})
        u["games"] += 1
        for k in tot:
            u[k] += d.get(k, 0)
    if not tot["taken"] or not tot["pa"]:
        return {}
    lg_csr = tot["csx"] / tot["taken"]
    lg_k = tot["k"] / tot["pa"]
    lg_bb = tot["bb"] / tot["pa"]
    lg_ovr = (tot["ovr"] / tot["chal"]) if tot["chal"] else None

    def shrink(num, den, prior_n, lg):
        return (num + prior_n * lg) / (den + prior_n) if lg is not None else None

    out = {}
    for name, u in by_ump.items():
        if u["games"] < min_games:
            continue
        csr = shrink(u["csx"], u["taken"], PRIOR_TAKEN, lg_csr)
        out[name] = {
            "games": u["games"],
            "csr": csr,
            "csr_d": csr - lg_csr,          # + = expands the zone
            "k_pct": shrink(u["k"], u["pa"], PRIOR_PA, lg_k),
            "k_d": shrink(u["k"], u["pa"], PRIOR_PA, lg_k) - lg_k,
            "bb_pct": shrink(u["bb"], u["pa"], PRIOR_PA, lg_bb),
            "bb_d": shrink(u["bb"], u["pa"], PRIOR_PA, lg_bb) - lg_bb,
            "chal": u["chal"],
            "ovr_against": (shrink(u["ovr"], u["chal"], PRIOR_CHAL, lg_ovr)
                            if lg_ovr is not None else None),
            "ovr_raw": (u["ovr"] / u["chal"]) if u["chal"] else None,
        }
    out["__league__"] = {"csr": lg_csr, "k_pct": lg_k, "bb_pct": lg_bb,
                         "ovr_against": lg_ovr, "games": len(acc)}
    return out


def score_rv_events(events: List[dict], re24: Dict[tuple, float]
                    ) -> Dict[str, dict]:
    """Price measured decisions with RE24.

    RV = (runs that scored on the play + run expectancy of the state it left)
    minus the run expectancy of the state it started from. An inning-ending
    play leaves an expectancy of zero. Positive is good for the BATTING team,
    so bunts are reported from the batting side and IBBs are flipped to the
    pitching side that chose to issue them."""
    out: Dict[str, dict] = {}
    for e in events:
        before = re24.get((e["b0"], e["o0"]))
        if before is None:
            continue
        after = 0.0 if e["o1"] >= 3 else re24.get((e["b1"], e["o1"]))
        if after is None:
            continue
        rv = e["runs"] + after - before
        if e["kind"] == "ibb":
            rv = -rv          # the pitching team wants run expectancy DOWN
        d = out.setdefault(e["kind"], {"n": 0, "rv": 0.0})
        d["n"] += 1
        d["rv"] += rv
    for d in out.values():
        d["per"] = d["rv"] / d["n"] if d["n"] else None
    return out


def steal_run_value(sb_by_base: Dict[str, int], cs_by_base: Dict[str, int],
                    re24: Dict[tuple, float]) -> Optional[dict]:
    """Net runs from the running game, modelled off the base-out transitions.

    Steals fire mid-at-bat where the play's post-state can't isolate them, so
    these are priced with the league RE24 transition rather than measured
    play by play: a steal of 2nd is (1B -> 2B) at the same outs, a caught
    stealing is (1B -> empty) with one more out. Averaged over the three
    out-states, which is the standard simplification."""
    def avg_delta(b_from, b_to, add_out):
        vals = []
        for o in range(3):
            a = re24.get((b_from, o))
            if a is None:
                continue
            if add_out:
                b = 0.0 if o + 1 >= 3 else re24.get((b_to, o + 1))
            else:
                b = re24.get((b_to, o))
            if b is None:
                continue
            vals.append(b - a)
        return sum(vals) / len(vals) if vals else None

    gain = {"2b": avg_delta(1, 2, False), "3b": avg_delta(2, 4, False)}
    loss = {"2b": avg_delta(1, 0, True), "3b": avg_delta(2, 0, True)}
    total, n = 0.0, 0
    for base in ("2b", "3b"):
        if gain[base] is not None:
            total += gain[base] * sb_by_base.get(base, 0)
            n += sb_by_base.get(base, 0)
        if loss[base] is not None:
            total += loss[base] * cs_by_base.get(base, 0)
            n += cs_by_base.get(base, 0)
    if not n:
        return None
    return {"n": n, "rv": total, "per": total / n,
            "sb2_rv": gain["2b"], "cs2_rv": loss["2b"]}


# --- openers ---------------------------------------------------------------
# An opener is a PLANNED short start, and the trap is that a starter who got
# shelled looks identical on innings alone. Measured over a 400-game league
# sample, the short-start population is cleanly bimodal: starts ending at 3
# outs run a median 19 pitches / 4 batters (6.3 pitches per out), while starts
# ending at 9 outs run 69 pitches / 17 batters. All three bounds are load
# bearing — the outs bound excludes ordinary short starts, the pitch bound the
# man who needed 50 pitches to get six outs, the batter bound the man who
# faced twelve. Together they flag ~6.7% of starts.
OPENER_MAX_OUTS = 6
OPENER_MAX_PITCHES = 40
OPENER_MAX_BF = 9
# Weighted mean of the openers in that sample: 4.15 outs. Used as the
# regression target for a pitcher who IS an opener — shrinking him toward the
# club's *starter* norm is what the old code did, and it is badly wrong.
OPENER_IP_BASELINE = 1.4


def is_opener(outs: Optional[int], pitches: Optional[int],
              bf: Optional[int]) -> bool:
    """Does this start look planned-short rather than blown-up?"""
    if outs is None or outs > OPENER_MAX_OUTS:
        return False
    if pitches is not None and pitches > OPENER_MAX_PITCHES:
        return False
    if bf is not None and bf > OPENER_MAX_BF:
        return False
    return True


def summarize_stints(stints: List[dict]) -> Optional[dict]:
    """Manager tendencies from one team's season of pitching stints."""
    sp = [s for s in stints if s["order"] == 1]
    rp = [s for s in stints if s["order"] > 1]
    if not sp:
        return None
    opener_ids = {id(s) for s in sp
                  if is_opener(s["outs_rec"], s["pitches"], s["bf"])}

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def frac(seq, pred):
        return (sum(1 for s in seq if pred(s)) / len(seq)) if seq else None

    sp_ip = [s["outs_rec"] / 3.0 for s in sp]
    avg_sp_ip = mean(sp_ip)
    trad = [s for s in sp if id(s) not in opener_ids]
    return {
        "games": len(sp),
        "sp_pitches": mean([s["pitches"] for s in sp]),
        # sp_ip keeps opener games IN: its consumer is relief_ip below, which
        # is about the innings the pen actually has to cover, and a bullpen
        # game is precisely when that is largest. sp_ip_trad answers the
        # different question of how deep a REAL start goes.
        "sp_ip": avg_sp_ip,
        "opener_rate": (len(opener_ids) / len(sp)) if sp else None,
        "sp_ip_trad": mean([s["outs_rec"] / 3.0 for s in trad]),
        "sp_short": frac(sp, lambda s: s["outs_rec"] < 15),
        # pulled with the inning unfinished — a quick-hook signature
        "sp_mid_pull": frac(sp, lambda s: s["outs_rec"] % 3 != 0),
        "pitchers_per_game": (len(stints) / len(sp)),
        "rp_per_game": (len(rp) / len(sp)),
        "rp_ip": mean([s["outs_rec"] / 3.0 for s in rp]),
        "rp_short_stint": frac(rp, lambda s: s["bf"] <= 3),
        "rp_multi_inning": frac(rp, lambda s: s["outs_rec"] >= 4),
        "rp_mid_inning": frac(rp, lambda s: s["mid_inning"]),
        "rp_close": frac(rp, lambda s: s["score_diff"] is not None
                         and abs(s["score_diff"]) <= 2),
        # A lineup turns over every 9 batters, so a starter is into his THIRD
        # time through from batter 19. The research consensus is that this is
        # the decision point that matters — roughly -0.35 R/9 for the third
        # look — more than the pitch count managers actually watch.
        "sp_tto3": frac(sp, lambda s: s["bf"] >= 19),
        "sp_bf": mean([s["bf"] for s in sp]),
        # full distribution, not just the mean — the hook CURVE is the story
        # (a mean of 5.2 IP hides whether he hooks sharply or reluctantly)
        "sp_bf_list": [s["bf"] for s in sp],
        # Inherited runners: how often this manager hands his relievers a
        # mess, and how often the mess scores. Low bequeathed totals are a
        # philosophy signal — some managers simply won't change mid-inning.
        "ir": sum(s["inh"] for s in rp),
        "ir_scored": sum(s["inh_scored"] for s in rp),
        "ir_strand": (1.0 - sum(s["inh_scored"] for s in rp)
                      / sum(s["inh"] for s in rp)) if sum(
                          s["inh"] for s in rp) else None,
        "bq_per_start": (sum(s["bequeathed"] for s in sp) / len(sp)
                         if sp else None),
        # the number that matters for the pen: innings it has to cover
        "relief_ip": (9.0 - avg_sp_ip) if avg_sp_ip is not None else None,
        "by_pitcher": _stints_by_pitcher(rp),
    }


def _tendencies_from_acc(a: Optional[dict]) -> Optional[dict]:
    """One club's cached manager-tendency dict from its season accumulators.

    Shared by the league-wide prefetch and the per-club method so the two
    cannot drift — they write the SAME cache key, and a difference between
    them would surface as a club whose numbers change depending on which
    path happened to build it.

    !! COUPLING !! Adding a key here means bumping `mgr_tend_v5_` at both
    call sites, or older cache entries deserialise into the new shape with
    the new feature silently blank."""
    if not a:
        return None
    data = summarize_stints(a["stints"])
    if not data:
        return None
    dec = a["dec"]
    if dec.get("games"):
        g = dec["games"]
        data["decisions"] = {
            "games": int(g),
            "sac_bunt": dec["sac_bunt"] / g,
            "sb_att": dec["sb_att"] / g,
            "sb_rate": (dec["sb_ok"] / dec["sb_att"]
                        if dec["sb_att"] else None),
            "pinch_hit": dec["pinch_hit"] / g,
            "ibb": dec["ibb"] / g,
            "mound_visit": dec["mound_visit"] / g,
            "def_move": dec["def_move"] / g,
        }
        data["rv_events"] = a["rv"]
        data["sb_by_base"] = {b: dec.get(f"sb_{b}", 0)
                              for b in ("2b", "3b", "home")}
        data["cs_by_base"] = {b: dec.get(f"cs_{b}", 0)
                              for b in ("2b", "3b", "home")}
    if a["abs"].get("n"):
        data["abs"] = summarize_abs(a["abs"])
    return data


def _stints_by_pitcher(rp: List[dict]) -> Dict[int, dict]:
    """Per-reliever deployment: the game states he actually gets brought
    into. A closer used only with a lead of 1-3 is a very different asset
    from one his manager will spend in a tie."""
    by: Dict[int, List[dict]] = {}
    for s in rp:
        by.setdefault(s["pid"], []).append(s)
    out = {}
    for pid, ss in by.items():
        n = len(ss)
        diffs = [s["score_diff"] for s in ss if s["score_diff"] is not None]
        if not diffs:
            continue
        out[pid] = {
            "n": n,
            "save_spot": sum(1 for d in diffs if 1 <= d <= 3) / len(diffs),
            "big_lead": sum(1 for d in diffs if d > 3) / len(diffs),
            "tie": sum(1 for d in diffs if d == 0) / len(diffs),
            "trail": sum(1 for d in diffs if d < 0) / len(diffs),
            "mid_inning": sum(1 for s in ss if s["mid_inning"]) / n,
            # late and close — the spots that decide games
            "high_lev": sum(1 for s in ss
                            if (s["inning"] or 0) >= 7
                            and s["score_diff"] is not None
                            and abs(s["score_diff"]) <= 2) / n,
            "avg_inning": sum(s["inning"] for s in ss
                              if s["inning"]) / max(1, sum(
                                  1 for s in ss if s["inning"])),
            "avg_bf": sum(s["bf"] for s in ss) / n,
            "ir": sum(s["inh"] for s in ss),
            "ir_strand": (1.0 - sum(s["inh_scored"] for s in ss)
                          / sum(s["inh"] for s in ss))
                         if sum(s["inh"] for s in ss) else None,
        }
    return out


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


def _fnum(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
        print(f"EffortMLB: park factors unavailable: {e}")
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
        # FanGraphs playerid -> MLBAM id, built off whichever leaders board
        # has already loaded. The splits API keys on the former and every
        # other join in this file on the latter.
        self._fg_id_map: Dict[str, Dict[int, int]] = {}
        # (position, stat_type) -> (ts, {split label: {mlbam: row}})
        self._fg_splits: Dict[tuple, tuple] = {}
        self._fg_splits_lock = asyncio.Lock()
        self._fg_lock = asyncio.Lock()
        self._itp_pages: Dict[int, tuple] = {}       # pid -> (ts, page dict)
        self._mgr_tend: Dict[str, tuple] = {}        # abbr -> (ts, tendencies)
        # venue -> {'All'|'R'|'L': park-factor row}; one fetch per session
        self._park_cache: Optional[Dict[str, Dict[str, dict]]] = None
        # league-average defensive value per contact zone (the `adv` baseline)
        self._lg_zone_def: Dict[tuple, float] = {}
        self._frv_cache: Optional[Dict[str, dict]] = None
        self._all_team_def: Dict[str, Dict[str, dict]] = {}
        # league RE24 accumulator: (bases, outs) -> [sum runs to end, n]
        self._re24_acc: Dict[tuple, List[float]] = {}
        # win expectancy + transitions (Leverage Index) and the starter's
        # times-through-order states, all off the same play-by-play pass
        self._we_acc: Dict[tuple, List[float]] = {}
        self._we_trans: Dict[tuple, int] = {}
        self._tto_acc: Dict[int, Dict[tuple, List[float]]] = {}
        # {gamePk: called-pitch tallies} + {gamePk: home-plate umpire}
        self._ump_acc: Dict[int, dict] = {}
        # season play-by-play checkpoint: {"games": set, "teams": {tid: acc}}
        # loaded lazily from disk; the league tables above ride with it
        self._pbp_ck: Optional[dict] = None
        self._pbp_lock = asyncio.Lock()
        self._ump_by_game: Optional[tuple] = None    # (ts, {pk: name})
        self._arsenal_stats: Optional[tuple] = None  # (ts, {(pid, pt): row})
        self._arm_angles: Optional[tuple] = None     # (ts, {pid: angle})
        self._arsenal_phys: Optional[tuple] = None   # (ts, {pid: {pt: {...}}})
        self._swing_path: Optional[tuple] = None     # (ts, {pid: swing dict})
        self._catcher_def: Optional[tuple] = None    # (ts, {pid: recv dict})
        # (ts, {pid: hold dict}) and (ts, {pid: steal dict})
        self._pitcher_rungame: Optional[tuple] = None
        self._basestealing: Optional[tuple] = None
        # generic Savant boards, all (ts, {pid: dict}) — see _savant_board
        self._swing_take: Optional[tuple] = None
        self._batted_ball_board: Optional[tuple] = None
        self._arm_board: Optional[tuple] = None
        self._of_jump_board: Optional[tuple] = None
        self._proj_cache: Dict[str, Dict[int, dict]] = {}
        self._stance_board: Optional[dict] = None    # {pid: {foot_sep, angle}}
        self._park_factors: Optional[Dict[str, dict]] = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._roster_lock = asyncio.Lock()
        # url -> (fetched_at, rows) for every Savant CSV board, + per-url
        # single-flight locks (see _fetch_savant_csv)
        self._savant_csv: Dict[str, tuple] = {}
        self._savant_csv_locks: Dict[str, asyncio.Lock] = {}
        # (pid, player_type, year) -> single-flight lock for _get_pitch_detail
        self._pitch_detail_locks: Dict[tuple, asyncio.Lock] = {}
        # single-flight for the league-wide boards that memoise by hand
        self._xstats_locks: Dict[str, asyncio.Lock] = {}
        self._frv_lock = asyncio.Lock()
        self._proj_locks: Dict[str, asyncio.Lock] = {}
        # {pid: [xwobacon, bbe] | None} for season-1 — see get_prior_wobacon.
        # Permanent (a finished season cannot change); None until read.
        self._prior_wobacon: Optional[Dict[str, Optional[list]]] = None
        self._bullpen_lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------- shared session

    async def session(self) -> aiohttp.ClientSession:
        """The one keep-alive session every fetch should ride on. Created
        lazily (a connector must be built on the running loop) and reused for
        the process' lifetime, so statsapi/savant sockets survive between
        panel loads instead of paying a fresh DNS+TCP+TLS handshake each."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=32, limit_per_host=16,
                    ttl_dns_cache=300, keepalive_timeout=60))
        return self._session

    @asynccontextmanager
    async def http(self):
        """`async with stats.http() as session:` — hands out the shared
        session. Deliberately does NOT close it on exit (that is what
        `async with aiohttp.ClientSession()` at each call site used to do,
        throwing away every pooled connection)."""
        yield await self.session()

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------ fetching

    async def _get_json(self, session: aiohttp.ClientSession, url: str,
                        params: Optional[dict] = None) -> Optional[dict]:
        cache_key = url + json.dumps(params or {}, sort_keys=True)
        cached = dev_cache_get(cache_key)
        if cached is not None:
            return json_loads(cached)
        async with self._sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        print(f"EffortMLB: {resp.status} for {url}")
                        return None
                    data = json_loads(await resp.read())
                    # dev_cache_put no-ops when the dev cache is off, but the
                    # argument is evaluated either way — this re-serialised
                    # every response in normal runs and threw the string away.
                    if DEV_CACHE:
                        dev_cache_put(cache_key, json.dumps(data))
                    return data
            except Exception as e:
                print(f"EffortMLB: error fetching {url}: {e}")
                return None

    @staticmethod
    def _read_roster_cache(cache) -> Optional[dict]:
        """Blocking disk read + JSON parse of the roster cache — runs in an
        executor so the (large) parse never stalls the qasync event loop."""
        try:
            return json_loads(cache.read_text())
        except Exception:
            return None

    @staticmethod
    def _build_roster_maps(data: dict) -> tuple:
        """CPU-bound roster-dict build (thousands of players, norm_name each).
        Kept out of the event loop via run_in_executor."""
        teams = {t["id"]: t.get("abbreviation", "?") for t in data["teams"]}
        name_to_abbr = {t.get("name", ""): t.get("abbreviation", "?")
                        for t in data["teams"]}
        roster = {}
        for p in data["players"]:
            rec = {
                "id": p["id"],
                "name": p.get("fullName", ""),
                "team_id": (p.get("currentTeam") or {}).get("id"),
                "position": (p.get("primaryPosition") or {}).get("abbreviation", ""),
                # L/R/S — the lineup strip colours the platoon edge with it.
                # Already in the /sports/1/players payload (no field filter),
                # so it is in the on-disk cache too and costs nothing.
                "bats": (p.get("batSide") or {}).get("code", ""),
                "throws": (p.get("pitchHand") or {}).get("code", ""),
            }
            roster[norm_name(rec["name"])] = rec
        return teams, name_to_abbr, roster

    async def ensure_roster(self, session: aiohttp.ClientSession) -> bool:
        """Load the season player list + team abbreviations (disk-cached).
        The disk read/parse and the roster-map build are both offloaded to a
        thread so the initial launch doesn't freeze while the UI is up."""
        async with self._roster_lock:
            if self._roster and self._teams:
                return True
            loop = asyncio.get_event_loop()
            cache = SAVE_DIR / f"mlb_roster_{self.season}.json"
            data = None
            if cache.exists() and time.time() - cache.stat().st_mtime < ROSTER_TTL:
                data = await loop.run_in_executor(
                    None, self._read_roster_cache, cache)
            if data is None:
                # _build_roster_maps reads six fields per player; the full
                # payload is 1.4MB of biography. Trimming it to 0.22MB was
                # verified to build an identical roster/teams/name map, and
                # it shrinks the on-disk cache this writes by the same 84%,
                # so the once-a-day reload gets cheaper too.
                players = await self._get_json(
                    session, f"{STATS_BASE}/sports/1/players",
                    {"season": str(self.season),
                     "fields": ROSTER_FIELDS})
                teams = await self._get_json(
                    session, f"{STATS_BASE}/teams", {"sportId": "1"})
                if not players or not teams:
                    return False
                data = {"players": players.get("people", []),
                        "teams": teams.get("teams", [])}
                try:
                    await loop.run_in_executor(
                        None, cache.write_text, json.dumps(data))
                except Exception:
                    pass

            (self._teams, self._team_name_to_abbr,
             self._roster) = await loop.run_in_executor(
                None, self._build_roster_maps, data)
            print(f"EffortMLB: roster loaded "
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
            print(f"EffortMLB: no roster match for '{player_name}'")
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
                print(f"EffortMLB: summarize failed for {player}: {e}")
                return label, None

        results = await asyncio.gather(*(one(*row) for row in rows))
        return dict(results)

    # ------------------------------------------------------------- matchup

    async def _get_schedule(self, session: aiohttp.ClientSession,
                            force: bool = False) -> List[dict]:
        """Today's games hydrated with probable pitchers (cached).

        `force` skips the TTL. This call is the ONLY source of lineups in the
        window, and a lineup posts hours after launch — the lineup poll uses
        it to re-read a slate that was fetched before the cards were out."""
        if (not force and self._schedule
                and time.time() - self._schedule[0] < LOG_TTL):
            return self._schedule[1]
        today = datetime.now().strftime("%Y-%m-%d")
        data = await self._get_json(session, f"{STATS_BASE}/schedule", {
            "sportId": "1", "date": today,
            "hydrate": "probablePitcher,lineups,officials"})
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
                                           "probable": None, "fielders": {}})
                players = lineups.get(key) or []
                if players and not m["posted"]:
                    m["posted"] = True
                    m["slots"] = {p.get("id"): i + 1
                                  for i, p in enumerate(players)}
                    # position + name per starter, so a fielder who is in the
                    # game but absent from the fielding boards can still be
                    # SHOWN as unpriced rather than silently vanishing
                    m["fielders"] = {
                        (p.get("primaryPosition") or {}).get("abbreviation"):
                            {"id": p.get("id"), "name": p.get("fullName", "")}
                        for p in players
                        if (p.get("primaryPosition") or {}).get("abbreviation")
                        in ("1B", "2B", "3B", "SS", "LF", "CF", "RF")}
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
            # SO-Walk rate (K-BB%) straight off the season counts, and the
            # runners-stranded rate (LOB%) and BABIP-allowed delta vs league —
            # all StatsAPI-only so the season line paints instantly (the
            # FG-board additions land later, once the arsenal card resolves).
            bf = fnum(s.get("battersFaced"))
            k = fnum(s.get("strikeOuts"))
            bb = fnum(s.get("baseOnBalls"))
            kbb_pct = ((k - bb) / bf) if (bf and k is not None
                                          and bb is not None) else None
            h = fnum(s.get("hits"))
            hbp = fnum(s.get("hitByPitch")) or 0.0
            r = fnum(s.get("runs"))
            hr = fnum(s.get("homeRuns"))
            lob_pct = None
            if None not in (h, bb, r, hr):
                on = h + bb + hbp
                denom = on - 1.4 * hr
                if denom > 0:
                    lob_pct = max(0.0, min(1.0, (on - r) / denom))
            babip = fnum(s.get("babip"))
            babip_delta = None
            if babip is not None:
                d = babip - LG_BABIP
                tri = "▲" if d >= 0 else "▼"   # ▲ above / ▼ below lg
                babip_delta = f"{tri}{abs(d):.3f}".replace("0.", ".")
            pairs = [
                ("W-L", f"{s.get('wins', 0)}-{s.get('losses', 0)}"),
                ("ERA", s.get("era")), ("G", s.get("gamesPlayed")),
                ("GS", s.get("gamesStarted")), ("IP", s.get("inningsPitched")),
                ("SO", s.get("strikeOuts")), ("WHIP", s.get("whip")),
                ("K/9", s.get("strikeoutsPer9Inn")),
                ("BB/9", s.get("walksPer9Inn")),
                ("K-BB%", f"{kbb_pct:.1%}" if kbb_pct is not None else None),
                ("HR/9", s.get("homeRunsPer9")),
                ("AVGa", s.get("avg")),
                ("BABIP", babip_delta),
                ("LOB%", f"{lob_pct:.1%}" if lob_pct is not None else None),
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
            lock = self._xstats_locks.setdefault(ptype, asyncio.Lock())
            async with lock:
                cached = self._xstats.get(ptype)
                if not cached or time.time() - cached[0] >= PITCH_SPLITS_TTL:
                    # Via _fetch_savant_csv rather than a bespoke fetch: it
                    # already does the BOM strip and the DictReader pass, and
                    # brings the per-URL single-flight lock this board was
                    # missing (every batter on the slate wants the same one).
                    url = SAVANT_XSTATS_URL.format(player_type=ptype,
                                                   year=self.season)
                    board: Dict[int, dict] = {}
                    for row in await self._fetch_savant_csv(session, url):
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
        # "One page fetch covers all 30 teams" only holds if the concurrent
        # callers don't all miss the cache together — which they did, pulling
        # this ~0.5MB page twice per session. Serialise the miss.
        async with self._bullpen_lock:
            if not self._bullpen or time.time() - self._bullpen[0] >= 1800:
                await self._load_bullpen_page(session)
        return self._bullpen[1].get(team_abbr, [])

    async def _load_bullpen_page(self, session: aiohttp.ClientSession):
        """Download + parse the league bullpen-usage page into self._bullpen.
        Caller holds _bullpen_lock."""
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
                    print(f"EffortMLB: bullpen fetch failed: {e}")
            dev_cache_put(BULLPEN_USAGE_URL, html)
        if html:
            # lxml over the whole-league usage page measured ~70ms on the
            # loop thread — a visible hitch every 30 minutes. It touches
            # no shared state beyond the read-only team-name map, so it
            # offloads cleanly.
            teams = await asyncio.get_running_loop().run_in_executor(
                None, self._parse_bullpen_page, html)
        self._bullpen = (time.time(), teams)

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
                print(f"EffortMLB: FG pitching board loaded "
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
            # same source as SIERA so the ERA-SIERA gap can't mix providers
            "era": row.get("ERA"),
            # lets pen_strength drop rotation arms that show up on the
            # bullpen-usage page after a relief outing
            "gs": row.get("GS"), "g": row.get("G"),
            # -- PitchingBot: FanGraphs' OTHER pitch-quality model, and the
            #    reason to carry it next to Stuff+ is `command`. Stuff+ grades
            #    the pitch in isolation and has no location term at all, so a
            #    pitcher with good shape and no idea where it is going reads
            #    the same as one who spots it. pb_command is the half Stuff+
            #    structurally cannot see. Same 100-scale convention.
            #    pb_xRV100 is its expected run value per 100 pitches, which
            #    unlike the + scales is in runs and signed (negative is good
            #    for the pitcher).
            "pb_stuff": row.get("pb_stuff"),
            "pb_command": row.get("pb_command"),
            "pb_overall": row.get("pb_overall"),
            "pb_era": row.get("pb_ERA"),
            "pb_xrv100": row.get("pb_xRV100"),
            # per-pitch stuff / command / overall, same codes as sp_s_*
            "pb_per_pitch": {
                code: {"s": row.get(f"pb_s_{code}"),
                       "c": row.get(f"pb_c_{code}"),
                       "o": row.get(f"pb_o_{code}")}
                for code in ("FF", "SI", "FC", "SL", "CU", "KC", "CH", "FS")
                if row.get(f"pb_s_{code}") is not None
                or row.get(f"pb_c_{code}") is not None
            },
            # per-pitch Location+ and Pitching+ — only Stuff+ (sp_s_*) was
            # being read, so the location half of the + family was unused too
            "loc_per_pitch": {
                code: row.get(f"sp_l_{code}")
                for code in ("FF", "SI", "FC", "SL", "CU", "KC", "CH", "FS",
                             "FO")
                if row.get(f"sp_l_{code}") is not None
            },
            # other ERA estimators already on the board and never read
            "xera": row.get("xERA"), "kwera": row.get("kwERA"),
            "lob": row.get("LOB%"), "babip": row.get("BABIP"),
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
                print(f"EffortMLB: FG batting board loaded "
                      f"({len(board)} batters)")
        row = self._fg_batting.get(pid)
        if not row:
            return None
        return {
            "woba": row.get("wOBA"), "wrcplus": row.get("wRC+"),
            "war": row.get("WAR"),
            # PA gates the rate stats: a 359 wRC+ is a handful of trips, not
            # a hitter, and it read as a real number on the opponent card
            # PlayerName, NOT Name: the FG board's `Name` is a raw HTML anchor
            # (`<a href="statss.aspx?...">Max Muncy</a>`). Rendered into a
            # rich-text label it parses as a malformed tag and the whole name
            # disappears — the opponent card showed a bare "7  131" for Muncy.
            "pa": row.get("PA"), "name": row.get("PlayerName"),
            # Pos is FanGraphs' positional adjustment; Defense minus Pos is
            # the position-relative fielding component
            "pos": row.get("Pos"), "games": row.get("G"),
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
            # Plate-discipline + batted-ball profile for the season-line 4th row
            "z_contact": row.get("Z-Contact%"),   # contact on in-zone swings
            "contact": row.get("Contact%"),       # overall contact rate
            "swstr": row.get("SwStr%"),            # swinging-strike rate
            "chase": row.get("O-Swing%"),          # out-of-zone swing (chase)
            "pull_pct": row.get("Pull%"),
            "hr_fb": row.get("HR/FB"),
            "hardhit": row.get("HardHit%"),
            # -- Expected stats, FanGraphs' own. Kept alongside the Savant
            #    xstats board rather than instead of it: they are different
            #    models and the two DISAGREEING is worth seeing.
            "xavg": row.get("xAVG"), "xslg": row.get("xSLG"),
            "xwoba": row.get("xwOBA"),
            # -- Park- and league-adjusted index family. Everything here is
            #    scaled so 100 = league average AFTER the park correction,
            #    same convention as wRC+. This is the block that lets a
            #    peripheral be read the way wRC+ already is: Judge's 136
            #    Hard%+ means 36% more hard contact than league *for his
            #    park*, which his raw 46.2% Hard% cannot say on its own.
            #    NOTE these are indices, not rates — do not print a % sign.
            "avg_plus": row.get("AVG+"), "obp_plus": row.get("OBP+"),
            "slg_plus": row.get("SLG+"), "iso_plus": row.get("ISO+"),
            "babip_plus": row.get("BABIP+"),
            "bb_plus": row.get("BB%+"), "k_plus": row.get("K%+"),
            "ld_plus": row.get("LD%+"), "gb_plus": row.get("GB%+"),
            "fb_plus": row.get("FB%+"), "hrfb_plus": row.get("HRFB%+"),
            "pull_plus": row.get("Pull%+"), "cent_plus": row.get("Cent%+"),
            "oppo_plus": row.get("Oppo%+"),
            "soft_plus": row.get("Soft%+"), "med_plus": row.get("Med%+"),
            "hard_plus": row.get("Hard%+"),
            # -- Contact-quality tiers. Soft/Med/Hard partition every batted
            #    ball and are NOT the same cut as HardHit% (which is a flat
            #    95mph EV gate); this is SIS's classification.
            "soft": row.get("Soft%"), "med": row.get("Med%"),
            "hard": row.get("Hard%"),
            # -- Situational value. `Clutch` is FanGraphs' own: how much
            #    better the hitter did in high leverage than his own overall
            #    line, so it is already self-relative and near zero for
            #    almost everyone. WPA/LI is the leverage-NEUTRAL version of
            #    WPA, which is why both are here — WPA alone confounds
            #    performance with when he happened to bat.
            "clutch": row.get("Clutch"), "wpa_li": row.get("WPA/LI"),
            "re24": row.get("RE24"), "rew": row.get("REW"),
            "pli": row.get("pLI"), "wraa": row.get("wRAA"),
            # -- Batting stance geometry. This is the board the local
            #    BATTING_STANCE_CSV export was standing in for — it has been
            #    in the leaders board all along, so the "TODO: automate the
            #    fetch" note on that constant is closed by these three keys.
            #    Inches from the plate / from the back of the box, and the
            #    stance angle.
            "depth_in_box": row.get("DepthInBox"),
            "dist_off_plate": row.get("DistanceOffPlate"),
            "tilt": row.get("Tilt"),
            "competitive_swings": row.get("CompetitiveSwings"),
            # -- Baserunning components behind the BsR total already read
            #    above: Spd is Bill James' speed score, UBR the non-steal
            #    baserunning runs, wSB the steal runs, XBR the extra-base
            #    taken runs.
            "spd": row.get("Spd"), "ubr": row.get("UBR"),
            "wsb": row.get("wBsR"), "xbr": row.get("XBR"),
            # -- Three-true-outcome share and called-strike rate; `Swords`
            #    is FanGraphs' count of swings so bad they are comic.
            "tto": row.get("TTO%"), "cstr": row.get("CStr%"),
            # -- Savant ATTACK ZONES, passed through under their raw names
            #    (scH-/scS-/scSI-/scSO-/scC-/scW-/scZ-/scO- x Zone/Swing/
            #    Contact%). See _ATTACK_ZONES for how the letters were
            #    identified — FanGraphs publishes no key for them.
            **{k: v for k, v in row.items() if k.startswith("sc")},
            # -- pfx pitch-type block: usage, velocity faced and run value
            #    per 100 by type. `pitches` turns the usage share into a
            #    pitch COUNT, which is what the sample gate needs.
            "pitches": row.get("Pitches"),
            **{k: v for k, v in row.items() if k.startswith("pfx")},
        }

    async def _get_fg_id_map(self, position: str) -> Dict[int, int]:
        """FanGraphs playerid -> MLBAM id.

        The splits API keys on `playerid`; everything else in this file keys
        on MLBAM. The leaders board carries BOTH, so loading it (which the
        panel already does for every player it shows) is what makes the
        splits joinable at all. Falls back to fetching the board if the panel
        has not already.
        """
        if position in self._fg_id_map:
            return self._fg_id_map[position]
        stats = "bat" if position == "B" else "pit"
        board = self._fg_batting if position == "B" else self._fg_pitching
        if board is None:
            # get_fg_* populates the cached board and holds its own lock
            if position == "B":
                await self.get_fg_batting(0)
                board = self._fg_batting
            else:
                await self.get_fg_pitching(0)
                board = self._fg_pitching
        mapping: Dict[int, int] = {}
        for mlbam, row in (board or {}).items():
            fgid = row.get("playerid")
            try:
                mapping[int(fgid)] = int(mlbam)
            except (TypeError, ValueError):
                continue
        if not mapping:
            print(f"EffortMLB: FG id map empty for '{stats}' — splits "
                  f"cannot be joined")
        self._fg_id_map[position] = mapping
        return mapping

    async def get_fg_splits(self, position: str = "B",
                            stat_type: str = FG_SPLIT_TYPE_ADVANCED
                            ) -> Dict[str, Dict[int, dict]]:
        """Every split in FG_SPLIT_IDS for the whole league, re-keyed to MLBAM.

        Returns {split label: {mlbam id: stat row}}. One request per split
        (~40 of them) but each covers all ~600 hitters, and they are
        slate-cached, so this is a once-a-day cost shared by every player the
        panel will ever show — not a per-player fetch.

        Splits that fail are simply absent from the returned dict; callers
        must not treat a missing label as zero.
        """
        key = (position, stat_type)
        async with self._fg_splits_lock:
            cached = self._fg_splits.get(key)
            if cached and time.time() - cached[0] < FG_SPLITS_TTL:
                return cached[1]
            id_map = await self._get_fg_id_map(position)
            loop = asyncio.get_running_loop()
            labels = list(FG_SPLIT_IDS)

            async def one(label: str):
                rows = await loop.run_in_executor(
                    None, fetch_fg_split_sync, FG_SPLIT_IDS[label],
                    stat_type, position, self.season)
                return label, rows

            out: Dict[str, Dict[int, dict]] = {}
            # Modest fan-out AND a pause between chunks. These are 230KB
            # responses and the whole set is slate-cached after the first
            # run, so there is nothing to gain from hammering — and there is
            # something real to lose: firing this set at FanGraphs without a
            # pause trips Cloudflare's rate limit on `/api/*`, which takes
            # down the LEADERS board too (that one needs headless Firefox and
            # is the spine of every value stat in the window). ~10s once a
            # day is the right trade.
            for i in range(0, len(labels), 4):
                chunk = labels[i:i + 4]
                if i:
                    await asyncio.sleep(FG_SPLITS_CHUNK_PAUSE)
                for label, rows in await asyncio.gather(
                        *(one(x) for x in chunk)):
                    if not rows:
                        continue
                    remapped = {}
                    for fgid, rec in rows.items():
                        mlbam = id_map.get(fgid)
                        if mlbam is not None:
                            remapped[mlbam] = rec
                    if remapped:
                        out[label] = remapped
            print(f"EffortMLB: FG splits loaded ({len(out)}/{len(labels)} "
                  f"splits, {position}, type {stat_type})")
            self._fg_splits[key] = (time.time(), out)
            return out

    async def get_batter_swing_path(self, session: aiohttp.ClientSession,
                                    pid: int) -> Optional[dict]:
        """Statcast swing-path / attack-angle / intercept / box-position for
        one batter (Savant bat-tracking/swing-path-attack-angle board, one
        league-wide CSV cached ~1h, keyed by MLBAM id). Feeds the combo
        flight-viewer's batter-in-the-box overlay. All angles in degrees,
        intercept + box positions in inches (Savant convention)."""
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        now = time.time()
        if not self._swing_path or now - self._swing_path[0] >= PITCH_SPLITS_TTL:
            board: Dict[int, dict] = {}
            for row in await self._fetch_savant_csv(
                    session, SAVANT_SWING_PATH_URL.format(year=self.season)):
                try:
                    bid = int(row["id"])
                except (KeyError, ValueError):
                    continue
                board[bid] = {
                    "side": row.get("side") or "",
                    "bat_speed": fnum(row.get("avg_bat_speed")),
                    "swing_tilt": fnum(row.get("swing_tilt")),
                    "attack_angle": fnum(row.get("attack_angle")),
                    "attack_dir": fnum(row.get("attack_direction")),
                    "ideal_aa_rate": fnum(row.get("ideal_attack_angle_rate")),
                    "intercept_vs_plate": fnum(
                        row.get("avg_intercept_y_vs_plate")),
                    "intercept_vs_batter": fnum(
                        row.get("avg_intercept_y_vs_batter")),
                    "depth_in_box": fnum(row.get("avg_batter_y_position")),
                    "dist_off_plate": fnum(row.get("avg_batter_x_position")),
                    "competitive_swings": fnum(row.get("competitive_swings")),
                }
            self._swing_path = (now, board)
        d = self._swing_path[1].get(pid)
        if d is not None:
            d = dict(d, **self._load_stance_board().get(pid, {}))
        return d

    async def get_catcher_defense(self, session: aiohttp.ClientSession,
                                  pid: int) -> Optional[dict]:
        """Receiving profile for one catcher: framing runs + called-strike
        rate (Savant catcher-framing) and blocking runs + blocks above
        average (catcher-blocking). Two league-wide CSVs merged into one
        board keyed by MLBAM id, cached ~1h."""
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        now = time.time()
        if not self._catcher_def or now - self._catcher_def[0] >= PITCH_SPLITS_TTL:
            board: Dict[int, dict] = {}
            for row in await self._fetch_savant_csv(
                    session,
                    SAVANT_CATCHER_FRAMING_URL.format(year=self.season)):
                try:
                    cid = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                board[cid] = {
                    "framing_runs": fnum(row.get("rv_tot")),
                    "strike_rate": fnum(row.get("pct_tot")),
                    "framed_pitches": fnum(row.get("pitches")),
                }
            for row in await self._fetch_savant_csv(
                    session,
                    SAVANT_CATCHER_BLOCKING_URL.format(year=self.season)):
                try:
                    cid = int(row["player_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                board.setdefault(cid, {}).update({
                    "blocking_runs": fnum(row.get("catcher_blocking_runs")),
                    "blocks_aa": fnum(row.get("blocks_above_average")),
                    "pbwp": fnum(row.get("n_pbwp")),
                    "x_pbwp": fnum(row.get("x_pbwp")),
                })
            for row in await self._fetch_savant_csv(
                    session,
                    SAVANT_CATCHER_THROWING_URL.format(year=self.season)):
                try:
                    cid = int(row["player_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                board.setdefault(cid, {}).update({
                    "throw_runs": fnum(row.get("catcher_stealing_runs")),
                    "cs_aa": fnum(row.get("caught_stealing_above_average")),
                    "pop_time": fnum(row.get("pop_time")),
                    "arm": fnum(row.get("arm_strength")),
                    "sb_att": fnum(row.get("sb_attempts")),
                })
            self._catcher_def = (now, board)
        return self._catcher_def[1].get(pid)

    async def _savant_board(self, session: aiohttp.ClientSession, attr: str,
                            url: str, id_cols, cols: Dict[str, str]
                            ) -> Dict[int, dict]:
        """Load-and-cache one Savant leaderboard keyed by MLBAM id.

        These boards differ only in their URL, which column holds the id, and
        which columns are wanted — `id_cols` is a tuple because Savant is not
        consistent about it even across boards on the same page
        (`player_id`, `id`, `resp_fielder_id` are all in use)."""
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        cached = getattr(self, attr, None)
        now = time.time()
        if cached and now - cached[0] < PITCH_SPLITS_TTL:
            return cached[1]
        board: Dict[int, dict] = {}
        try:
            rows = await self._fetch_savant_csv(
                session, url.format(year=self.season))
        except Exception as e:
            print(f"EffortMLB: savant board {attr} failed: {e}")
            return (cached[1] if cached else {})
        for row in rows:
            rid = None
            for c in id_cols:
                try:
                    rid = int(row[c])
                    break
                except (KeyError, TypeError, ValueError):
                    continue
            if rid is None:
                continue
            board[rid] = {dst: fnum(row.get(src)) for dst, src in cols.items()}
        setattr(self, attr, (now, board))
        return board

    async def get_sprint_speed(self, session: aiohttp.ClientSession
                               ) -> Dict[int, dict]:
        """Sprint speed (ft/s) and home-to-first, keyed by MLBAM id.

        The replay moves each baserunner at HIS speed, so a 30 ft/s burner
        visibly beats a 25 ft/s catcher to the next bag instead of every
        runner gliding at the same rate."""
        return await self._savant_board(
            session, "_sprint_board",
            "https://baseballsavant.mlb.com/leaderboard/sprint_speed"
            "?year={year}&position=&team=&min=10&csv=true",
            ("player_id",),
            {"speed": "sprint_speed", "hp_1b": "hp_to_1b"})

    async def get_projections(self, session: aiohttp.ClientSession,
                              stats: str = "bat",
                              system: str = "rfangraphsdc"
                              ) -> Dict[int, dict]:
        """FanGraphs projections keyed by MLBAM id.

        Slate-cached: a projection is a season-long estimate and does not
        move between the morning and first pitch, so it is exactly the kind
        of thing the per-day cache is for."""
        ck = f"fgproj_{system}_{stats}_{self.season}"
        mem = self._proj_cache.get(ck)
        if mem:
            return mem
        lock = self._proj_locks.setdefault(ck, asyncio.Lock())
        async with lock:
            mem = self._proj_cache.get(ck)
            if mem:
                return mem
            return await self._fetch_projections(session, stats, system, ck)

    async def _fetch_projections(self, session: aiohttp.ClientSession,
                                 stats: str, system: str, ck: str
                                 ) -> Dict[int, dict]:
        """Uncached body of `get_projections` — call through that. The
        projections tab asks for several boards at once, and FanGraphs 403s
        per-IP on burst, so overlapping identical pulls are worth avoiding."""
        raw = slate_cache_get(ck)
        rows = None
        if raw:
            try:
                rows = json.loads(raw)
            except ValueError:
                rows = None
        if rows is None:
            url = FG_PROJ_URL.format(system=system, stats=stats)
            try:
                # DO NOT send a browser User-Agent here. Cloudflare checks the
                # UA against the TLS fingerprint, so claiming to be Chrome
                # from a non-Chrome stack is WORSE than being honest: plain
                # curl gets 200, curl with a Chrome UA gets 403, and aiohttp
                # with a Chrome UA gets 403 for the same reason. Sending no
                # UA override passes on every system/side (10/10 at 200).
                async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        print(f"EffortMLB: projections {system}/{stats} "
                              f"HTTP {resp.status}")
                        return {}
                    rows = await resp.json(content_type=None)
            except Exception as e:
                print(f"EffortMLB: projections {system}/{stats} failed: {e}")
                return {}
            if isinstance(rows, list) and rows:
                slate_cache_put(ck, json.dumps(rows))
        out: Dict[int, dict] = {}
        for r in (rows or []):
            try:
                pid = int(r.get("xMLBAMID"))
            except (TypeError, ValueError):
                continue
            out[pid] = r
        self._proj_cache[ck] = out
        return out

    async def get_swing_take(self, session) -> Dict[int, dict]:
        """Hitter run value by zone — Heart / Shadow / Chase / Waste."""
        return await self._savant_board(
            session, "_swing_take", SAVANT_SWING_TAKE_URL,
            ("player_id", "id"),
            {"all": "runs_all", "heart": "runs_heart", "shadow": "runs_shadow",
             "chase": "runs_chase", "waste": "runs_waste", "pa": "pa",
             "pitches": "pitches"})

    async def get_batted_ball(self, session) -> Dict[int, dict]:
        """Hitter ground/air crossed with pull/straight/oppo."""
        return await self._savant_board(
            session, "_batted_ball_board", SAVANT_BATTED_BALL_URL,
            ("id", "player_id"),
            {"bbe": "bbe", "gb": "gb_rate", "air": "air_rate", "fb": "fb_rate",
             "ld": "ld_rate", "pu": "pu_rate", "pull": "pull_rate",
             "straight": "straight_rate", "oppo": "oppo_rate",
             "pull_gb": "pull_gb_rate", "pull_air": "pull_air_rate",
             "oppo_air": "oppo_air_rate"})

    async def get_arm_strength(self, session) -> Dict[int, dict]:
        """Throwing arm by position group (mph)."""
        return await self._savant_board(
            session, "_arm_board", SAVANT_ARM_STRENGTH_URL,
            ("player_id", "id"),
            {"max": "max_arm_strength", "overall": "arm_overall",
             "of": "arm_of", "inf": "arm_inf", "throws": "total_throws"})

    async def get_of_jump(self, session) -> Dict[int, dict]:
        """Outfield jump: the reaction / burst / route under the OAA."""
        return await self._savant_board(
            session, "_of_jump_board", SAVANT_OF_JUMP_URL,
            ("resp_fielder_id", "player_id", "id"),
            {"oaa": "outs_above_average", "reaction": "rel_league_reaction_distance",
             "burst": "rel_league_burst_distance",
             "route": "rel_league_routing_distance", "n": "n"})

    async def get_pitcher_running_game(self, session: aiohttp.ClientSession,
                                       pid: int) -> Optional[dict]:
        """How well a pitcher holds runners.

        `rate_sbx` is stolen-base attempts per OPPORTUNITY, which is the
        number that actually belongs next to a manager's attempt rate — a
        raw attempt count mostly measures how often his club had men on.
        The lead distances are the mechanism: a pitcher who lets runners
        take a long secondary lead concedes the base before the throw."""
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        now = time.time()
        if (not self._pitcher_rungame
                or now - self._pitcher_rungame[0] >= PITCH_SPLITS_TTL):
            board: Dict[int, dict] = {}
            for row in await self._fetch_savant_csv(
                    session,
                    SAVANT_PITCHER_RUNGAME_URL.format(year=self.season)):
                try:
                    rid = int(row["player_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                # `key_target_base` reads "All" on every row of the default
                # board — it is already aggregated across second and third,
                # one row per pitcher, so take the values rather than
                # summing per-base rows that do not exist.
                board[rid] = {
                    "runs": fnum(row.get("runs_prevented_on_running_attr")),
                    "sb": int(fnum(row.get("n_sb")) or 0),
                    "cs": int(fnum(row.get("n_cs")) or 0),
                    "pk": int(fnum(row.get("n_pk")) or 0),
                    "att": int(fnum(row.get("n_init")) or 0),
                    "rate_sbx": fnum(row.get("rate_sbx")),
                    "lead_pri": fnum(row.get("r_primary_lead")),
                    "lead_sec": fnum(row.get("r_secondary_lead")),
                    "cs_aa": fnum(row.get("n_pitcher_cs_aa")),
                }
            self._pitcher_rungame = (now, board)
        return self._pitcher_rungame[1].get(pid)

    async def get_basestealing(self, session: aiohttp.ClientSession
                               ) -> Dict[int, dict]:
        """Runner-side stealing value, keyed by MLBAM id (whole board)."""
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        now = time.time()
        if (not self._basestealing
                or now - self._basestealing[0] >= PITCH_SPLITS_TTL):
            board: Dict[int, dict] = {}
            for row in await self._fetch_savant_csv(
                    session,
                    SAVANT_BASESTEALING_URL.format(year=self.season)):
                try:
                    rid = int(row["player_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                # same shape as the pitcher board — one aggregated row each
                board[rid] = {
                    "runs": fnum(row.get("runs_stolen_on_running_act")),
                    "sb": int(fnum(row.get("n_sb")) or 0),
                    "cs": int(fnum(row.get("n_cs")) or 0),
                    "att": int(fnum(row.get("n_init")) or 0),
                    "rate_sbx": fnum(row.get("rate_sbx")),
                    "lead_sec": fnum(row.get("r_secondary_lead")),
                }
            self._basestealing = (now, board)
        return self._basestealing[1]

    def _load_stance_board(self) -> Dict[int, dict]:
        """Batting-stance metrics (foot separation + open/closed stance angle)
        from the locally-downloaded Savant export, keyed by MLBAM id. Cached
        once per process; empty if the file is absent."""
        if self._stance_board is not None:
            return self._stance_board
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        board: Dict[int, dict] = {}
        if BATTING_STANCE_CSV.exists():
            import io as _io
            text = BATTING_STANCE_CSV.read_text(encoding="utf-8-sig")
            for row in csv.DictReader(_io.StringIO(text)):
                try:
                    bid = int(row["id"])
                except (KeyError, ValueError):
                    continue
                board[bid] = {
                    "foot_sep": fnum(row.get("avg_foot_sep")),
                    "stance_angle": fnum(row.get("avg_stance_angle")),
                }
        else:
            print("EffortMLB: batting-stance.csv not found — stance angle/feet "
                  "unavailable")
        self._stance_board = board
        return board

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
            x = (hx - _SPRAY_HP[0]) * _SPRAY_SCALE
            y = (_SPRAY_HP[1] - hy) * _SPRAY_SCALE
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
            print(f"EffortMLB: weather unavailable for {venue}: {e}")
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
                                player_id: int, player_type: str,
                                year: Optional[int] = None) -> List[dict]:
        """Raw pitch-by-pitch rows (needed fields only) from the player's
        full-season Savant detail — one ~1-3s CSV fetch, cached 1h. Both the
        by-pitch and by-velocity aggregations run off this cache.

        `year` defaults to the current season; BMIELKE passes the prior one to
        build its player-specific prior.

        Single-flight per key, for the same reason `_fetch_savant_csv` is:
        `_show_player_detail` spawns five concurrent tasks and three of them
        (pitch splits, BMIELKE, zone grids) want the SAME (player, type,
        year) rows, so without the lock they all miss the empty cache
        together and each pull the full-season CSV. Measured on one hitter
        click: 4 fetches / 4.56MB where 3 unique keys were needed."""
        year = year or self.season
        key = (player_id, player_type, year)
        cached = self._pitch_splits.get(key)
        if cached and time.time() - cached[0] < PITCH_SPLITS_TTL:
            return cached[1]
        lock = self._pitch_detail_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._pitch_splits.get(key)
            if cached and time.time() - cached[0] < PITCH_SPLITS_TTL:
                return cached[1]
            return await self._fetch_pitch_detail(
                session, player_id, player_type, year)

    async def _fetch_pitch_detail(self, session: aiohttp.ClientSession,
                                  player_id: int, player_type: str,
                                  year: int) -> List[dict]:
        """Uncached body of `_get_pitch_detail` — always call through that,
        never directly, or the single-flight guarantee is lost."""
        key = (player_id, player_type, year)
        url = SAVANT_SEARCH_URL.format(year=year, player_id=player_id,
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
                            print(f"EffortMLB: savant search {resp.status} "
                                  f"for player {player_id}")
                except Exception as e:
                    print(f"EffortMLB: savant search failed: {e}")
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
                    "bb_type": row.get("bb_type") or "",
                    "hr": (row.get("events") or "") == "home_run",
                    "xwoba": fnum(row.get("estimated_woba_using_speedangle")),
                    "event": row.get("events") or "",
                    "hc_x": fnum(row.get("hc_x")),
                    "hc_y": fnum(row.get("hc_y")),
                    # separates the shallow fly from the one on the track —
                    # different outfielder skill (coming in vs going back)
                    "hit_distance": fnum(row.get("hit_distance_sc")),
                    "if_align": row.get("if_fielding_alignment") or "",
                    "of_align": row.get("of_fielding_alignment") or "",
                    "date": row.get("game_date") or "",
                    "bat_speed": fnum(row.get("bat_speed")),
                    # Swing-path pair BMIELKE v9 selected. Both are measured
                    # per SWING (not per batted ball), which is why they carry
                    # weight at 25 batted balls.
                    "attack_angle": fnum(row.get("attack_angle")),
                    "icept_y": fnum(
                        row.get("intercept_ball_minus_batter_pos_y_inches")),
                    # SP-form extras (times-through-order + real wOBA weights)
                    "tto": fnum(row.get("n_thruorder_pitcher")),
                    "stand": row.get("stand") or "",
                    # the arm he faced — needed for platoon splits on anything
                    # computed off this cache rather than off StatsAPI
                    "p_throws": row.get("p_throws") or "",
                    # days off before this game — Savant ships it and nothing
                    # read it; it is the schedule half of "form"
                    "rest": fnum(row.get("batter_days_since_prev_game")),
                    # count AFTER the pitch — see the ABS note; used for the
                    # first-pitch-strike series
                    "balls": fnum(row.get("balls")),
                    "strikes": fnum(row.get("strikes")),
                    # Working from the stretch is a different pitcher — and
                    # it is the split the HOLD line on the defence card is
                    # about, so the two belong on screen together.
                    "men_on": any(row.get(b) not in (None, "", "null")
                                  for b in ("on_1b", "on_2b", "on_3b")),
                    "woba_v": fnum(row.get("woba_value")),
                    "woba_d": fnum(row.get("woba_denom")),
                    # Savant's own per-pitch run-expectancy delta, from the
                    # batting team's point of view. It has been arriving in
                    # this CSV all along with zero references in the code
                    # while RE24 was being rebuilt by hand elsewhere.
                    "d_run_exp": fnum(row.get("delta_run_exp")),
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
                    # per-PITCH arm angle. The card's `arm_angle` is the
                    # SEASON value off a separate leaderboard, so slot drift
                    # across starts was not observable anywhere until now.
                    "arm_angle": fnum(row.get("arm_angle")),
                })
        self._pitch_splits[key] = (time.time(), rows)
        return rows

    async def get_count_splits(self, session: aiohttp.ClientSession,
                               player_id: int, player_type: str) -> List[dict]:
        """Per-count aggregates off the pitch detail this panel already
        caches — no extra request."""
        rows = await self._get_pitch_detail(session, player_id, player_type)
        return count_splits(rows, player_type == "pitcher")

    async def get_batted_ball_profile(self, session: aiohttp.ClientSession,
                                      player_id: int,
                                      player_type: str) -> List[dict]:
        """Trajectory/spray/alignment mix — same cached rows, no extra call.
        For a pitcher this reads as the profile he ALLOWS."""
        rows = await self._get_pitch_detail(session, player_id, player_type)
        return batted_ball_profile(rows)

    async def get_plate_discipline(self, session: aiohttp.ClientSession,
                                   player_id: int,
                                   player_type: str) -> List[dict]:
        """Swing/chase/contact rates — same cached rows again."""
        rows = await self._get_pitch_detail(session, player_id, player_type)
        return plate_discipline(rows)

    async def get_pitch_mix_by_count(self, session: aiohttp.ClientSession,
                                     pitcher_id: int) -> List[dict]:
        """Opposing starter's usage mix per count — his own cached detail."""
        rows = await self._get_pitch_detail(session, pitcher_id, "pitcher")
        return pitch_mix_by_count(rows)

    async def get_zone_grids(self, session: aiohttp.ClientSession,
                             batter_id: int,
                             pitcher_id: Optional[int]) -> tuple:
        """(batter whiff grid, starter location grid).

        The starter's grid is filtered to batters of THIS hitter's hand — a
        pitcher's location map against lefties is not his map against
        righties, and blending them would describe a matchup that never
        happens. The hand is taken from the hitter's OWN rows rather than from
        MatchupContext, which does not carry it; a switch-hitter resolves to
        whichever side he has batted from more, which is the side these whiff
        numbers are mostly made of anyway."""
        brows = await self._get_pitch_detail(session, batter_id, "batter")
        bat = zone_cells(brows)
        stand = ""
        hands = [r.get("stand") for r in brows if r.get("stand") in ("L", "R")]
        if hands:
            stand = max(("L", "R"), key=hands.count)
        sp = None
        if pitcher_id:
            prows = await self._get_pitch_detail(session, pitcher_id, "pitcher")
            sp = zone_cells(prows, stand_filter=stand or None)
        return bat, sp

    def _prior_path(self) -> Path:
        return SAVE_DIR / f"bmielke_prior_{self.season - 1}.json"

    def _prior_load(self) -> Dict[str, Optional[list]]:
        """The on-disk prior store, read once per process."""
        if self._prior_wobacon is None:
            try:
                p = self._prior_path()
                self._prior_wobacon = (json_loads(p.read_text())
                                       if p.exists() else {})
            except Exception:
                self._prior_wobacon = {}
        return self._prior_wobacon

    async def get_prior_wobacon(self, session: aiohttp.ClientSession,
                                player_id: int) -> tuple:
        """(xwOBAcon, batted balls) for LAST season — BMIELKE's prior.

        Cached PERMANENTLY on disk, keyed by prior season. That season is
        final: the two scalars this returns cannot change, but deriving them
        costs a ~0.8MB full-season pitch-by-pitch CSV, and the 1h in-memory
        TTL it used to ride meant every hitter re-downloaded his immutable
        prior once an hour, every session — measured as half the Savant bytes
        of a lineup click-through. A sub-25-BBE miss is stored as null so
        those hitters stop re-downloading too.

        Returns (None, None) on any failure, and BMIELKE falls back to the
        league prior — which is what it did before v9, so a miss is a
        degradation and never an error."""
        store = self._prior_load()
        key = str(player_id)
        if key in store:
            hit = store[key]
            return (None, None) if hit is None else (hit[0], hit[1])
        try:
            rows = await self._get_pitch_detail(session, player_id, "batter",
                                                year=self.season - 1)
        except Exception as e:
            print(f"EffortMLB: prior-season fetch failed for {player_id}: {e}")
            return None, None
        xw = [r.get("xwoba") or 0.0 for r in rows
              if r.get("ev") is not None and r.get("hc_x") is not None
              and r.get("hc_y") is not None]
        # An EMPTY pull is a failed/throttled fetch, not a real sub-25 hitter
        # — recording it as a null would make that failure permanent.
        out = None if len(xw) < 25 else [sum(xw) / len(xw), len(xw)]
        if rows or out is not None:
            store[key] = out
            try:
                self._prior_path().write_text(json.dumps(store))
            except OSError as e:
                print(f"EffortMLB: prior store write failed: {e}")
        return (None, None) if out is None else (out[0], out[1])

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
                "xw": [], "bat": [], "bip": [], "la": [], "dhh": 0})
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
                    # Spray-adjusted hard hit: scored against the league's
                    # 75th-percentile EV AT THIS BALL'S DIRECTION. `stand` is
                    # always the BATTER's hand, so oppo-signing is correct
                    # whether these rows were pulled for a hitter or for a
                    # pitcher (in which case it reads as DHH allowed).
                    ang = _spray_angle(r.get("hc_x"), r.get("hc_y"))
                    if ang is not None and r.get("stand") in ("L", "R"):
                        if spray_hard_hit(
                                ev, ang * (1.0 if r["stand"] == "R" else -1.0)):
                            b["dhh"] += 1
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
                "dhh": (b["dhh"] / len(b["ev"])) if b["ev"] else None,
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
            h = st.get("hits") or 0
            bb = st.get("baseOnBalls") or 0
            hbp = st.get("hitByPitch") or 0
            runs = st.get("runs") or 0
            hr = st.get("homeRuns") or 0
            # Per-start LOB% (runners stranded): (H+BB+HBP-R)/(H+BB+HBP-1.4HR),
            # clamped to [0,1]; None when there were no baserunners to strand
            denom = h + bb + hbp - 1.4 * hr
            lob = (max(0.0, min(1.0, (h + bb + hbp - runs) / denom))
                   if denom > 0 else None)
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
                "lob": lob,
            })
        starts = [a for a in apps if a["started"]] or apps
        n = len(starts)
        n_open = sum(1 for a in starts
                     if is_opener(a["outs"], a["np"], a["bf"]))
        leash = {
            "starts": n,
            "ip_per_start": sum(a["outs"] for a in starts) / n / 3,
            "np_per_start": _avg([a["np"] for a in starts if a["np"]]),
            # A man who opens is not a short starter with a noisy average —
            # it is his ROLE, and the relief-innings estimate must not
            # regress him toward the club's starter norm.
            "opener_rate": (n_open / n) if n else None,
            "is_opener": bool(n and n_open / n >= 0.5),
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
        bbt_by_date: Dict[str, List[int]] = {}     # [gb, fb, total] batted
        # Plate discipline, computed from the plate location vs that batter's
        # own zone. Z-Contact% is the highest-persistence skill metric in the
        # 2024-26 study that nothing else here exposes (year-over-year r=.71,
        # and 4th-best predictor of next-season SIERA).
        zone_by_date: Dict[str, List[int]] = {}    # [in zone, total]
        zcon_by_date: Dict[str, List[int]] = {}    # [contact, swings] in zone
        chase_by_date: Dict[str, List[int]] = {}   # [swings, pitches] o-zone
        # Actual wOBA allowed, so the xwOBA line has something to be a gap
        # FROM — xwOBA alone says how hard he was hit, not whether it cost
        # him. Uses the real wOBA weights already on the rows.
        woba_by_date: Dict[str, List[float]] = {}  # [value, denom]
        # Release geometry per start. Arm angle and extension are already
        # parsed and used nowhere over TIME, yet a slot that drifts across
        # starts is one of the few visible fatigue/injury tells a box score
        # never shows.
        arm_by_date: Dict[str, List[float]] = {}
        ext_by_date: Dict[str, List[float]] = {}
        # First-pitch strike rate. The count on a `_get_pitch_detail` row is
        # PRE-pitch, not post-pitch — verified on 5,702 cached pitches: 0-0 is
        # 24.7% of them (about one per plate appearance) and NO row anywhere
        # carries 4 balls or 3 strikes, both of which are impossible under a
        # post-pitch reading. The ABS parser's post-pitch convention comes
        # from the play-by-play API, which is a different source; the comment
        # here used to claim it applied to Statcast too and it does not.
        #
        # The old test was `(balls + strikes) == 1`, which selects the SECOND
        # pitch of each plate appearance. That accidentally gets the right
        # answer for the at-bats it sees — the pre-count on pitch two tells
        # you what pitch one was — but it silently drops every at-bat that
        # ENDED on the first pitch. Those are 11.3% of all first pitches and
        # every one of them is a strike (the ball was put in play), so both
        # numerator and denominator lost strikes and the rate read low:
        # 56.8% measured against a true 61.6% on the same 32,839 pitches.
        # League first-pitch-strike rate is ~61-62%, which is the corrected
        # number, not the old one.
        fstr_by_date: Dict[str, List[int]] = {}    # [strikes, first pitches]
        for r in rows:
            if r.get("arm_angle") is not None:
                arm_by_date.setdefault(r["date"], []).append(r["arm_angle"])
            if r.get("ext") is not None:
                ext_by_date.setdefault(r["date"], []).append(r["ext"])
            b, s = r.get("balls"), r.get("strikes")
            if b == 0 and s == 0:
                f = fstr_by_date.setdefault(r["date"], [0, 0])
                f[0] += r["desc"] not in _NON_STRIKE_DESCS
                f[1] += 1
            if r.get("woba_d"):
                wv = woba_by_date.setdefault(r["date"], [0.0, 0.0])
                wv[0] += r.get("woba_v") or 0.0
                wv[1] += r["woba_d"]
            n_csw, n_p = csw_by_date.setdefault(r["date"], [0, 0])
            csw_by_date[r["date"]] = [n_csw + (r["desc"] in self._CSW_DESCS),
                                      n_p + 1]
            if r["desc"] in _SWING_DESCS:
                w = whiff_by_date.setdefault(r["date"], [0, 0])
                w[1] += 1
                if r["desc"] in _WHIFF_DESCS:
                    w[0] += 1
            in_zone = self._in_zone(r)
            if in_zone is not None:
                z = zone_by_date.setdefault(r["date"], [0, 0])
                z[0] += in_zone
                z[1] += 1
                if in_zone:
                    if r["desc"] in _SWING_DESCS:
                        c = zcon_by_date.setdefault(r["date"], [0, 0])
                        c[1] += 1
                        c[0] += r["desc"] not in _WHIFF_DESCS
                else:
                    ch = chase_by_date.setdefault(r["date"], [0, 0])
                    ch[1] += 1
                    ch[0] += r["desc"] in _SWING_DESCS
            if r["xwoba"] is not None:
                xw_by_date.setdefault(r["date"], []).append(r["xwoba"])
            if r["ev"] is not None:
                bb = bip_by_date.setdefault(r["date"], [[], 0, 0])
                bb[0].append(r["ev"])
                bb[1] += r["ev"] >= 95
                if r["la"] is not None:
                    bb[2] += _is_barrel(r["ev"], r["la"])
            # Batted-ball mix: GB% / FB% allowed (popups count as fly balls,
            # FanGraphs-style; line drives sit in the denominator only)
            bt = r.get("bb_type")
            if bt in ("ground_ball", "fly_ball", "line_drive", "popup"):
                c = bbt_by_date.setdefault(r["date"], [0, 0, 0])
                c[2] += 1
                if bt == "ground_ball":
                    c[0] += 1
                elif bt in ("fly_ball", "popup"):
                    c[1] += 1
        # Damage splits: times-through-order + platoon (vs LHB/RHB), same
        # per-PA aggregation off the event-ending rows
        def _bin():
            return {"pa": 0, "k": 0, "bb": 0, "hr": 0, "wv": 0.0, "wd": 0.0,
                    "xw": [], "ev": [], "hard": 0, "brl": 0, "bip": 0}

        tto_bins: Dict[int, dict] = {}
        hand_bins: Dict[str, dict] = {}
        base_bins: Dict[str, dict] = {}
        for r in rows:
            if not r["event"]:
                continue
            bins = [tto_bins.setdefault(min(int(r["tto"] or 1), 3), _bin())]
            if r.get("stand") in ("L", "R"):
                bins.append(hand_bins.setdefault(r["stand"], _bin()))
            bins.append(base_bins.setdefault(
                "Men on" if r.get("men_on") else "Empty", _bin()))
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

        # Label carries "TTO" so a bare 1/2/3+ beside the platoon rows isn't
        # ambiguous about what the number counts
        tto = [_row(f"TTO {t}{'+' if t == 3 else ''}", tto_bins[t])
               for t in sorted(tto_bins)]
        tto += [_row(f"vs {h}", hand_bins[h]) for h in ("L", "R")
                if h in hand_bins]
        # Windup vs stretch. Also levels this table with the starts table
        # beside it (5 rows came up 46px short of its 6).
        tto += [_row(b, base_bins[b]) for b in ("Empty", "Men on")
                if b in base_bins]
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
            "gb": {d: (gb / tot if tot else None)
                   for d, (gb, _fb, tot) in bbt_by_date.items()},
            "fb": {d: (fb / tot if tot else None)
                   for d, (_gb, fb, tot) in bbt_by_date.items()},
            "zone": {d: (z / t if t else None)
                     for d, (z, t) in zone_by_date.items()},
            "zcon": {d: (c / s if s else None)
                     for d, (c, s) in zcon_by_date.items()},
            "chase": {d: (sw / p if p else None)
                      for d, (sw, p) in chase_by_date.items()},
            # actual wOBA allowed — the line xwOBA is a gap from
            "woba": {d: (v / dn) for d, (v, dn) in woba_by_date.items() if dn},
            "fstr": {d: (s / n if n else None)
                     for d, (s, n) in fstr_by_date.items()},
            "arm": {d: _avg(vs) for d, vs in arm_by_date.items()},
            "ext": {d: _avg(vs) for d, vs in ext_by_date.items()},
            "tto": tto,
        }

    @staticmethod
    def _in_zone(r: dict) -> Optional[bool]:
        """True/False if this pitch's location and the batter's zone are both
        known, else None (so unknowns stay out of the denominator).

        STRICT rulebook zone — ball centre over the plate, no ball-radius
        widening. Widening it by a radius put Zone% ~6.5pp above FanGraphs and
        pulled Chase% ~2.5pp below, because the near-edge pitches hitters
        chase most got counted as strikes instead. Verified against the FG
        board: Warren .871 Z-Con / .401 Zone, Imanaga .832 / .401."""
        px, pz = r.get("plate_x"), r.get("plate_z")
        top, bot = r.get("sz_top"), r.get("sz_bot")
        if px is None or pz is None or top is None or bot is None:
            return None
        return abs(px) <= _ZONE_HALF_W_FT and bot <= pz <= top

    async def get_sp_movement(self, session: aiohttp.ClientSession,
                              pid: int) -> dict:
        """Per-pitch-type movement, plate location and release point for the
        SP shape plots, off the cached pitch detail: {'pitches': [{pitch, n,
        velo, mean_hb, mean_ivb, mv: [(hb_in, ivb_in), ...], loc: [(px_ft,
        pz_ft), ...], rel: [(rx_ft, rz_ft), ...], mean_rel_x, mean_rel_z,
        ext}, ...] usage-sorted, 'sz_top': ft, 'sz_bot': ft}.
        hb/ivb are pfx_x/pfx_z in inches (catcher's view); samples are
        thinned to ~120 points per pitch. rel/ext feed the delivery plot —
        arm-slot consistency and how far off the rubber he lets go."""
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
                                                 "velo": [], "kin": [],
                                                 "rel": [], "ext": []})
            b["mv"].append((r["pfx_x"] * 12, r["pfx_z"] * 12))
            if r["velo"] is not None:
                b["velo"].append(r["velo"])
            if r.get("plate_x") is not None and r.get("plate_z") is not None:
                b["loc"].append((r["plate_x"], r["plate_z"]))
            if (r.get("release_pos_x") is not None
                    and r.get("release_pos_z") is not None):
                b["rel"].append((r["release_pos_x"], r["release_pos_z"]))
            if r.get("ext") is not None:
                b["ext"].append(r["ext"])
            if all(r.get(k) is not None for k in KIN):
                b["kin"].append([r[k] for k in KIN])
        pitches = []
        for name, b in sorted(by_pitch.items(), key=lambda kv: -len(kv[1]["mv"])):
            mv, loc, rel = b["mv"], b["loc"], b["rel"]
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
                "rel": rel[::max(1, len(rel) // 120)],
                "mean_rel_x": _avg([p[0] for p in rel]) if rel else None,
                "mean_rel_z": _avg([p[1] for p in rel]) if rel else None,
                "ext": _avg(b["ext"]) if b["ext"] else None,
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

    async def get_manager_tendencies(self, session: aiohttp.ClientSession,
                                     team_abbr: str) -> Optional[dict]:
        """How this team's manager actually runs a game — hook depth, pen
        churn, how much relief work the pen is asked for — from a season of
        StatsAPI play-by-play (~110 games, ~3s).

        The raw play-by-play is deliberately NOT routed through _get_json:
        that dev-caches every response, and 110 games x ~0.6MB per team would
        bury savedata/devcache. Only the small aggregate is cached."""
        if not team_abbr:
            return None
        cached = self._mgr_tend.get(team_abbr)
        if cached and time.time() - cached[0] < FG_CACHE_TTL:
            return cached[1]
        # v5: the cached shape has gained `decisions`, `by_pitcher`,
        # `rv_events`, inherited-runner counts, `sp_bf_list`,
        # `opener_rate`/`sp_ip_trad`, and now `abs`. The key MUST carry a
        # schema version —
        # without it, dev-cache entries written by an older build silently
        # deserialise with missing fields (CHC and NYY rendered blank
        # offensive columns exactly this way, and a later build made the Hook
        # curve read "no data" while every other view worked). Bump it in the
        # SAME edit that changes the returned dict; the symptom is never an
        # error, always a silently empty feature.
        # NOTE the key said v4 while the comment above described v5 — the
        # bump was written down and not applied, which is precisely how a
        # pre-`abs` entry gets deserialised into the new shape.
        dc_key = f"mgr_tend_v5_{team_abbr}_{self.season}"
        dc = dev_cache_get(dc_key) or slate_cache_get(dc_key)
        if dc is not None:
            try:
                data = json_loads(dc)
            except ValueError:
                data = None
            if data:
                self._mgr_tend[team_abbr] = (time.time(), data)
                return data

        team_id = next((tid for tid, ab in self._teams.items()
                        if ab == team_abbr), None)
        if team_id is None:
            return None
        sched = await self._get_json(
            session, f"{STATS_BASE}/schedule",
            {"sportId": "1", "season": str(self.season),
             "teamId": str(team_id), "gameType": "R",
             "fields": SEASON_SCHED_FIELDS})
        games = _final_games(sched)
        if not games:
            return None

        ck = await self._ingest_pbp(session, games)
        data = _tendencies_from_acc(ck["teams"].get(team_id))
        if data:
            self._mgr_tend[team_abbr] = (time.time(), data)
            blob = json.dumps(data)
            dev_cache_put(dc_key, blob)
            slate_cache_put(dc_key, blob)
        return data

    # ------------------------------------------- season play-by-play walk

    def _pbp_ck_load(self) -> dict:
        """Load (once per session) the season checkpoint, with the league
        tables it was built from. See PBP_CK_VERSION for why the tables and
        the gamePk set are one file."""
        if self._pbp_ck is not None:
            return self._pbp_ck
        ck: dict = {"games": set(), "teams": {}}
        p = _pbp_ck_path(self.season)
        if SLATE_CACHE and p.exists():
            try:
                raw = json_loads(p.read_bytes())
                for tid, a in (raw["teams"] or {}).items():
                    ck["teams"][int(tid)] = {
                        "stints": a["stints"],
                        "dec": defaultdict(float, a["dec"]),
                        "rv": a["rv"],
                        "abs": defaultdict(float, a["abs"]),
                    }
                self._re24_acc = _decode_tuple_keyed(raw["re24"])
                self._we_acc = _decode_tuple_keyed(raw["we_acc"])
                self._we_trans = {(tuple(a), tuple(b)): n
                                  for a, b, n in raw["we_trans"]}
                self._tto_acc = {int(t): _decode_tuple_keyed(v)
                                 for t, v in raw["tto"]}
                self._ump_acc = {int(k): v for k, v in raw["ump"]}
                ck["games"] = set(raw["games"])
                print(f"EffortMLB: play-by-play checkpoint — "
                      f"{len(ck['games'])} games already folded in "
                      f"({len(self._we_trans)} WE transitions)")
            except Exception as e:
                # Partial state is worse than none: a half-decoded file
                # would leave league tables counting games the set no longer
                # claims. Throw ALL of it away and rebuild.
                print(f"EffortMLB: play-by-play checkpoint unreadable ({e}); "
                      "rebuilding from scratch")
                ck = {"games": set(), "teams": {}}
                self._re24_acc, self._we_acc, self._we_trans = {}, {}, {}
                self._tto_acc, self._ump_acc = {}, {}
        self._pbp_ck = ck
        return ck

    def _pbp_ck_save(self) -> None:
        ck = self._pbp_ck
        if not SLATE_CACHE or not ck:
            return
        try:
            payload = {
                "games": sorted(ck["games"]),
                "teams": {str(t): {"stints": a["stints"], "dec": dict(a["dec"]),
                                   "rv": a["rv"], "abs": dict(a["abs"])}
                          for t, a in ck["teams"].items()},
                "re24": _encode_tuple_keyed(self._re24_acc),
                "we_acc": _encode_tuple_keyed(self._we_acc),
                "we_trans": [[list(a), list(b), n]
                             for (a, b), n in self._we_trans.items()],
                "tto": [[t, _encode_tuple_keyed(v)]
                        for t, v in self._tto_acc.items()],
                "ump": [[str(k), v] for k, v in self._ump_acc.items()],
            }
            p = _pbp_ck_path(self.season)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(p)                    # never a half-written checkpoint
        except Exception as e:
            print(f"EffortMLB: play-by-play checkpoint write failed: {e}")

    async def _ingest_pbp(self, session: aiohttp.ClientSession,
                          games: List[tuple],
                          progress: Optional[Callable] = None) -> dict:
        """Fold every game in `games` the checkpoint has NOT already seen
        into the per-club accumulators and the league tables, and return the
        checkpoint.

        This is the only place play-by-play is downloaded. Each gamePk is
        fetched exactly once ever — a FINAL game's play-by-play is immutable,
        so on the day after a full walk this makes ~15 requests, not ~1,650.

        The raw play-by-play is deliberately NOT routed through _get_json:
        that dev-caches every response, and a season at ~0.115MB each would
        bury savedata/devcache. Only the derived aggregate is persisted.

        Serialised on `_pbp_lock`. The manager board fans out over thirty
        clubs at once and every club's schedule overlaps every other's — two
        ingests running together would both fetch the same gamePk before
        either marked it seen, and fold it into the league tables TWICE. The
        lock costs nothing in the normal case: the second caller wakes up to
        find its games already folded in and returns without a request."""
        async with self._pbp_lock:
            return await self._ingest_pbp_locked(session, games, progress)

    async def _ingest_pbp_locked(self, session: aiohttp.ClientSession,
                                 games: List[tuple],
                                 progress: Optional[Callable] = None) -> dict:
        ck = self._pbp_ck_load()
        seen = ck["games"]
        todo = [g for g in games if g[0] not in seen]
        if not todo:
            return ck
        done = [0]

        async def one(gp, hid, aid):
            async with self._sem:
                try:
                    async with session.get(
                            f"{STATS_BASE}/game/{gp}/playByPlay",
                            params={"fields": PBP_FIELDS},
                            timeout=aiohttp.ClientTimeout(total=45)) as resp:
                        if resp.status != 200:
                            return
                        pbp = json_loads(await resp.read())
                except Exception:
                    return
            stints = pitching_stints(pbp, hid, aid)
            dec = decision_counts(pbp, hid, aid)
            rvs = rv_events_from_pbp(pbp, hid, aid)
            chg = abs_challenges(pbp, hid, aid)
            for tid in (hid, aid):
                a = ck["teams"].get(tid)
                if a is None:
                    a = ck["teams"][tid] = {
                        "stints": [], "dec": defaultdict(float),
                        "rv": [], "abs": defaultdict(float)}
                a["stints"].extend(s for s in stints if s["team"] == tid)
                for k, v in dec.get(tid, {}).items():
                    a["dec"][k] += v
                a["rv"].extend(rvs.get(tid, []))
                for k, v in chg.get(tid, {}).items():
                    a["abs"][k] += v
            # League tables, ONCE per game — `seen` is what guarantees it,
            # for both clubs of this game and for every later run.
            re24_from_pbp(pbp, self._re24_acc)
            we_from_pbp(pbp, self._we_acc, self._we_trans)
            tto_from_pbp(pbp, self._tto_acc)
            umpire_game_stats(pbp, gp, self._ump_acc)
            # Marked only after every accumulator has taken it: a game that
            # errored out above must be retried next run, not skipped.
            seen.add(gp)
            done[0] += 1
            if progress and done[0] % 25 == 0:
                progress(done[0], len(todo))

        await asyncio.gather(*(one(*g) for g in todo))
        self._pbp_ck_save()
        return ck

    async def prefetch_manager_tendencies(self, session: aiohttp.ClientSession,
                                          progress: Optional[Callable] = None
                                          ) -> None:
        """Build EVERY club's manager tendencies in ONE league-wide pass.

        Two things this avoids. A game has TWO clubs, so thirty per-club
        walks download the season's ~1,650 games ~3,300 times — here each
        gamePk is dispatched to both clubs' accumulators from a single fetch.
        And the season checkpoint means only games never folded in before are
        fetched at all, so the day after a full walk costs ~15 games.

        Same per-club output and same cache keys as the per-club method, so a
        warm start is unaffected and that method still works standalone."""
        if not self._teams:
            return

        # ONE league schedule, not thirty team schedules. It is fetched even
        # when every club is cached — it is one small request, and it is how
        # we learn which gamePks are new.
        sched = await self._get_json(
            session, f"{STATS_BASE}/schedule",
            {"sportId": "1", "season": str(self.season), "gameType": "R",
             "fields": SEASON_SCHED_FIELDS})
        games = _final_games(sched)
        if not games:
            return

        ck = await self._ingest_pbp(session, games, progress)

        # Rebuilt for every club from the in-memory accumulators (cheap, no
        # I/O) rather than only for clubs that missed the cache — otherwise
        # last night's games would sit in the checkpoint unreported.
        for tid, abbr in self._teams.items():
            data = _tendencies_from_acc(ck["teams"].get(tid))
            if not data:
                continue
            self._mgr_tend[abbr] = (time.time(), data)
            blob = json.dumps(data)
            dc_key = f"mgr_tend_v5_{abbr}_{self.season}"
            dev_cache_put(dc_key, blob)
            slate_cache_put(dc_key, blob)

    # ---------------------------------------------------- slate persistence

    def save_league_tables(self):
        """Persist the league tables the play-by-play walk builds.

        These MUST travel with the set of games they were summed over, which
        is why they live in the season checkpoint rather than a table of
        their own: restoring tables built from one set of gamePks next to a
        game set claiming another would double-count or drop games, and the
        symptom would be quietly wrong run expectancy, not an error.

        `_ingest_pbp` already saves after folding in new games; this stays so
        the board's end-of-load call is still correct (and cheap — it rewrites
        the same file)."""
        self._pbp_ck_save()

    def load_league_tables(self) -> bool:
        """Restore the league tables from the season checkpoint.

        A club served from the slate cache returns without walking any
        play-by-play, so on a warm start nothing else would populate RE24,
        the win-expectancy grid, the TTO bins or the umpire tallies — the
        manager board would come up instantly with the leverage,
        run-expectancy and umpire features silently empty."""
        self._pbp_ck_load()
        return bool(self._re24_acc)

    def run_expectancy(self, min_n: int = 200) -> Dict[tuple, float]:
        """League RE24 table built from whatever play-by-play has been seen:
        {(bases, outs): expected runs to the end of the half-inning}. Cells
        under `min_n` samples are dropped rather than believed."""
        return {k: v[0] / v[1] for k, v in self._re24_acc.items()
                if v[1] >= min_n}

    def leverage_index(self) -> Dict[tuple, float]:
        """Empirical Leverage Index per game state (league average = 1.0)."""
        return build_leverage(self._we_acc, self._we_trans)

    async def warm_we_surface(self):
        """Precompute the win-expectancy surface on a worker thread.

        `annotate_win_expectancy` is synchronous and memoises the fit on
        `_we_surface`, so warming that cache here means the Replay tab finds
        it ready instead of running a ~190ms sklearn fit on the UI thread —
        measured as the single largest named stall during startup, landing
        mid-animation.

        Worth offloading precisely BECAUSE it is sklearn: the fit spends its
        time in BLAS with the GIL released, unlike the play-by-play
        `json.loads` where threading bought nothing.

        Call once the league tables are FINAL. The cache key is the table
        sizes, so warming against tables that then grow just refits later.
        """
        acc, trans = self._we_acc, self._we_trans
        if not acc:
            return
        sig = (len(acc), len(trans))
        cached = getattr(self, "_we_surface", None)
        if cached and cached[0] == sig:
            return

        def _fit():
            states = {k for pair in trans for k in pair} | set(acc)
            we = smooth_win_expectancy(acc, states)
            if not we:
                we = {k: v[0] / v[1] for k, v in acc.items() if v[1] >= 30}
            return we, build_leverage(acc, trans)

        try:
            we, li = await asyncio.get_event_loop().run_in_executor(None, _fit)
        except Exception as e:
            print(f"EffortMLB: WE surface warm failed: {e}")
            return
        self._we_surface = (sig, we, li)

    def tto_run_values(self) -> Dict[int, float]:
        """Runs allowed per batter faced by a STARTER, by times through the
        order — measured off our own RE24, not imported."""
        return tto_penalty(self._tto_acc, self.run_expectancy())

    async def get_umpire_map(self, session: aiohttp.ClientSession
                             ) -> Dict[int, str]:
        """{gamePk: home-plate umpire} for the season so far.

        The play-by-play feed does not name the officials, but the SCHEDULE
        feed hydrates them for any date range — so the whole season costs one
        request, not one per game."""
        if self._ump_by_game and time.time() - self._ump_by_game[0] < 6 * 3600:
            return self._ump_by_game[1]
        year = datetime.now().year
        data = await self._get_json(session, f"{STATS_BASE}/schedule", {
            "sportId": "1", "gameType": "R",
            "startDate": f"{year}-03-01",
            "endDate": datetime.now().strftime("%Y-%m-%d"),
            "hydrate": "officials",
            # only the plate umpire's name is read out of this, but the
            # untrimmed season-with-officials is 3.1MB and 27ms to parse on
            # the loop; the whitelist takes it to 0.54MB / 4ms for a
            # verified-identical map. `hydrate` still has to stay — fields
            # filters what comes back, it cannot add officials.
            "fields": UMPIRE_SCHED_FIELDS})
        out: Dict[int, str] = {}
        for d in (data or {}).get("dates", []):
            for g in d.get("games", []):
                for o in (g.get("officials") or []):
                    if o.get("officialType") == "Home Plate":
                        nm = (o.get("official") or {}).get("fullName")
                        if nm:
                            out[g["gamePk"]] = nm
        self._ump_by_game = (time.time(), out)
        return out

    async def umpire_profiles(self, session: aiohttp.ClientSession
                              ) -> Dict[str, dict]:
        """Per-umpire zone profiles built from play-by-play already on hand."""
        return build_umpire_profiles(self._ump_acc,
                                     await self.get_umpire_map(session))

    def tonight_umpire(self, game: dict) -> Optional[str]:
        """Home-plate umpire for a scheduled game (officials hydrate)."""
        for o in (game.get("officials") or []):
            if o.get("officialType") == "Home Plate":
                return (o.get("official") or {}).get("fullName")
        return None

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
        """Shared Savant CSV fetch (BOM-stripped), cached per URL.

        Most callers wrap this in a board-level cache of their own, but
        `get_team_defense` did not — and it is called once per club, so the
        league-wide OAA board was downloaded and re-parsed 61 times in a
        single session (measured: 1.37MB and 61 csv.DictReader passes on the
        loop thread). Caching HERE fixes it for every board at once.

        The per-URL lock is the other half: without it the concurrent callers
        all miss the empty cache together and fetch in parallel, which is why
        the insidethepen page was being pulled twice. Rows are handed out by
        reference — every caller only reads them.
        """
        now = time.time()
        hit = self._savant_csv.get(url)
        if hit and now - hit[0] < PITCH_SPLITS_TTL:
            return hit[1]
        lock = self._savant_csv_locks.setdefault(url, asyncio.Lock())
        async with lock:
            hit = self._savant_csv.get(url)
            if hit and time.time() - hit[0] < PITCH_SPLITS_TTL:
                return hit[1]
            text = dev_cache_get(url)
            if text is None:
                async with self._sem:
                    try:
                        async with session.get(url, headers=SAVANT_HEADERS,
                                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status != 200:
                                print(f"EffortMLB: savant csv {resp.status} {url[:80]}")
                                return []
                            text = (await resp.text()).lstrip("﻿")
                    except Exception as e:
                        print(f"EffortMLB: savant csv failed: {e}")
                        return []
                dev_cache_put(url, text)
            import io as _io
            rows = list(csv.DictReader(_io.StringIO(text)))
            self._savant_csv[url] = (time.time(), rows)
            return rows

    async def get_fielding_runs(self, session: aiohttp.ClientSession
                                ) -> Dict[str, dict]:
        """Per-fielder arm and double-play runs, keyed by MLBAM id.

        This board is the only source for the two skills Outs Above Average
        does not measure at all — OAA is catch probability, so an outfielder
        who holds the runner at first is invisible to it. There is no CSV
        endpoint; the numbers sit in a `const data = [...]` literal in the
        page source (note: `const`, not the `var` the park-factor page uses)."""
        if self._frv_cache is not None:
            return self._frv_cache
        async with self._frv_lock:
            if self._frv_cache is not None:
                return self._frv_cache
            return await self._fetch_fielding_runs(session)

    async def _fetch_fielding_runs(self, session: aiohttp.ClientSession
                                   ) -> Dict[str, dict]:
        """Uncached body of `get_fielding_runs` — call through that."""
        out: Dict[str, dict] = {}
        try:
            async with self._sem:
                async with session.get(
                        SAVANT_FRV_URL, headers=SAVANT_HEADERS,
                        timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    html = await resp.text() if resp.status == 200 else ""
            import re as _re
            m = _re.search(r"const data = (\[.*?\]);", html, _re.S)
            for r in (json.loads(m.group(1)) if m else []):
                out[str(r.get("id"))] = {
                    "arm": r.get("arm_runs") or 0.0,
                    "dp": r.get("dp_runs") or 0.0,
                    "range": r.get("range_runs") or 0.0,
                    "total": r.get("total_runs") or 0.0,
                }
        except Exception as e:
            print(f"EffortMLB: fielding run value unavailable: {e}")
        self._frv_cache = out
        return out

    async def get_team_catcher(self, session: aiohttp.ClientSession,
                               team_abbr: str,
                               lineup_ids: Optional[set] = None
                               ) -> Optional[dict]:
        """Tonight's receiver and what he is worth.

        Deliberately NOT folded into the defence advantage: framing acts on
        called strikes, so it moves strikeouts and walks, not what happens to
        a ball in play. Adding it to an outs-on-contact number would be a
        category error even though it is the larger effect of the two."""
        if not team_abbr or not self._roster:
            return None
        tid = next((t for t, ab in (self._teams or {}).items()
                    if ab == team_abbr), None)
        if tid is None:
            return None
        cands = [r for r in self._roster.values()
                 if r.get("team_id") == tid and r.get("position") == "C"]
        if not cands:
            return None
        if lineup_ids:
            starting = [r for r in cands if r["id"] in lineup_ids]
            if starting:
                cands = starting
        best, best_rec = None, None
        for r in cands:
            try:
                d = await self.get_catcher_defense(session, r["id"])
            except Exception:
                d = None
            if not d:
                continue
            # with no posted card, the man who has caught the most is the
            # least-bad guess at who catches tonight
            seen = d.get("framed_pitches") or 0
            if best is None or seen > best:
                best, best_rec = seen, dict(d, name=r["name"], id=r["id"])
        return best_rec

    async def all_team_defense(self, session: aiohttp.ClientSession
                               ) -> Dict[str, Dict[str, dict]]:
        """Every club's primary alignment, cached. Used to rank one club's
        gloves against the other 29 for a given pitcher's contact."""
        if self._all_team_def:
            return self._all_team_def
        out = {}
        for abbr in sorted(set((self._teams or {}).values())):
            d = await self.get_team_defense(session, abbr)
            if d:
                out[abbr] = d
        self._all_team_def = out
        return out

    async def league_zone_defense(self, session: aiohttp.ClientSession
                                  ) -> Dict[tuple, float]:
        """League-average defensive value per contact zone — the baseline the
        advantage is measured against.

        It is NOT zero even though OAA is centred: averaged over the 30 clubs'
        primary alignments it runs about -0.93 for grounders up the middle to
        +1.15 in the 5-6 hole, because the zones draw on different gloves and
        different directional skills. Subtracting it is what makes `adv` mean
        'versus a generic defence' rather than 'versus nothing'."""
        if self._lg_zone_def:
            return self._lg_zone_def
        try:
            teams = sorted(set((self._teams or {}).values()))
            acc: Dict[tuple, List[float]] = defaultdict(list)
            mix = (_LG_RHH_SHARE, 1.0 - _LG_RHH_SHARE)
            for abbr in teams:
                dfn = await self.get_team_defense(session, abbr)
                if not dfn:
                    continue
                for kind in ("gb", "air"):
                    for sec in _SECTORS:
                        for depth in (("",) if kind == "gb"
                                      else ("shallow", "deep")):
                            v = _zone_defense((kind, sec, depth), dfn, mix)
                            if v is not None:
                                acc[(kind, sec, depth)].append(v)
            self._lg_zone_def = {z: sum(v) / len(v)
                                 for z, v in acc.items() if v}
        except Exception as e:
            print(f"EffortMLB: league zone defence failed: {e}")
        return self._lg_zone_def or {}

    async def get_park_factors(self, venue: str) -> Optional[dict]:
        """Handed Statcast park factors for one venue: {'All'|'R'|'L': {...}}.

        Reuses MLBAnalytics/parkfactors.py rather than re-implementing the
        scrape — note its shipped parkFactors.csv is a stale 2023-2025 window,
        so this goes to the live page (plain requests, no Selenium) and caches
        for the session."""
        if not venue:
            return None
        if self._park_cache is None:
            loop = asyncio.get_running_loop()
            try:
                import sys as _sys
                _p = str(Path(__file__).resolve().parent.parent
                         / "MLBAnalytics")
                if _p not in _sys.path:
                    _sys.path.insert(0, _p)
                from parkfactors import fetch_park_factors
                cache: Dict[str, Dict[str, dict]] = {}
                for side in ("All", "R", "L"):
                    rows = await loop.run_in_executor(
                        None, fetch_park_factors, 3,
                        None if side == "All" else side)
                    for r in rows:
                        cache.setdefault(r["Venue"], {})[side] = r
                self._park_cache = cache
            except Exception as e:
                print(f"EffortMLB: park factors unavailable: {e}")
                self._park_cache = {}
        # venue names differ slightly between StatsAPI and Savant
        pc = self._park_cache
        if venue in pc:
            return pc[venue]
        # Prefix matching alone silently loses sponsor-renamed parks: the
        # board calls Dodger Stadium "UNIQLO Field at Dodger Stadium", so
        # neither name starts with the other and the Dodgers had NO live
        # factors at all (the card was falling back to the stale CSV).
        # Match on the distinctive words instead of on position.
        STOP = {"field", "park", "stadium", "at", "the", "of", "ballpark",
                "coliseum", "centre", "center"}

        def toks(s):
            return {w for w in
                    "".join(c if c.isalnum() or c.isspace() else " "
                            for c in (s or "").lower()).split()
                    if w not in STOP and len(w) > 2}

        vt = toks(venue)
        if vt:
            best, score = None, 0
            for k in pc:
                ov = len(vt & toks(k))
                if ov > score:
                    best, score = k, ov
            if best and score:
                return pc[best]
        key = next((k for k in pc if k.lower().startswith(venue.lower()[:12])
                    or venue.lower().startswith(k.lower()[:12])), None)
        return pc.get(key) if key else None

    async def get_team_defense(self, session: aiohttp.ClientSession,
                               team_abbr: str,
                               lineup_ids: Optional[set] = None,
                               posted_fielders: Optional[dict] = None
                               ) -> Dict[str, dict]:
        """This club's primary defender at each position, with his OAA and
        the directional / batter-hand splits.

        One league-wide CSV, cached like the other Savant boards. Savant keys
        the board by team NICKNAME ("Red Sox"), while the roster map is by
        full name ("Boston Red Sox"), so the join is a suffix match — an
        equality join silently drops all 30."""
        if not team_abbr:
            return {}
        rows = await self._fetch_savant_csv(
            session, SAVANT_OAA_URL.format(year=self.season))
        frv = await self.get_fielding_runs(session)
        try:
            arm_board = await self.get_arm_strength(session)
            jump_board = await self.get_of_jump(session)
        except Exception as e:
            print(f"EffortMLB: arm/jump boards failed: {e}")
            arm_board, jump_board = {}, {}
        nick_to_abbr = dict(self._team_name_to_abbr or {})
        out: Dict[str, dict] = {}

        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        for r in rows:
            nick = (r.get("display_team_name") or "").strip()
            ab = SAVANT_NICK_ALIAS.get(nick) or next(
                (a for full, a in nick_to_abbr.items()
                 if nick and full.endswith(nick)), None)
            if ab != team_abbr:
                continue
            pos = (r.get("primary_pos_formatted") or "").strip()
            if not pos:
                continue
            # When tonight's card is posted, price the gloves that are
            # ACTUALLY playing. The season primary at each position is a
            # different team: KC rested Witt (+18), Garcia (+6), Isbel (+4)
            # and Pasquantino behind Dobnak on 2026-07-29, and a
            # primaries-only read would have shown none of that.
            if lineup_ids is not None and int(r.get("player_id") or 0) \
                    not in lineup_ids:
                continue
            rec = {
                "name": r.get("last_name, first_name") or "",
                "pid": r.get("player_id"),
                "oaa": fnum(r.get("outs_above_average")),
                "frp": fnum(r.get("fielding_runs_prevented")),
                "rhh": fnum(r.get("outs_above_average_rhh")),
                "lhh": fnum(r.get("outs_above_average_lhh")),
                "in": fnum(r.get("outs_above_average_infront")),
                "back": fnum(r.get("outs_above_average_behind")),
                "to3b": fnum(r.get("outs_above_average_lateral_toward3bline")),
                "to1b": fnum(r.get("outs_above_average_lateral_toward1bline")),
            }
            # arm + DP, converted onto the OAA (outs) scale so they can ride
            # the same contact weighting as range
            ex = frv.get(str(r.get("player_id"))) or {}
            rec["arm"] = ex.get("arm")
            rec["dp"] = ex.get("dp")
            rec["aux"] = ((ex.get("arm") or 0.0)
                          + (ex.get("dp") or 0.0)) * FRV_RUNS_TO_OUTS
            # Raw arm speed and the jump COMPONENTS behind an outfielder's
            # OAA. They ride the record for the hover text rather than the
            # glove column, which is 64px wide and already full — and the
            # jump split is the useful part: two men at +5 OAA, one getting
            # there on reaction and one on route, are not the same fielder.
            try:
                rid = int(r.get("player_id"))
            except (TypeError, ValueError):
                rid = None
            if rid is not None:
                rec["arm_mph"] = (arm_board.get(rid) or {}).get("overall")
                jmp = jump_board.get(rid) or {}
                rec["jump"] = {k: jmp.get(k) for k in
                               ("reaction", "burst", "route")} if jmp else None
            # the board can carry two men at a position; keep the one with the
            # most defensive value recorded, as a stand-in for the regular
            prev = out.get(pos)
            if prev is None or abs(rec["oaa"] or 0) > abs(prev["oaa"] or 0):
                out[pos] = rec
        # A starter who clears neither board's playing-time minimum (Nick
        # Sogard, a utility man, is on NEITHER the OAA board nor
        # fielding-run-value) would otherwise just be absent — and his zones
        # then drop out of the weighted average entirely, so the rank is
        # quietly computed over six gloves instead of seven. Carry him as
        # explicitly UNPRICED instead of not carrying him at all.
        for pos, who in (posted_fielders or {}).items():
            if pos in out:
                continue
            pid = who.get("id")
            est = None
            rec = next((r for r in (self._roster or {}).values()
                        if r.get("id") == pid), None)
            try:
                fg = await self.get_fg_batting(int(pid)) if pid else None
            except (TypeError, ValueError):
                fg = None
            if fg:
                est = estimate_fielder(
                    _fnum(fg.get("defense")), _fnum(fg.get("pos")),
                    _fnum(fg.get("games")),
                    (rec or {}).get("position") or pos, pos)
            nm = who.get("name", "")
            if est is None:
                out[pos] = {"name": nm, "pid": pid, "oaa": None,
                            "unpriced": True}
            else:
                # no directional information exists for him, so every
                # direction prices at the same estimate
                out[pos] = {"name": nm, "pid": pid, "oaa": est, "est": True,
                            "to3b": est, "to1b": est, "in": est, "back": est,
                            "rhh": None, "lhh": None, "aux": 0.0}
        return out

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
                          QSize, pyqtSignal, QTimer, QElapsedTimer, QObject)
from PyQt6.QtGui import (QColor, QFont, QPixmap, QPainter, QPainterPath,
                         QIcon, QAction, QPen, QTextDocument, QPolygonF,
                         QFontMetrics, QRadialGradient)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    QGridLayout, QComboBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QScrollArea, QPushButton,
    QMenu, QToolButton, QLayout, QCheckBox, QLineEdit, QStackedWidget,
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


class UnitAxis(pg.AxisItem):
    """Axis whose unit rides on the TICK LABELS — `28°`, `7.3′` — instead of
    a rotated axis label beside them.

    A rotated label is the wrong vehicle for a one-glyph unit: at label size
    a bare degree sign all but vanishes, and a rotated prime reads as a stray
    tick mark. Suffixing the ticks is unambiguous, needs no explanation, and
    removes the label column entirely (which is the width it was costing).
    The full spelled-out unit lives in the crosshair readout."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._suffix = ""

    def set_suffix(self, suffix: str):
        if suffix == self._suffix:
            return
        self._suffix = suffix or ""
        self.picture = None          # force a repaint of the tick strings
        self.update()

    def tickStrings(self, values, scale, spacing):
        base = super().tickStrings(values, scale, spacing)
        if not self._suffix:
            return base
        return [f"{t}{self._suffix}" for t in base]


class StatMenuButton(QToolButton):
    """Compact 'Stats ▾' button that floats in a plot corner and opens a
    stay-open menu of checkable, color-swatched overlay entries. The
    QActions in .acts are drop-in isChecked()/setChecked()/toggled
    replacements for the old chip-row QPushButtons."""

    def __init__(self, overlays, parent=None, groups=None):
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
        # {first key of a group: heading}. Past ~15 entries a flat list stops
        # being scannable; headings are disabled actions, so they cost no
        # behaviour and cannot be toggled by mistake.
        heads = dict(groups or [])
        for key, label, color in overlays:
            if key in heads:
                if self.acts:
                    menu.addSeparator()
                h = QAction(heads[key], menu)
                h.setEnabled(False)
                menu.addAction(h)
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
        # Expand vertically so a stack of these fills the column height next to
        # the splits table (rows share the space via equal grid stretch); a
        # small minimum keeps them legible when the column is short.
        self.setMinimumHeight(15)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Expanding)
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


class FlowLayout(QLayout):
    """Left-to-right layout that wraps when it runs out of width.

    The detail tabs were a single ~470px column inside a 738px panel, so every
    tab carried ~270px of dead margin down its right edge and pushed its own
    content further down than it needed to. These blocks are all 327-471px
    wide, i.e. two fit side by side at the width the panel actually gets and
    one does not when the panel is narrow — which is exactly the case a flow
    layout exists for. Qt ships no such layout, so this is the standard
    implementation from its own examples, trimmed.
    """

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), test=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, test=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(self._effective(it))
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    @staticmethod
    def _effective(it):
        """The size the item will ACTUALLY be drawn at.

        `sizeHint()` alone is wrong here: `_fit_table` pins every table with
        `setFixedHeight`/`setFixedWidth`, and a QTableWidget's raw sizeHint
        ignores that — it reports its own natural height, which for these
        tables runs 100-150px larger. `setGeometry` then clamps the widget
        back to its fixed size while the row advanced by the unclamped hint,
        so a dead band appeared under EVERY row. Clamp to min/max and the
        rows pack tight."""
        hint = it.sizeHint()
        w = it.widget()
        if w is None:
            return hint
        return QSize(
            max(w.minimumWidth(), min(hint.width(), w.maximumWidth())),
            max(w.minimumHeight(), min(hint.height(), w.maximumHeight())))

    def _do(self, rect, test):
        """Pack items into the lowest space that fits, not into rows.

        ROW packing was the source of the "massive spacing between tables":
        every item in a row is pushed down by the TALLEST item in that row, so
        the 245px spray chart sitting beside a 130px table left ~115px of dead
        space under that table, and again under every short item it shared a
        row with. Nothing was misreporting its size — the arrangement itself
        wasted the space.

        This is a skyline (masonry) fill: each item goes at the leftmost x
        whose span is lowest, so a short table slides up under the previous
        short one instead of waiting for the tall neighbour to clear. Order is
        still preserved left-to-right, top-to-bottom, so the deliberate
        ordering comments elsewhere in this file still hold.
        """
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        sp = self.spacing()
        left, right = eff.x(), eff.right() + 1
        # skyline: [(x_start, x_end, height_y)], spanning the full width
        sky = [[left, right, eff.y()]]

        def top_over(x0, x1):
            """Lowest y an item spanning x0..x1 can sit at."""
            return max((s[2] for s in sky if s[1] > x0 and s[0] < x1),
                       default=eff.y())

        def occupy(x0, x1, y):
            """Raise the skyline over x0..x1 to y."""
            out = []
            for s in sky:
                if s[1] <= x0 or s[0] >= x1:
                    out.append(s)
                    continue
                if s[0] < x0:
                    out.append([s[0], x0, s[2]])
                if s[1] > x1:
                    out.append([x1, s[1], s[2]])
            out.append([x0, x1, y])
            out.sort()
            # merge equal-height neighbours so the list cannot grow unbounded
            merged = [out[0]]
            for s in out[1:]:
                if s[2] == merged[-1][2] and s[0] == merged[-1][1]:
                    merged[-1][1] = s[1]
                else:
                    merged.append(s)
            sky[:] = merged

        bottom = eff.y()
        for it in self._items:
            hint = self._effective(it)
            w = min(hint.width(), right - left)
            h = hint.height()
            # Candidate x positions are the skyline segment starts; pick the
            # one that sits highest, ties broken leftmost (so order reads
            # naturally rather than scattering).
            best = None
            for s in sky:
                x0 = s[0]
                if x0 + w > right:
                    x0 = right - w
                if x0 < left:
                    x0 = left
                y0 = top_over(x0, x0 + w)
                if best is None or y0 < best[1] - 0.5 or (
                        abs(y0 - best[1]) <= 0.5 and x0 < best[0]):
                    best = (x0, y0)
            x0, y0 = best
            if not test:
                it.setGeometry(QRect(QPoint(int(x0), int(y0)),
                                     QSize(w, h)))
            occupy(x0, min(x0 + w + sp, right), y0 + h + sp)
            bottom = max(bottom, y0 + h)
        return bottom - rect.y() + m.bottom()


class SplitBandChart(QWidget):
    """Every split as a dot against ITS OWN sampling band.

    This is the reason the splits view exists. A table of split rates invites
    exactly one reading — "he mashes with RISP" — that the sample almost never
    supports, and no amount of printing the PA column next to it fixes that,
    because a reader cannot convert 55 PA into "give or take 44 wRC+ points"
    in their head. So the conversion is the drawing: the grey band behind each
    row IS +-1 and +-2 sigma at that row's own PA (sd = sqrt(S/PA), measured —
    see SPLIT_NOISE_S), and the dot is where he actually sits.

    A dot inside the band is a split that has said nothing. Judge's 199 wRC+
    with RISP lands at +1.1 sigma — visibly ordinary, where the number alone
    reads as a headline.

    The vertical rule is the player's own SEASON line, not league average.
    The question a split answers is "is he different HERE than he usually is",
    and against league average every good hitter's splits are all to the right
    and the shape carries no information.
    """

    ROW_H = 15
    GROUP_GAP = 7
    LABEL_W = 74
    NUM_W = 40
    PAD = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._season = None
        self._today = set()
        self.setMinimumWidth(240)
        self.setToolTip(
            "Each dot is one split's wRC+; the bar behind it is that split's "
            "own sampling band (±1σ dark, ±2σ light) at its PA.\n\n"
            "The vertical rule is the player's SEASON line — the question is "
            "whether he is different in this split than he usually is, not "
            "whether he is good.\n\n"
            "A dot inside the dark band means the split has told you nothing: "
            "a hitter with a true talent equal to his season line would land "
            "there about two thirds of the time.\n\n"
            "Band width is sd = sqrt(107200 / PA), measured off the splits "
            "that cannot carry a real effect (month and inning). 50 PA gives "
            "±46 wRC+ points.\n\n"
            "Rows outlined in blue are the splits that apply to tonight.")

    def set_rows(self, rows: List[dict], season_wrc: Optional[float],
                 today: Optional[set] = None):
        self._rows = rows or []
        self._season = season_wrc
        self._today = set(today or ())
        n = len(self._rows)
        groups = len({r["group"] for r in self._rows})
        self.setMinimumHeight(
            max(60, n * self.ROW_H + max(0, groups - 1) * self.GROUP_GAP
                + 2 * self.PAD + 12))
        self.updateGeometry()
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#151a21"))
        if not self._rows or self._season is None:
            p.setPen(QColor("#7F8C8D"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "no split data")
            return

        left = self.PAD + self.LABEL_W
        right = self.width() - self.PAD - self.NUM_W
        if right - left < 60:
            return
        season = float(self._season)

        # Scale: widest 2-sigma reach on screen, so no band is ever clipped
        # and the bands stay comparable to one another.
        span = 20.0
        for r in self._rows:
            sd = r.get("sd") or 0
            w = r.get("wrc")
            span = max(span, 2.2 * sd)
            if w is not None:
                span = max(span, abs(float(w) - season) * 1.12)
        span = min(span, 220.0)

        def x_of(wrc):
            v = max(-span, min(span, float(wrc) - season))
            return left + (right - left) * (v + span) / (2 * span)

        mid_x = x_of(season)
        y = self.PAD
        last_group = None
        f_lab = QFont(); f_lab.setPointSize(7)
        f_num = QFont(); f_num.setPointSize(7); f_num.setBold(True)

        # season rule, full height, drawn under everything
        p.setPen(QPen(QColor("#4A6070"), 1, Qt.PenStyle.DashLine))
        p.drawLine(int(mid_x), self.PAD - 2, int(mid_x),
                   self.height() - self.PAD)

        for r in self._rows:
            if last_group is not None and r["group"] != last_group:
                y += self.GROUP_GAP
                p.setPen(QColor("#243140"))
                p.drawLine(self.PAD, y - self.GROUP_GAP // 2,
                           self.width() - self.PAD, y - self.GROUP_GAP // 2)
            last_group = r["group"]
            cy = y + self.ROW_H / 2
            sd = r.get("sd")
            wrc = r.get("wrc")
            is_today = r["split"] in self._today

            if sd:
                for k, col in ((2, QColor(52, 73, 94, 70)),
                               (1, QColor(52, 73, 94, 150))):
                    x0, x1 = x_of(season - k * sd), x_of(season + k * sd)
                    p.fillRect(QRectF(x0, cy - 4.5, x1 - x0, 9), col)

            if is_today:
                p.setPen(QPen(QColor("#3498DB"), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(self.PAD - 1, y, self.width() - 2 * self.PAD + 1,
                                  self.ROW_H - 1))

            p.setFont(f_lab)
            p.setPen(QColor("#EAF2F8" if is_today else "#95A5A6"))
            p.drawText(QRectF(self.PAD, y, self.LABEL_W - 4, self.ROW_H),
                       int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter),
                       r["split"])

            if wrc is not None:
                z = r.get("z")
                # Colour by SIGMAS, not by the raw gap: the raw gap is
                # dominated by whichever split has the fewest PA, which is
                # exactly backwards.
                if z is None:
                    col = QColor("#7F8C8D")
                elif abs(z) < 1:
                    col = QColor("#7F8C8D")
                elif z > 0:
                    col = QColor("#2ECC71" if z < 2 else "#27AE60")
                else:
                    col = QColor("#E74C3C" if z > -2 else "#C0392B")
                cx = x_of(wrc)
                p.setPen(QPen(col.darker(160), 1))
                p.setBrush(col)
                p.drawEllipse(QPointF(cx, cy), 3.4, 3.4)

                p.setFont(f_num)
                p.setPen(col if abs(z or 0) >= 1 else QColor("#BDC3C7"))
                p.drawText(QRectF(self.width() - self.PAD - self.NUM_W, y,
                                  self.NUM_W, self.ROW_H),
                           int(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter),
                           f"{wrc:.0f}")
            y += self.ROW_H
        p.end()


class FlowHost(QWidget):
    """Container for a FlowLayout that tells its PARENT how tall it needs to be.

    A plain QWidget does not. `FlowLayout` implements `heightForWidth`, but a
    layout's height-for-width is only consulted if the WIDGET advertises it in
    its size policy — and `FlowLayout.sizeHint` returns `minimumSize`, which is
    the largest single item rather than the height of the wrapped rows. So the
    enclosing QVBoxLayout sized the flow off one item and the rows that wrapped
    past that were laid out below the clip.

    On the Form tab that hid THREE tables outright: the flow needed 650px, the
    page gave it 416, and the direction / rest / attack tables were positioned
    off the bottom with no scrollbar to reach them — the panel's QScrollArea
    scrolls on MINIMUM size, and the minimum said 245.

    Pinning `minimumHeight` on every resize is what makes the scroll area
    aware there is more to show.
    """

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self.flow = FlowLayout(self, spacing=spacing)
        pol = self.sizePolicy()
        pol.setHeightForWidth(True)
        pol.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.setSizePolicy(pol)

    def addWidget(self, w):
        self.flow.addWidget(w)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self.flow.heightForWidth(w)

    def sizeHint(self):
        w = self.width() or 700
        return QSize(w, self.flow.heightForWidth(w))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._repin()

    def _repin(self):
        """Height the rows actually need at the CURRENT width."""
        h = self.flow.heightForWidth(max(1, self.width()))
        if h != self.minimumHeight():
            self.setMinimumHeight(h)
            self.updateGeometry()


class ZoneGrid(QWidget):
    """Painted strike-zone heat grid — catcher view, batter-relative.

    Three of these sit side by side: the hitter's WHIFF rate by zone, his
    SWING rate by zone, and where tonight's starter actually puts the ball.
    Whiff needs swing beside it — a high whiff rate in a cell he rarely
    offers at is not a hole, it is a take. Drawn rather than tabulated
    because the question is spatial — "is his hole where this guy lives" is a
    shape you see instantly and have to decode from a table.

    The out-of-zone bucket is the frame around the grid, which is where those
    pitches physically are.
    """

    def __init__(self, title: str, low: str, high: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._low, self._high = QColor(low), QColor(high)
        self._cells = None          # {"grid": 3x3, "oz": .., "total": ..}
        self._mode = "whiff"        # or "usage"
        self._sub = ""
        self.setMinimumSize(150, 132)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        self.setFixedHeight(132)

    def sizeHint(self):
        # 205: three of these plus two 6px gaps come to 627, which is what a
        # wide panel actually has. Without an explicit hint the flow falls
        # back to the 150 minimum and leaves ~250px empty beside them.
        return QSize(205, 132)

    def set_cells(self, cells, mode, sub=""):
        self._cells, self._mode, self._sub = cells, mode, sub
        self.update()

    def _value(self, c, in_zone=True):
        """(display value 0-1, label, enough-sample flag).

        Usage cells are a share of the starter's IN-ZONE pitches, so the nine
        of them sum to 100% and read against the same denominator as the
        matchup sentence and the table below. The chase frame is the only
        figure quoted against all pitches, which is what "he is out of the
        zone 58% of the time" naturally means."""
        if self._mode == "whiff":
            if c["sw"] < 8:
                return None, "—", False
            v = c["whiff"] / c["sw"]
            return v, f"{v:.0%}", c["sw"] >= 15
        if self._mode == "swing":
            # Denominator is PITCHES seen in the cell, not swings, so this is
            # the one grid whose sample is comfortable everywhere — ~200 a
            # cell on a full season against ~35 swings for the whiff grid.
            # That is why swing rate gets a grid and per-zone xwOBA does not.
            if c["pit"] < 8:
                return None, "—", False
            v = c["sw"] / c["pit"]
            return v, f"{v:.0%}", c["pit"] >= 20
        den = max(1, self._cells["in_zone"] if in_zone else self._cells["total"])
        v = c["pit"] / den
        return v, f"{v:.0%}", c["pit"] >= 8

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        f = QFont(); f.setPointSize(6)
        fb = QFont(); fb.setPointSize(6); fb.setBold(True)
        p.setFont(fb)
        p.setPen(QColor("#dc9437"))
        p.drawText(QRectF(2, 0, w - 4, 11), Qt.AlignmentFlag.AlignLeft,
                   self._title)
        if self._sub:
            p.setFont(f); p.setPen(QColor("#7F8C8D"))
            p.drawText(QRectF(2, 0, w - 4, 11), Qt.AlignmentFlag.AlignRight,
                       self._sub)
        if not self._cells:
            p.setFont(f); p.setPen(QColor("#4a5158"))
            p.drawText(QRectF(2, 16, w - 4, 12), Qt.AlignmentFlag.AlignLeft,
                       "no location data")
            p.end(); return

        # Scale the heat to THIS grid's own spread, so a hitter whose zone
        # whiff rates run 15-30% still shows shape. A fixed 0-100% ramp would
        # render every real grid as one flat mid-tone.
        vals = [v for row in self._cells["grid"] for c in row
                for v, _, ok in [self._value(c)] if v is not None and ok]
        lo, hi = (min(vals), max(vals)) if len(vals) >= 2 else (0.0, 1.0)
        if hi - lo < 1e-6:
            hi = lo + 1e-6

        pad, top = 14.0, 15.0
        # frame = out of zone; grid = the strike zone inside it
        gw = w - 2 * pad - 2
        gh = h - top - 16
        gx, gy = pad + 1, top
        ozv, ozl, _ = self._value(self._cells["oz"], in_zone=False)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1b2229"))
        p.drawRect(QRectF(1, top - 3, w - 2, gh + 6))
        # Chase rides on the TITLE line. Along the bottom it collided with the
        # "in" axis label, which sits under the left-hand column by
        # definition — there is no bottom-left corner free to put it in.
        p.setPen(QColor("#5D6D7E")); p.setFont(f)
        p.drawText(QRectF(2 + self.fontMetrics().horizontalAdvance(self._title)
                          + 8, 0, 70, 11),
                   Qt.AlignmentFlag.AlignLeft, f"chase {ozl}")

        cw, ch = gw / 3.0, gh / 3.0
        for r in range(3):
            for c in range(3):
                cell = self._cells["grid"][r][c]
                v, lab, ok = self._value(cell)
                x, y = gx + c * cw, gy + r * ch
                if v is None:
                    col = QColor("#232b33")
                else:
                    t = (v - lo) / (hi - lo)
                    t = max(0.0, min(1.0, t))
                    col = QColor(
                        int(self._low.red() + t * (self._high.red() - self._low.red())),
                        int(self._low.green() + t * (self._high.green() - self._low.green())),
                        int(self._low.blue() + t * (self._high.blue() - self._low.blue())))
                    if not ok:
                        col.setAlpha(110)      # thin sample, drawn faint
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRect(QRectF(x + 1, y + 1, cw - 2, ch - 2))
                p.setPen(QColor("#0d1117" if (v is not None and
                                              (v - lo) / (hi - lo) > 0.55)
                                else "#D5DBDB"))
                p.setFont(fb)
                p.drawText(QRectF(x, y, cw, ch),
                           Qt.AlignmentFlag.AlignCenter, lab)
        # zone outline
        p.setPen(QColor("#7F8C8D")); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(gx, gy, gw, gh))
        # axis hints: batter-relative, so they read the same for both hands
        p.setFont(f); p.setPen(QColor("#5D6D7E"))
        p.drawText(QRectF(gx, gy + gh + 1, gw / 3, 11),
                   Qt.AlignmentFlag.AlignHCenter, "in")
        p.drawText(QRectF(gx + 2 * gw / 3, gy + gh + 1, gw / 3, 11),
                   Qt.AlignmentFlag.AlignHCenter, "out")
        p.end()


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
        # Spray-adjusted hard hit — scored against the league's 75th-pct EV
        # at each ball's own direction. Sits next to HH% deliberately: it is
        # the same quantity with the pull bias taken out, and the two
        # diverging is itself the signal (a pull-only hitter reads high on
        # HH% and ordinary on DHH%). Stickier year to year, .831 -> .884.
        ("dhh",   "DHH%",     "#F1948A"),
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
        self._fg_split_data = None   # (league split boards, pid, season wRC+)
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

        # EffortMLB has no prop rows — the lineup rail is the click target.
        # (The string is inherited from the props window, where it was right.)
        self._placeholder = QLabel("Select a player from the lineup")
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

        # Left column: headshot on top, with the stat/line/window/filter
        # selectors tucked into the otherwise-empty space directly beneath it
        # (reclaims the full-width selector row that used to sit under the
        # whole header). Compact widths so nothing clips.
        head_left_col = QVBoxLayout()
        head_left_col.setSpacing(3)
        self._headshot = QLabel()
        self._headshot.setFixedSize(56, 56)
        self._headshot.setScaledContents(True)
        head_left_col.addWidget(self._headshot,
                                alignment=Qt.AlignmentFlag.AlignHCenter)

        # Four self-labelling controls in a tight 2×2 block under the headshot
        # (stat + line on top, game-window + venue below). No text labels —
        # values are self-evident (TB / 1.5 / L15 / All), tooltips carry the
        # rest — so the group stays narrow and hugs the left.
        AL = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        sel_grid = QGridLayout()
        sel_grid.setContentsMargins(0, 1, 0, 0)
        sel_grid.setHorizontalSpacing(3)
        sel_grid.setVerticalSpacing(3)
        self._stat_combo = QComboBox()
        # elide long market names rather than stretching the column
        self._stat_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._stat_combo.setMinimumContentsLength(5)
        self._stat_combo.setMaximumWidth(62)
        self._stat_combo.setToolTip("Prop stat")
        self._stat_combo.currentIndexChanged.connect(self._on_stat_controls_changed)
        self._line_spin = QDoubleSpinBox()
        self._line_spin.setRange(0.0, 500.0)
        self._line_spin.setSingleStep(0.5)
        self._line_spin.setDecimals(1)
        self._line_spin.setMaximumWidth(58)
        self._line_spin.setToolTip("Line")
        self._line_spin.valueChanged.connect(self._on_stat_controls_changed)
        # chart window / venue filter (client-side re-slice of the cached log)
        self._chart_window_combo = QComboBox()
        self._chart_window_combo.addItem("L15", 15)
        self._chart_window_combo.addItem("L30", 30)
        self._chart_window_combo.addItem("Season", 0)
        self._chart_window_combo.setMaximumWidth(62)
        self._chart_window_combo.setToolTip("Game window")
        self._chart_window_combo.currentIndexChanged.connect(
            self._on_chart_view_changed)
        self._chart_filter_combo = QComboBox()
        self._chart_filter_combo.addItem("All", "all")
        self._chart_filter_combo.addItem("Home", "home")
        self._chart_filter_combo.addItem("Road", "road")
        self._chart_filter_combo.setMaximumWidth(58)
        self._chart_filter_combo.setToolTip("Home / Road filter")
        self._chart_filter_combo.currentIndexChanged.connect(
            self._on_chart_view_changed)
        sel_grid.addWidget(self._stat_combo, 0, 0, AL)
        sel_grid.addWidget(self._line_spin, 0, 1, AL)
        sel_grid.addWidget(self._chart_window_combo, 1, 0, AL)
        sel_grid.addWidget(self._chart_filter_combo, 1, 1, AL)
        sel_grid.setColumnStretch(2, 1)   # soak up slack on the right
        head_left_col.addLayout(sel_grid)
        head_left_col.addStretch()
        top_row.addLayout(head_left_col)

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
        head_row.addLayout(header_left)

        # -- matchup strip: game/park/weather + opp SP. FULL WIDTH, on its own
        #    row under the header — NOT beside it.
        #
        #    It used to ride in the header's right-hand space with stretch=1,
        #    which sounds fine and was measurably not: the traditional grid
        #    wants 7 columns (~294px) and the headshot block ~120px, so in a
        #    529px panel the strip was left NINETY-SIX pixels. Its ~55 words
        #    then wrapped into a 15-line ribbon 489px tall, which in turn left
        #    a 427x364 dead rectangle beside it — 27% of the entire panel —
        #    and clipped the swing chips inside it to garbage (BMIELKE "130"
        #    rendered as "13", bat speed "78.3" as "78").
        #    Full width, the same text wraps to ~4 lines. Do not put this back
        #    beside the stat grid unless the panel gets much wider.
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
        self._matchup_frame.hide()

        # -- swing-tracking chips (batters): BMIELKE, bat speed, attack angle,
        #    fast-swing, squared-up, blast, length. Also its own full-width
        #    row. These were inside the matchup frame on an Ignored horizontal
        #    size policy — "so a long chip row never sets the panel's minimum
        #    width", which worked, at the cost of silently truncating every
        #    value once the frame was squeezed to 96px. Given the full panel
        #    width they fit outright, so the policy can go back to Preferred
        #    and the numbers can be read.
        self._swing_row = QWidget()
        self._swing_row.setSizePolicy(QSizePolicy.Policy.Preferred,
                                      QSizePolicy.Policy.Preferred)
        self._swing_lay = QGridLayout(self._swing_row)
        self._swing_lay.setContentsMargins(0, 2, 0, 1)
        self._swing_lay.setHorizontalSpacing(12)
        self._swing_lay.setVerticalSpacing(2)
        self._swing_row.hide()

        # The swing chips ride in the header's right-hand space, beside the
        # (now narrow) stat grid, instead of taking a full row of their own.
        # This is only safe because the grid was cut to 4 columns: at 7 it
        # left 96px here and silently truncated every chip value.
        head_row.addWidget(self._swing_row, stretch=1,
                           alignment=Qt.AlignmentFlag.AlignTop)
        content_lay.addLayout(head_row)
        content_lay.addWidget(self._matchup_frame)

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
        # Deliberately quiet. This is context, not the headline — it sits
        # directly above the trend plot, which is the thing worth looking at,
        # and a 56px block of saturated red/green with a dashed marker was
        # pulling the eye first. Shorter strip, translucent bars with no
        # outline, a faint reference line, and a small unfilled readout.
        self._banner.setFixedHeight(38)
        # Full-width strip just under the header: it runs from the left column
        # (under the headshot/selectors) all the way to the right edge of the
        # matchup box. Small minimum so it can still shrink on a narrow panel.
        self._banner.setMinimumWidth(140)
        # The Over x/x · line readout rides inside the banner's top-left
        # corner (over the oldest bars) instead of a separate side label
        # No fill — the opaque badge read as a second UI element sitting on
        # top of the chart. Plain text over the bars is quieter and, at this
        # height, does not collide with them.
        self._banner_text = pg.TextItem(anchor=(0, 0))
        self._banner_text.setZValue(50)
        # Its own full-width row between the header and the chart area
        content_lay.addWidget(self._banner)

        # -- analytical section: SUB-TABS, not a side-by-side row.
        #
        # This used to be [trend plot | flank of tables] in one QHBoxLayout,
        # which cannot work at the width this panel actually gets. MEASURED,
        # window 1548: the rail takes 158, the SP/charts pane 966, and this
        # whole panel 412 (viewport 394) — while its content wants 918. The
        # side-by-side split then reserved 272px for the plot and capped the
        # tables at 260, so the situational table hid 213px of its 11 columns,
        # the pitch matchup 211px of its 13, and the trend plot still only got
        # 160px. Everything lost.
        #
        # One thing at a time, each at the panel's full width, is the only
        # arrangement that fits: the tables need 373-471px and now get ~374.
        self._sections = QTabWidget()
        self._sections.setDocumentMode(True)
        # Switching sub-tab scrolls the panel back to the top. The panel sits
        # in a QScrollArea and the tabs are BELOW the header, so reading down
        # a long tab (the game log runs 24 rows) and then switching left the
        # new tab's content off-screen with the header scrolled away — which
        # reads as "the headshot and the stat grid have been removed". They
        # had not; they were simply above the viewport.
        self._sections.currentChanged.connect(self._scroll_to_top)
        self._sections.setStyleSheet(
            "QTabWidget::pane { border: 0; border-top: 1px solid #2C3E50;"
            " top: -1px; }"
            "QTabBar::tab { background: transparent; color: #7F8C8D;"
            " font-size: 8pt; padding: 3px 10px; border: 0;"
            " border-bottom: 2px solid transparent; }"
            "QTabBar::tab:selected { color: #ECF0F1;"
            " border-bottom: 2px solid #E67E22; }"
            "QTabBar::tab:hover { color: #BDC3C7; }")

        # FORM — the trend/spray stack. Gets ~374px here instead of 160.
        self._form_page = QWidget()
        self._form_lay = QVBoxLayout(self._form_page)
        self._form_lay.setContentsMargins(0, 4, 0, 0)
        self._form_lay.setSpacing(6)

        # PROFILE — percentile stack ABOVE the situational table, not beside
        # it: side by side they need 134 + 373 = 507 in a 394px viewport.
        # Stacked, the table gets its full natural width and stops scrolling.
        profile_page = QWidget()
        profile_lay = QVBoxLayout(profile_page)
        profile_lay.setContentsMargins(0, 4, 0, 0)
        profile_lay.setSpacing(8)
        pct_holder = QWidget()
        # HALF width. The bars ran the full panel, which made a percentile
        # rank look like a precision measurement and wasted the better half
        # of the row; paired with the discipline table they use the same
        # space and say more.
        pct_holder.setFixedWidth(self._PCT_STACK_W)
        self._pct_grid = QGridLayout(pct_holder)
        self._pct_grid.setContentsMargins(0, 0, 0, 0)
        self._pct_grid.setHorizontalSpacing(0)
        self._pct_grid.setVerticalSpacing(0)
        self._tbl_situ = self._make_stats_table()
        self._tbl_bb = self._make_stats_table()
        self._tbl_bb.setToolTip(
            "Batted-ball profile. Trajectory cuts are Statcast's: ground "
            "ball under 10°, line drive to 25°, fly ball to 50°, pop-up "
            "above.\n\n"
            "Spray is opposite-field-signed, so Pull/Cent/Oppo mean the same "
            "thing for a lefty and a righty.\n\n"
            "vL / vR are versus left- and right-handed pitchers. All values "
            "are percentages; the headers drop the % sign only to keep the "
            "table inside the panel width.\n\n"
            "Shift% is how often the infield was NOT in a standard alignment "
            "against him. Post-ban that is 'shade' and 'strategic' rather "
            "than a true shift, so read it as how much the defence bothers "
            "to move, not as the old shift rate.")
        self._tbl_disc = self._make_stats_table()
        self._tbl_disc.setToolTip(
            "Plate discipline, from the same zone geometry as the Zone tab "
            "(|plate_x| <= 0.83 ft and THIS batter's own sz_bot..sz_top), so "
            "the two views cannot disagree about what 'in the zone' means.\n\n"
            "Zone = share of pitches in the zone. Z-Sw/Z-Con = swing and "
            "contact rate on those. O-Sw = chase rate, O-Con = contact when "
            "he chases. 1st-Sw = swing rate on 0-0.\n\n"
            "These will not match the FanGraphs figures in the header to the "
            "decimal and should not — FanGraphs uses its own zone definition. "
            "Measured against it on Judge: Z-Con 83.4 vs 83.2, SwStr 12.9 vs "
            "12.8, chase 28.6 vs 29.1.")
        # Park- and league-adjusted index family. Everything else on this tab
        # is a RAW rate, which cannot answer "is that good, here" — a 39%
        # pull rate and a .193 ISO mean different things in Camden Yards than
        # in Oracle Park. These are the same quantities on wRC+'s scale.
        self._tbl_plus = self._make_stats_table()
        self._tbl_plus.setToolTip(
            "FanGraphs' park- and league-adjusted index family. 100 = league "
            "average AFTER the park correction, in every row — the same "
            "convention as wRC+.\n\n"
            "These are INDICES, not percentages: Hard+ 136 means 36% more "
            "hard contact than league average for his park, not 136% hard "
            "contact.\n\n"
            "Read against the raw rate beside it. The two diverging is the "
            "park: a hitter whose raw pull rate is ordinary but whose Pull+ "
            "is high pulls more than his park's hitters do.\n\n"
            "Directionality is NOT uniform — high is good for ISO/SLG/Hard, "
            "bad for K and Soft. Colour is keyed per row accordingly.")
        # Stance geometry + baserunning. The stance three come off the
        # leaders board (they are what BATTING_STANCE_CSV was standing in
        # for); the baserunning four are the components behind the BsR on
        # the lineup rail, which shows only the total.
        self._tbl_bio = self._make_stats_table()
        self._tbl_bio.setToolTip(
            "Where he stands and how he runs.\n\n"
            "Depth in box and distance off plate are inches (Statcast's "
            "convention); tilt is the stance angle. These move with approach "
            "— a hitter who moves up in the box is trying to get to a pitch "
            "before it breaks.\n\n"
            "Spd is Bill James' speed score. UBR is non-steal baserunning "
            "runs, wSB steal runs, XBR extra-bases-taken runs; BsR is their "
            "sum and is the figure on the lineup rail.")
        # pct(365)+disc(~350) pair, then situ(373)+bb(348) pair
        _pf = FlowHost(); _pfl = _pf
        _pfl.addWidget(pct_holder)
        _pfl.addWidget(self._tbl_disc)
        _pfl.addWidget(self._tbl_situ)
        _pfl.addWidget(self._tbl_bb)
        _pfl.addWidget(self._tbl_plus)
        _pfl.addWidget(self._tbl_bio)
        profile_lay.addWidget(_pf)
        profile_lay.addStretch()

        # ARSENAL — velo bands + the pitch-type matchup table.
        arsenal_page = QWidget()
        arsenal_lay = QVBoxLayout(arsenal_page)
        arsenal_lay.setContentsMargins(0, 4, 0, 0)
        arsenal_lay.setSpacing(6)
        self._tbl_velo = self._make_stats_table()
        self._tbl_pitch = self._make_stats_table()   # merged SP-mix + splits
        # Order matters in a flow: velo(395)+mix(327)=722 pairs inside 738,
        # velo+pitch(471)=866 does not. The widest table goes last so it takes
        # a row of its own instead of stranding a narrow one beside it.
        _af = FlowHost(); _afl = _af
        _afl.addWidget(self._tbl_velo)
        # Pitch-type expander lives INSIDE the matchup table as its last
        # (spanned) row — clicking it toggles the out-of-arsenal pitches
        self._tbl_pitch.cellClicked.connect(self._on_pitch_cell_clicked)
        # What the opposing starter actually GOES TO in each count. The table
        # above says what he throws and how the hitter fares against it; this
        # says when he throws it, which is the half that decides what is
        # coming with two strikes.
        self._tbl_mix = self._make_stats_table()
        self._tbl_mix.setToolTip(
            "The opposing starter's pitch mix BY COUNT (his own season, all "
            "batters).\n\n"
            "Each column sums to 100% — it is his usage share within that "
            "count, not the count's share of his pitches. Ahead/Behind are "
            "from the PITCHER's side, so 'Ahead' is 0-2.\n\n"
            "'2K' and '3B' overlap the other columns on purpose; they are the "
            "two counts where the mix actually shifts.")
        # PitchingBot — the opposing starter graded by a SECOND model, and
        # the one that has a command term.
        self._tbl_pbot = self._make_stats_table()
        self._tbl_pbot.setToolTip(
            "The opposing starter under FanGraphs' PitchingBot, per pitch, "
            "beside the Stuff+ already in the matchup table.\n\n"
            "The reason to carry a second model is COMMAND. Stuff+ grades a "
            "pitch's shape in isolation and has no location term at all, so "
            "a pitcher with vicious movement and no idea where it is going "
            "reads the same as one who spots it. Cmd is the half Stuff+ "
            "SCALES DIFFER, and the columns are not comparable by eye: "
            "Stf/Cmd/Ovr are PitchingBot's 20-80 scouting scale (league ~52, "
            "sd ~4), while Loc+ is FanGraphs' 100-centred Location+ (league "
            "~101, sd ~6). Both are coloured against their OWN measured "
            "league mark, at ±1 sd — hover a cell for its z.\n\n"
            "The two models DISAGREEING on a pitch is the useful signal — it "
            "usually means shape and location are pulling opposite ways.")
        _afl.addWidget(self._tbl_mix)
        _afl.addWidget(self._tbl_pbot)
        # The batter's own results BY PITCH TYPE, in run value. The matchup
        # table beside this carries the SP's run value per pitch and the
        # hitter's contact quality against it, but not the hitter's own RV —
        # which is the one figure that says "this pitch beats him".
        self._tbl_pfx = self._make_stats_table()
        self._tbl_pfx.setToolTip(
            "The HITTER's run value per 100 pitches by pitch type "
            "(FanGraphs pfx). Positive is good for the hitter.\n\n"
            "Rows under 100 pitches seen are dotted and greyed. These "
            "figures are not sample-gated at source and the tails are wild — "
            "a hitter can read +43 runs/100 against a knuckle curve he saw "
            "twenty times. Measured across 240 hitters, the noise sd falls "
            "6.6 -> 4.0 -> 3.2 -> 2.4 -> 2.0 runs/100 as pitches seen goes "
            "25 -> 50 -> 75 -> 100 -> 150, so 100 is where it stops being "
            "mostly noise.\n\n"
            "Velo is the average velocity he has faced of that type.")
        _afl.addWidget(self._tbl_pfx)
        _afl.addWidget(self._tbl_pitch)
        arsenal_lay.addWidget(_af)
        arsenal_lay.addStretch()

        # CONTEXT — deliberately empty for now. This is the space the tab
        # split frees up; what goes in it is the next decision.
        self._context_page = QWidget()
        self._context_lay = QVBoxLayout(self._context_page)
        self._context_lay.setContentsMargins(0, 4, 0, 0)
        self._context_lay.setSpacing(6)
        self._tbl_count = self._make_stats_table()
        self._tbl_count.setToolTip(
            "Performance by COUNT, pre-pitch.\n\n"
            "First / Ahead / Even / Behind partition every pitch. "
            "'2 strikes' and '3 balls' sit below the rule because they "
            "OVERLAP those four — they are the two counts worth asking "
            "about and neither is a clean slice of the others.\n\n"
            "Ahead and Behind are always from the shown player's point of "
            "view, so a pitcher's 'Ahead' is 0-2 and a batter's is 3-1.\n\n"
            "RV/100 is Savant's own per-pitch run-expectancy delta, summed "
            "per 100 pitches. Signed so that higher is better for the player "
            "shown, whichever side he is on.\n\n"
            "It measures value added FROM a count, not the value OF the "
            "count, so league average is near zero in every bucket "
            "(+0.04 to +0.24, measured on 380k pitches) — not just overall. "
            "Cells are coloured against that per-count baseline rather than "
            "against zero; hover one for its exact league mark.")
        self._ctx_flow_w = FlowHost()
        self._ctx_flow = self._ctx_flow_w
        self._ctx_flow.addWidget(self._tbl_count)
        # Game log — the exact numbers behind the Form plot's shapes. Honours
        # the same window/venue selectors as the plot and the banner.
        self._tbl_gamelog = self._make_stats_table()
        self._tbl_gamelog.setToolTip(
            "Game log, newest first. Honours the window and home/road "
            "selectors under the headshot, same as the trend plot.\n\n"
            "The stat column is whichever prop is selected, coloured against "
            "its line. The rest are that game's Statcast: batted balls, "
            "average and peak exit velocity, barrels, xwOBAcon, whiff rate.")
        # Situational VALUE. Everything else on this panel is context-free —
        # an xwOBAcon is the same number in a blowout and a tie game. These
        # are the four figures that price WHEN it happened.
        self._tbl_lev = self._make_stats_table()
        self._tbl_lev.setToolTip(
            "What his season was worth in context, from FanGraphs.\n\n"
            "WPA is win probability added — it counts a 9th-inning single "
            "for far more than a 12-0 one, so it measures the SITUATIONS he "
            "happened to bat in as much as the hitting.\n\n"
            "WPA/LI strips the leverage back out, which is why both are "
            "here: WPA/LI is what he did, WPA is what it was worth.\n\n"
            "Clutch is the difference — how much better he hit in high "
            "leverage than in his own overall line. It is self-relative, so "
            "it is near zero for almost everyone and has almost no "
            "year-to-year signal. Read it as description, NOT as a skill and "
            "not as a forecast.\n\n"
            "pLI is the average leverage he batted in (1.00 = average). "
            "RE24 is runs added over the base-out state, REW the same in "
            "wins, wRAA runs above average with no context at all.")
        self._ctx_flow.addWidget(self._tbl_lev)
        self._ctx_flow.addWidget(self._tbl_gamelog)
        self._context_lay.addWidget(self._ctx_flow_w)
        self._context_lay.addStretch()

        # ZONE — the hitter's holes against where tonight's starter lives.
        zone_page = QWidget()
        zone_lay = QVBoxLayout(zone_page)
        zone_lay.setContentsMargins(0, 4, 0, 0)
        zone_lay.setSpacing(6)
        # A FlowHost, not a QHBoxLayout: three grids at a 150px minimum need
        # 462px and the panel drops to ~394 on a narrow window, where an
        # hbox simply CLIPS the third one off the right edge with no way to
        # reach it. The flow wraps it onto a second line instead.
        grids = FlowHost(spacing=6)
        self._zone_bat = ZoneGrid("WHIFF", "#1d3b30", "#E74C3C")
        # SWING completes the pair. Whiff alone cannot tell a HOLE from a
        # zone he simply leaves alone: 40% whiff on 9 swings low-away is a
        # pitch he mostly takes, not a weakness worth attacking. Swing rate
        # is what says he offers there — and it is measured per PITCH, so
        # unlike a per-zone xwOBA grid the sample is comfortable in every
        # cell (~200 vs ~35).
        self._zone_swing = ZoneGrid("SWING", "#2a2438", "#F4D03F")
        self._zone_sp = ZoneGrid("SP LOCATION", "#1b2b3a", "#3498DB")
        grids.addWidget(self._zone_bat)
        grids.addWidget(self._zone_swing)
        grids.addWidget(self._zone_sp)
        zone_lay.addWidget(grids)
        self._zone_note = QLabel("")
        self._zone_note.setObjectName("matchupLine")
        self._zone_note.setWordWrap(True)
        self._zone_note.setTextFormat(Qt.TextFormat.RichText)
        zone_lay.addWidget(self._zone_note)
        self._tbl_zone = self._make_stats_table()
        self._tbl_zone.setToolTip(
            "Zone rows are thirds of THIS batter's own strike zone "
            "(sz_top/sz_bot come per pitch), not fixed heights — a 6'7\" "
            "hitter's letters are not a 5'8\" hitter's.\n\n"
            "Columns are batter-relative (inside/middle/outside), so a lefty "
            "and a righty read the same; plate_x is signed by handedness "
            "before binning.\n\n"
            "Whiff rate rather than xwOBA on purpose: this hitter has ~480 "
            "swings behind the grid but only ~140 batted balls, so per-zone "
            "xwOBA would be about 16 balls a cell — noise drawn as a heat "
            "map.")
        _zf = FlowHost(); _zfl = _zf
        _zfl.addWidget(self._tbl_zone)
        # Savant ATTACK ZONES — the coarse partition the 3x3 grid cannot
        # express, because its cells are all inside the zone. Heart/Shadow/
        # Chase/Waste is the partition that separates discipline from
        # damage: the 3x3 says WHERE in the zone, this says whether he is
        # picking the right pitches to swing at in the first place.
        self._tbl_az = self._make_stats_table()
        self._tbl_az.setToolTip(
            "Statcast ATTACK ZONES — Heart / Shadow / Chase / Waste. They "
            "partition every pitch he has seen (the four Seen% sum to 100).\n\n"
            "Heart is the middle of the zone, Shadow the band straddling its "
            "edge (split here into the in-zone and out-of-zone halves), Chase "
            "well outside, Waste nowhere near.\n\n"
            "This is the discipline view the 3×3 grids cannot give: their "
            "nine cells are all INSIDE the zone, so they say where in the "
            "zone he struggles but nothing about whether he should have been "
            "swinging at all. Shadow is where plate discipline is actually "
            "decided — it is 42% of all pitches and the only band where the "
            "swing decision is genuinely hard.\n\n"
            "Coloured against the measured league mark (240 hitters, 250+ "
            "PA). Direction is per row: swinging MORE in the Heart is good, "
            "swinging more at Chase/Waste is bad, and contact is good "
            "everywhere. Hover a cell for the league figure.")
        _zfl.addWidget(self._tbl_az)
        zone_lay.addWidget(_zf)
        zone_lay.addStretch()

        # SPLITS — the FanGraphs splits leaderboard, ~40 splits deep, with
        # the sampling band drawn rather than left to the reader.
        splits_page = QWidget()
        splits_lay = QVBoxLayout(splits_page)
        splits_lay.setContentsMargins(0, 4, 0, 0)
        splits_lay.setSpacing(6)
        _shead = QHBoxLayout()
        _shead.setContentsMargins(0, 0, 0, 0)
        _shead.setSpacing(6)
        self._split_head = QLabel("")
        self._split_head.setObjectName("matchupLine")
        self._split_head.setWordWrap(True)
        self._split_head.setTextFormat(Qt.TextFormat.RichText)
        _shead.addWidget(self._split_head, stretch=1)
        # The default view hides splits under SPLIT_MIN_PA because their band
        # is wider than the chart, but hiding data is the UI's call to make,
        # not the model's — this puts every row back, thin ones included.
        self._split_all = QCheckBox("all splits")
        self._split_all.setToolTip(
            "Show every split FanGraphs returns, including ones too thin to "
            "read (under 15 PA).\n\n"
            "They are not wrong, just wide: at 15 PA the sampling band is "
            "±85 wRC+ points, wider than the chart's scale. The dots still "
            "plot; treat them as the raw record rather than a measurement.")
        self._split_all.setStyleSheet(
            "QCheckBox { color: #7F8C8D; font-size: 8pt; }")
        self._split_all.toggled.connect(lambda _: self._render_splits())
        _shead.addWidget(self._split_all, alignment=Qt.AlignmentFlag.AlignTop)
        splits_lay.addLayout(_shead)
        self._split_chart = SplitBandChart()
        splits_lay.addWidget(self._split_chart)
        self._tbl_split = self._make_stats_table()
        self._tbl_split.setToolTip(
            "FanGraphs splits leaderboard — the numbers behind the chart "
            "above.\n\n"
            "wRC+ is park- and league-adjusted, so 100 is average in EVERY "
            "row and the rows are comparable to each other; a triple-slash "
            "line is not.\n\n"
            "'±' is one sampling sigma at that row's PA and 'z' is how many "
            "of them the split sits from the player's own season line. Under "
            "1σ the split is indistinguishable from him just being himself.\n\n"
            "Share is the row's PA as a fraction of its group, shown only "
            "for groups that partition the season (platoon, venue, outs, "
            "slot, inning, month).")
        _sf = FlowHost(); _sfl = _sf
        _sfl.addWidget(self._tbl_split)
        splits_lay.addWidget(_sf)
        splits_lay.addStretch()

        self._sections.addTab(self._form_page, "Form")
        self._sections.addTab(profile_page, "Profile")
        self._sections.addTab(splits_page, "Splits")
        self._sections.addTab(arsenal_page, "Arsenal")
        self._sections.addTab(zone_page, "Zone")
        self._sections.addTab(self._context_page, "Context")
        content_lay.addWidget(self._sections, stretch=1)

        # -- trend page: the big analytical plot. Rolling average of the
        #    prop stat (left axis) with toggleable rolling Statcast overlays
        #    on a second right-hand ViewBox.
        trend_page = QWidget()
        # Hug the plot. On a Preferred vertical policy this page GROWS when
        # the tab is taller than its content, but the plot inside is capped
        # at 320 — so the surplus became dead band above and below the chart
        # (~140px each on a tall window). The plot's cap is deliberate; the
        # page must not out-grow it. Surplus belongs to the trailing stretch.
        trend_page.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Minimum)
        # Same clamp, for the same reason: PlotWidget's sizeHint is larger
        # than the 320 cap set on it below, so the page sized itself off the
        # hint and left a band under a plot that had already stopped growing.
        trend_page.setMaximumHeight(320)
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
        # 320, not 400. The Form tab stacks header (243) + plot + spray row
        # (245) + the direction/rest row (88); at 400 that totalled ~1025 and
        # the bottom row fell off a 1080-tall window. At 320 it lands ~945 and
        # the last row clears the viewport with the plot still readable.
        self._trend_plot.setMaximumHeight(320)
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
        self._spray_action.setChecked(True)     # visible by default
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
        # No trailing stretch: it existed to pool spare height BELOW a capped
        # plot, which on its own tab just means dead space. The plot takes the
        # page (still bounded by setMaximumHeight).

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
        # Matched to the 185px width it now gets. The plot is aspect-locked,
        # so at 365 tall it drew a small diamond in a tall empty box and made
        # the whole flow row 365 high — which pushed the second row past the
        # bottom of the panel entirely.
        # Tall box on purpose. The plot is ASPECT-LOCKED, so the field is
        # drawn at whichever dimension binds: in a short box the height binds
        # and the diamond shrinks to ~144px wide inside its 185px slot. Giving
        # it depth makes WIDTH the binding dimension, so the field fills all
        # 185px it has. The trade is dead space above and below the diamond,
        # which is what that column has spare anyway.
        self._spray_plot.setMaximumHeight(240)
        # No floating "Stats ▾" here. It existed to toggle back to the trend
        # view when the two SHARED one slot; under the masonry flow the spray
        # and the trend plot are both on screen at once, so the button only
        # ever offered to hide a chart the user was already looking at — and
        # it sat on top of the spray it was covering. The action itself is
        # kept (the trend plot's own menu still carries it).

        spray_page = QWidget()
        # Legend OVERLAID on the plot's bottom-left corner, not a strip beside
        # it. The plot is ASPECT-LOCKED, so its width is the only thing that
        # sets how big the field draws — a title row above spent height the
        # chart could not use, and the vertical strip that replaced it spent
        # 52px of the one dimension that mattered. Floating it (the pattern
        # `_trend_bottom_legend` already uses on the trend plot) costs
        # nothing: the bottom-left of the box is foul ground outside the LF
        # line, permanently empty. The counts stay stacked — four categories,
        # not a sentence.
        spray_lay = QVBoxLayout(spray_page)
        spray_lay.setContentsMargins(0, 0, 0, 0)
        spray_lay.setSpacing(0)
        self._spray_legend = QLabel("", self._spray_plot)
        self._spray_legend.setObjectName("matchupLine")
        self._spray_legend.setTextFormat(Qt.TextFormat.RichText)
        self._spray_legend.setStyleSheet("background: transparent;")
        self._spray_legend.setAlignment(Qt.AlignmentFlag.AlignLeft
                                        | Qt.AlignmentFlag.AlignBottom)
        spray_lay.addWidget(self._spray_plot, stretch=1)
        # keeps the overlay pinned as the aspect-locked plot resizes
        self._spray_plot.installEventFilter(self)

        from PyQt6.QtWidgets import QStackedWidget
        # Trend and spray SIDE BY SIDE, not stacked behind a menu toggle.
        # The spray chart is aspect-locked and roughly square, so it wants
        # ~340px and the trend keeps the rest — and the Form tab had the
        # vertical room sitting empty anyway. The Stats menu action now
        # HIDES/SHOWS the spray rather than swapping which one you can see.
        # Trend across the TOP at full width, spray BELOW it — not side by
        # side. The trend is a time series and wants width; squeezed to 370px
        # beside the spray it became a tall narrow strip with unreadable date
        # ticks. Stacked, the trend keeps its aspect and the spray (which is
        # aspect-locked and square) sits under it with room left beside it.
        self._spray_page = spray_page
        # 150, was 185. The old figure was sized for ROW packing — "narrow
        # enough that the spray, the rolling-form table and the attack table
        # share ONE row" — and that constraint is gone now that `_do` is a
        # skyline fill.
        #
        # What matters instead is whether a SECOND COLUMN OF TABLES fits
        # beside it. The tables run 247-280 wide, so a three-column layout
        # needs spray + 280 + 247 + two 8px gaps. At 185 that is 728 and the
        # panel is ~715, which missed by ~13px — so every table stacked into
        # one tall column and left a ~240px dead band down the right side,
        # just too narrow for the 247px table that wanted to go there. At 150
        # it comes to 693 and the column forms.
        #
        # The spray is aspect-locked, so this costs a slightly smaller chart
        # and buys a whole column of tables out of the dead space.
        # 260 wide, was 150. The chart is aspect-locked, so WIDTH is the only
        # lever on how big it draws — and at 150 (minus the old title row) it
        # rendered about 148px across inside a 245-tall page, leaving ~90px
        # of dead height under it. 260 minus the 46px legend strip gives the
        # plot ~210 across, a 40% bigger field, and the height now matches
        # what an aspect-locked square actually uses instead of padding it.
        #
        # This DOES cost the second table column at panel widths under ~790
        # (260 + 280 + 247 + gaps = 803). That is the trade asked for: the
        # tables stack in one column to the right of a bigger field, and the
        # flow height is unchanged because the table stack was already the
        # tallest thing in the row.
        spray_page.setFixedWidth(262)
        spray_page.setFixedHeight(222)
        self._form_lay.addWidget(trend_page)
        self._tbl_form = self._make_stats_table()
        self._tbl_form.setToolTip(
            "Rolling form. The plot above shows the SHAPE; this puts numbers "
            "on it at the windows people actually quote.\n\n"
            "Honours the home/road selector but NOT the window selector — "
            "the windows are the rows, so filtering to one would leave a "
            "table of a single repeated line.\n\n"
            "Rates are pooled over the window (total barrels / total batted "
            "balls), not an average of per-game rates, so a one-batted-ball "
            "game cannot swing it.")
        _ff = FlowHost(); self._form_flow = _ff
        self._form_flow.addWidget(spray_page)
        self._form_flow.addWidget(self._tbl_form)
        self._tbl_attack = self._make_stats_table()
        self._tbl_attack.setToolTip(
            "How pitchers have been WORKING him lately versus the rest of the "
            "season. The plot above says a hitter went cold; it cannot say "
            "why, and one ordinary cause is that staffs changed approach.\n\n"
            "Split on the games in the current window (the L15/L30 selector), "
            "not on a pitch count, so it lines up with the plot.\n\n"
            "A slump that is just variance looks FLAT here. Deltas of a few "
            "points are noise — the row is worth reading when the mix moves "
            "5+ points or the zone rate moves with it.")
        self._form_flow.addWidget(self._tbl_attack)
        self._tbl_dir = self._make_stats_table()
        self._tbl_dir.setToolTip(
            "Where he hits the ball, split by WHERE IT WAS PITCHED.\n\n"
            "Raw pull rate over time is close to meaningless: direction is "
            "mostly dictated by location — you pull the inside one and serve "
            "the outside one — so a falling pull rate often means he is being "
            "worked away, not that he changed anything.\n\n"
            "Pull/Cent/Oppo are therefore reported WITHIN each band (that part "
            "is the hitter). 'Seen' is the share of pitches to that band and "
            "'\u0394Seen' how it has moved in the current window (that part is "
            "the pitcher). Read together they separate the two.\n\n"
            "Bands are batter-relative and spray is oppo-signed, so a lefty "
            "and a righty read the same.")
        self._form_flow.addWidget(self._tbl_dir)
        self._tbl_rest = self._make_stats_table()
        self._tbl_rest.setToolTip(
            "Contact quality by days off before the game — the schedule half "
            "of form. `batter_days_since_prev_game` ships in the Savant CSV "
            "and nothing read it until now.\n\n"
            "Buckets are back-to-back / 1 day / 2+ days rather than a "
            "continuum, because those are the states people actually argue "
            "about.\n\n"
            "Pooled per pitch, not averaged per game, so a one-batted-ball "
            "afternoon cannot outvote a full one. Watch the BBE column — a "
            "bucket with 15 batted balls is a curiosity, not a finding.")
        self._form_flow.addWidget(self._tbl_rest)
        self._form_lay.addWidget(_ff)
        self._form_lay.addStretch()

    def eventFilter(self, obj, ev):
        if obj is self._trend_plot:
            if ev.type() == QEvent.Type.Resize:
                self._place_trend_chip_bar()
            elif ev.type() == QEvent.Type.Leave:
                self._trend_vline.hide()
                self._trend_htext.hide()
        elif obj is self._spray_plot and ev.type() == QEvent.Type.Resize:
            self._place_spray_legend()
        return super().eventFilter(obj, ev)

    def _place_spray_legend(self):
        """Pin the spray counts to the plot's bottom-left corner. The axes
        are hidden, so there is no tick band to clear — 4px off the frame.

        The width comes from laying the HTML out in a QTextDocument, not from
        `adjustSize()`: a rich-text QLabel sizes itself from its CURRENT width
        (it re-wraps to whatever it already is), so on this label — which
        starts at nothing — it settled on 33px and folded the venue line into
        a column one word wide. Measuring the document is the only reading
        that does not depend on the label's previous size."""
        leg = self._spray_legend
        doc = QTextDocument()
        doc.setDefaultFont(leg.font())
        doc.setHtml(leg.text())
        doc.setTextWidth(-1)
        leg.setFixedWidth(min(int(doc.idealWidth()) + 6,
                              max(40, self._spray_plot.width() - 8)))
        leg.adjustSize()
        leg.move(4, max(0, self._spray_plot.height() - leg.height() - 4))
        leg.raise_()

    def _set_spray_legend(self, html: str):
        self._spray_legend.setText(html)
        self._place_spray_legend()

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

    def _scroll_to_top(self, *_):
        """Walk up to the enclosing QScrollArea and return it to the top.

        The panel does not own its scroll area — MLBWindow wraps it, and so
        does the props window — so it has to be found rather than held."""
        w = self.parentWidget()
        while w is not None:
            if isinstance(w, QScrollArea):
                w.verticalScrollBar().setValue(0)
                return
            w = w.parentWidget()

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
        # Column 0 carries the ROW IDENTITY, and _sync_flank_widths stretches
        # it when the table is capped — so its natural width has to be
        # recorded here, while ResizeToContents is still in force, or there is
        # nothing to floor the stretch against later.
        t._natural_c0 = t.columnWidth(0) if t.columnCount() else 0
        t.setFixedWidth(w)

    # Width the percentile stack gets beside the split table (bars scale
    # to fit, no distortion — paintEvent works off the live width).
    # 134: at 150 the splits table came up ~15px short and grew a needless
    # internal h-scrollbar
    # Half the panel at the width it actually gets (738), leaving the other
    # half for the discipline table beside it. 350 not 365: the discipline
    # table measures 367, and 365+8+367 = 740 missed the 738 panel by two
    # pixels and bumped the table onto its own row.
    _PCT_STACK_W = 350
    # The plot no longer sits BESIDE these tables — it has its own tab — so
    # nothing needs reserving for it. This was 272, which in a 394px viewport
    # capped every table at 260 and was the direct cause of the situational
    # table hiding 213px of its columns and the pitch matchup 211px.
    _PLOT_RESERVE = 0
    _FLANK_MARGIN = 20
    # (The spray legend used to be a 48px vertical strip beside the chart.
    # It now floats over the plot's bottom-left corner — see
    # `_place_spray_legend` — so it reserves no width at all.)
    # Narrowest a row-label column may be squeezed to before the table is
    # made to scroll instead. 64px fits "Runners on" / "Full count" at 8pt,
    # which are the longest labels any of these tables carries.
    _C0_FLOOR = 64

    def _available_width(self) -> int:
        """Width the panel actually has to lay out in. When wrapped in a
        QScrollArea (as in the props window) the parent is the scroll
        viewport, whose width is the real budget — self.width() would be the
        already-overgrown content width and useless for capping."""
        p = self.parentWidget()
        w = p.width() if p is not None else self.width()
        return w if w > 50 else 0

    def _sync_flank_widths(self):
        """Size each table to its OWN content, capped to the panel.

        This used to hand every table one shared width (the widest one's) so
        their left edges lined up in a single stacked column. Under the flow
        layout that is exactly wrong: it inflated a 355px count table and a
        367px game log to 471 each, and 471+471 does not fit in 738, so two
        blocks that would happily sit side by side were forced onto separate
        rows with ~270px of dead margin beside each. At natural widths they
        pair (355+367+8 = 730) and the tab fills.

        The cap still matters — a table wider than the panel scrolls
        internally rather than running off the edge."""
        avail = self._available_width()
        cap = (max(260, avail - self._FLANK_MARGIN) if avail else 10 ** 6)
        self._flank_target = cap

        for name in ("_tbl_velo", "_tbl_pitch", "_tbl_situ", "_tbl_count",
                     "_tbl_gamelog", "_tbl_bb", "_tbl_mix", "_tbl_zone",
                     "_tbl_disc", "_tbl_form", "_tbl_attack", "_tbl_dir",
                     "_tbl_rest", "_tbl_split", "_tbl_plus",
                     "_tbl_bio", "_tbl_lev", "_tbl_pbot", "_tbl_az",
                     "_tbl_pfx"):
            t = getattr(self, name, None)
            nat = getattr(t, "_natural_w", 0) if t is not None else 0
            if not nat:
                continue
            width = min(nat, cap)
            t.setFixedWidth(width)
            # Column 0 only stretches when the table was CAPPED; at natural
            # width stretching it just pads the label column with air.
            #
            # BUT THE STRETCH NEEDS A FLOOR. Stretch absorbs the SLACK, and a
            # capped table has none — the other columns are ResizeToContents
            # and take what they need, so column 0 gets the remainder, which
            # can be nothing. It then bottoms out on the header's global
            # minimumSectionSize (18px) and the row labels vanish entirely,
            # leaving a table of numbers with no way to tell which row is
            # which. The splits table hit this at 438 natural in a 372
            # viewport: column 0 came out at exactly 18.
            #
            # (This is the same failure the batted-ball table hit — see the
            # "trim the header before dropping a statistic" note — but the
            # fix there was to shrink the content until it fit, which only
            # works until the next narrow panel.)
            #
            # Below the floor, column 0 goes Fixed and the table scrolls
            # instead. Scrolling the numeric columns is recoverable; scrolling
            # away the identity column is not.
            hdr_t = t.horizontalHeader()
            if width < nat:
                nat_c0 = getattr(t, "_natural_c0", 0)
                others = sum(t.columnWidth(c)
                             for c in range(1, t.columnCount()))
                room = t.viewport().width() - others
                floor = min(nat_c0, self._C0_FLOOR) if nat_c0 else 0
                if floor and room < floor:
                    hdr_t.setSectionResizeMode(
                        0, QHeaderView.ResizeMode.Fixed)
                    t.setColumnWidth(0, floor)
                else:
                    hdr_t.setSectionResizeMode(
                        0, QHeaderView.ResizeMode.Stretch)
            else:
                hdr_t.setSectionResizeMode(
                    0, QHeaderView.ResizeMode.ResizeToContents)
            nat_h = getattr(t, "_natural_h", 0)
            if nat_h:
                sb = (t.horizontalScrollBar().sizeHint().height()
                      if width < nat else 0)
                t.setFixedHeight(nat_h + sb)
        # Every table above just changed size, which re-wraps the flows they
        # sit in. A FlowHost only re-pins its height on its OWN resize, and
        # resizing a child does not resize the host — so it has to be told,
        # or the newly-wrapped rows sit below the panel's scroll extent.
        for host in self.findChildren(FlowHost):
            host._repin()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-cap the flank when the panel (splitter/monitor) changes width.
        # Cheap (setFixedWidth only); the chart re-thins its own ticks on the
        # next combo change, so no heavy re-render on every drag pixel.
        if getattr(self, "_tbl_velo", None) is not None:
            self._sync_flank_widths()

    def _on_bottom_view_toggle(self, *_):
        """Spray is visible by default now; the action hides it to give the
        trend plot the whole tab."""
        spray = self._spray_action.isChecked()
        self._spray_page.setVisible(spray)
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
    # 4, not 7. At 7 the traditional grid ran ~500px across the top and the
    # headshot/name block pushed the total past 620, leaving nothing usable
    # to its right. At 4 it is ~170px, the whole identity block tucks into
    # the left ~410px, and the swing chips move up into the space freed.
    _TRAD_PAIRS_PER_ROW = 7
    # 4 across in the header's right-hand slot (~320px), giving the nine
    # batter chips three tidy rows that sit level with the 7-band stat grid.
    _SWING_CHIPS_PER_ROW = 3

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

    # ------------------------------------------------------- FanGraphs splits

    def show_fg_splits(self, splits: Dict[str, Dict[int, dict]], pid: int,
                       season_wrc: Optional[float]):
        """League-wide FG split boards + which player to pull out of them."""
        self._fg_split_data = (splits, pid, season_wrc)
        self._render_splits()

    def _tonight_splits(self) -> set:
        """The split labels that describe tonight's plate appearances."""
        ctx = self._last_ctx
        if ctx is None:
            return set()
        today = {"Home" if ctx.is_home else "Away"}
        hand = getattr(ctx, "opp_pitcher_hand", None)
        if hand in ("L", "R"):
            today.add("vs LHP" if hand == "L" else "vs RHP")
        slot = getattr(ctx, "lineup_slot", None)
        if isinstance(slot, int) and 1 <= slot <= 9:
            today.add(("Bat 1st", "Bat 2nd", "Bat 3rd", "Bat 4th", "Bat 5th",
                       "Bat 6th", "Bat 7th", "Bat 8th", "Bat 9th")[slot - 1])
        return today

    def _render_splits(self):
        table = self._tbl_split
        table.setRowCount(0)
        data = getattr(self, "_fg_split_data", None)
        if not data:
            self._split_head.setText("")
            self._split_chart.set_rows([], None)
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Splits — not loaded"])
            self._fit_table(table)
            return
        splits, pid, season_wrc = data
        show_all = self._split_all.isChecked()
        rows = split_profile(splits, pid, season_wrc,
                             min_pa=0 if show_all else SPLIT_MIN_PA)
        today = self._tonight_splits()
        self._split_chart.set_rows(rows, season_wrc, today)

        # Header: the platoon read, shrunk. This is the one split with a
        # measured true-variance behind it, so it is the only one that gets
        # to state a believable number rather than just a band.
        bits = []
        if season_wrc is not None:
            bits.append(f"season <b>{season_wrc:.0f}</b> wRC+")
        by = {r["split"]: r for r in rows}
        l, r_ = by.get("vs LHP"), by.get("vs RHP")
        if l and r_ and l.get("wrc") is not None and r_.get("wrc") is not None:
            sh = shrink_platoon(l["wrc"], l["pa"], r_["wrc"], r_["pa"])
            if sh:
                obs, true, w = sh["obs"], sh["true"], sh["weight"]
                side = "vs LHP" if true >= 0 else "vs RHP"
                bits.append(
                    f"platoon gap <b>{obs:+.0f}</b> observed → "
                    f"<b style='color:#F4D03F'>{true:+.0f}</b> believable "
                    f"({w*100:.0f}% kept, better {side})")
        if today:
            bits.append("tonight: " + ", ".join(sorted(today)))
        self._split_head.setText(
            " &nbsp;·&nbsp; ".join(bits)
            or "no season line — splits shown without z-scores")

        headers = ["Split", "PA", "Sh", "wRC+", "±", "z", "wOBA", "ISO",
                   "BABIP", "BB%", "K%"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        highlight_bg = QColor(24, 42, 58)
        last_group = None
        rix = 0
        for r in rows:
            # A thin rule between groups, as its own spanned row
            if last_group is not None and r["group"] != last_group:
                table.insertRow(rix)
                sep = QTableWidgetItem(r["group"].upper())
                sep.setForeground(QColor("#5D6D7E"))
                fnt = sep.font(); fnt.setPointSize(6); fnt.setBold(True)
                sep.setFont(fnt)
                table.setItem(rix, 0, sep)
                table.setSpan(rix, 0, 1, len(headers))
                rix += 1
            elif last_group is None:
                table.insertRow(rix)
                sep = QTableWidgetItem(r["group"].upper())
                sep.setForeground(QColor("#5D6D7E"))
                fnt = sep.font(); fnt.setPointSize(6); fnt.setBold(True)
                sep.setFont(fnt)
                table.setItem(rix, 0, sep)
                table.setSpan(rix, 0, 1, len(headers))
                rix += 1
            last_group = r["group"]

            def fmt3(v):
                return f"{v:.3f}".lstrip("0") if v is not None else ""

            def pct(v):
                # FanGraphs ships these as fractions but labels them percents
                return f"{v*100:.1f}" if v is not None else ""

            z = r.get("z")
            table.insertRow(rix)
            vals = [
                f"{r['pa']:.0f}",
                f"{r['share']*100:.0f}%" if r.get("share") else "",
                f"{r['wrc']:.0f}" if r.get("wrc") is not None else "",
                f"±{r['sd']:.0f}" if r.get("sd") else "",
                f"{z:+.1f}" if z is not None else "",
                fmt3(r.get("woba")), fmt3(r.get("iso")), fmt3(r.get("babip")),
                pct(r.get("bb")), pct(r.get("k")),
            ]
            items = [cell(r["split"], align_right=False)] + [cell(v) for v in vals]
            # Colour the wRC+ and z cells by SIGMAS — see SplitBandChart
            if z is not None and abs(z) >= 1:
                col = QColor("#2ECC71") if z > 0 else QColor("#E74C3C")
                if abs(z) >= 2:
                    col = QColor("#27AE60") if z > 0 else QColor("#C0392B")
                items[3].setForeground(col)
                items[5].setForeground(col)
            else:
                items[5].setForeground(QColor("#7F8C8D"))
            if r["split"] in today:
                for it in items:
                    it.setBackground(highlight_bg)
                fnt = items[0].font(); fnt.setBold(True)
                items[0].setFont(fnt)
            for c, it in enumerate(items):
                table.setItem(rix, c, it)
            rix += 1
        table.resizeRowsToContents()
        self._fit_table(table)
        self._sync_flank_widths()

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

    def show_count_splits(self, data: List[dict]):
        """Per-count table on the Context tab (count_splits output)."""
        table = self._tbl_count
        table.setRowCount(0)
        table.clearSpans()
        if not data:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Count splits — no data"])
            self._fit_table(table)
            self._sync_flank_widths()
            return
        headers = ["Count", "Pit", "Sw%", "Whf%", "BBE", "EV", "xwOBAcon",
                   "RV/100"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        pct = lambda v: f"{v:.0%}" if v is not None else "—"
        # The overlapping buckets (2 strikes / 3 balls) still sort to the
        # bottom, but they are marked ON THE ROW with a leading dot and a
        # dimmed label rather than by a spanned "also" separator above them.
        # The separator spent a whole row of height to say one word, and the
        # word did not read as "these overlap the rows above".
        rows = [d for d in data if not d["overlap"]]
        over = [d for d in data if d["overlap"]]
        table.setRowCount(len(rows) + len(over))
        r = 0
        for d in rows + over:
            if d["overlap"]:
                lab = cell(f"· {d['split']}", align_right=False)
                lab.setForeground(QColor("#95A5A6"))
                lab.setToolTip("Overlaps the buckets above — these pitches "
                               "are already counted in First/Ahead/Even/"
                               "Behind, so the column does not sum to 100%.")
                table.setItem(r, 0, lab)
            else:
                table.setItem(r, 0, cell(d["split"], align_right=False))
            table.setItem(r, 1, cell(f"{d['pit']}"))
            table.setItem(r, 2, cell(pct(d["swing"])))
            table.setItem(r, 3, cell(pct(d["whiff"])))
            table.setItem(r, 4, cell(f"{d['bbe']}"))
            table.setItem(r, 5, cell(f"{d['ev']:.1f}" if d["ev"] else "—"))
            table.setItem(r, 6, cell(f"{d['xw']:.3f}".lstrip("0")
                                     if d["xw"] else "—"))
            # Coloured against this bucket's LEAGUE baseline, not against
            # zero — see _COUNT_RV_BASE. The dead band keeps near-average
            # cells grey instead of flickering green/red on noise.
            edge = d["edge"]
            rv = cell(f"{d['rv100']:+.2f}")
            rv.setForeground(QColor("#2ECC71" if edge > 0.25
                                    else "#E74C3C" if edge < -0.25
                                    else "#BDC3C7"))
            rv.setToolTip(f"{d['rv100']:+.2f} vs league {d['base']:+.2f} "
                          f"for this count  →  {edge:+.2f}")
            table.setItem(r, 7, rv)
            r += 1
        self._fit_table(table)
        self._sync_flank_widths()

    def show_zone(self, bat_cells: Optional[dict], sp_cells: Optional[dict],
                  sp_name: str = ""):
        """Zone tab: the hitter's whiff grid, the starter's location grid, and
        the one sentence that crosses them."""
        self._zone_bat.set_cells(bat_cells, "whiff",
                                 f"{bat_cells['total']} pitches"
                                 if bat_cells else "")
        self._zone_swing.set_cells(bat_cells, "swing",
                                   f"{bat_cells['in_zone']} in zone"
                                   if bat_cells else "")
        self._zone_sp.set_cells(sp_cells, "usage",
                                sp_name.split()[-1] if sp_name else "")
        m = zone_matchup(bat_cells, sp_cells) if (bat_cells and sp_cells) else None
        if m:
            edge = m["share"] - m["neutral"]
            col = "#E74C3C" if edge > 0.04 else "#2ECC71" if edge < -0.04 \
                else "#BDC3C7"
            who = sp_name.split()[-1] if sp_name else "He"
            self._zone_note.setText(
                f"<span style='color:#7F8C8D'>{who} puts </span>"
                f"<span style='color:{col};font-weight:bold'>"
                f"{m['share']:.0%}</span>"
                f"<span style='color:#7F8C8D'> of in-zone pitches in his 3 "
                f"highest-whiff cells ({m['whiff']:.0%} whiff there). "
                f"Neutral is 33%.</span>")
        else:
            self._zone_note.setText(
                "<span style='color:#7F8C8D'>Not enough located pitches to "
                "cross the two grids.</span>")
        # numeric backing for the heat map
        table = self._tbl_zone
        table.setRowCount(0); table.clearSpans()
        if not bat_cells:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Zone — no location data"])
            self._fit_table(table); self._sync_flank_widths(); return
        headers = ["Zone", "Pit", "Sw", "Whf%", "BBE", "xwOBAcon", "SP%"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        sp_in = (sum(sp_cells["grid"][r][c]["pit"]
                     for r in range(3) for c in range(3))
                 if sp_cells else 0)
        entries = [(f"{_ZONE_ROWS[r]}-{_ZONE_COLS[c]}",
                    bat_cells["grid"][r][c],
                    sp_cells["grid"][r][c] if sp_cells else None)
                   for r in range(3) for c in range(3)]
        entries.append(("Chase", bat_cells["oz"],
                        sp_cells["oz"] if sp_cells else None))
        table.setRowCount(len(entries))
        for r, (lab, b, s) in enumerate(entries):
            table.setItem(r, 0, cell(lab, align_right=False))
            table.setItem(r, 1, cell(f"{b['pit']}"))
            table.setItem(r, 2, cell(f"{b['sw']}"))
            table.setItem(r, 3, cell(f"{b['whiff']/b['sw']:.0%}"
                                     if b["sw"] >= 8 else "—"))
            table.setItem(r, 4, cell(f"{b['bbe']}"))
            table.setItem(r, 5, cell(f"{b['xw']/b['bbe']:.3f}".lstrip("0")
                                     if b["bbe"] >= 5 else "—"))
            table.setItem(r, 6, cell(f"{s['pit']/sp_in:.0%}"
                                     if (s and sp_in and lab != "Chase")
                                     else "—"))
        self._fit_table(table)
        self._sync_flank_widths()

    def show_pitch_mix(self, pitcher_name: str, data: List[dict]):
        """SP pitch mix by count on the Arsenal tab."""
        table = self._tbl_mix
        table.setRowCount(0)
        table.clearSpans()
        if not data:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Mix by count — no data"])
            self._fit_table(table)
            self._sync_flank_widths()
            return
        headers = [f"{pitcher_name.split()[-1]} mix", "All", "0-0", "Ahd",
                   "Evn", "Bhd", "2K", "3B"]
        keys = ["all", "First", "Ahead", "Even", "Behind", "2 strikes",
                "3 balls"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        # Only the pitches he actually throws — a 2% show-me pitch adds a row
        # of noise to a table whose point is what is coming in a big count.
        data = [d for d in data if (d.get("all") or 0) >= 0.03]
        table.setRowCount(len(data))
        for r, d in enumerate(data):
            table.setItem(r, 0, cell(d["pitch"], align_right=False))
            for c, k in enumerate(keys, start=1):
                v = d.get(k)
                it = cell(f"{v:.0%}" if v is not None else "—")
                # Flag where a count genuinely changes his mix
                base = d.get("all") or 0
                if v is not None and k != "all" and abs(v - base) >= 0.08:
                    it.setForeground(QColor("#2ECC71" if v > base
                                            else "#E67E22"))
                table.setItem(r, c, it)
        self._fit_table(table)
        self._sync_flank_widths()

    def show_discipline(self, data: List[dict]):
        """Plate-discipline table beside the percentile stack."""
        table = self._tbl_disc
        table.setRowCount(0); table.clearSpans()
        if not data:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Discipline — no data"])
            self._fit_table(table); self._sync_flank_widths(); return
        headers = ["Disc", "Pit", "Zone", "Z-Sw", "Z-Con", "O-Sw", "O-Con",
                   "SwStr", "1st-Sw"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        pct = lambda v: f"{v:.0%}" if v is not None else "—"
        table.setRowCount(len(data))
        for r, d in enumerate(data):
            table.setItem(r, 0, cell(d["split"], align_right=False))
            table.setItem(r, 1, cell(f"{d['pit']}"))
            for c, k in enumerate(("zone", "zsw", "zcon", "osw", "ocon",
                                   "swstr", "fsw"), start=2):
                table.setItem(r, c, cell(pct(d[k])))
        self._fit_table(table)
        self._sync_flank_widths()

    def show_batted_ball(self, data: List[dict]):
        """Batted-ball profile table on the Profile tab."""
        table = self._tbl_bb
        table.setRowCount(0)
        table.clearSpans()
        if not data:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Batted ball — no data"])
            self._fit_table(table)
            self._sync_flank_widths()
            return
        # Headers carry no "%" and the splits are abbreviated: the full
        # "Batted ball / GB% / ... / Shift%" set measures 462px against a
        # 388px cap, and the shared column-0 Stretch then squeezed the label
        # column to "ted l". Every cell already prints its own % sign, so the
        # header does not need to. Trimming beats dropping a statistic.
        headers = ["Split", "BBE", "GB", "LD", "FB", "PU",
                   "Pull", "Cent", "Oppo", "Shift"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        pct = lambda v: f"{v:.0%}" if v is not None else "—"
        table.setRowCount(len(data))
        for r, d in enumerate(data):
            table.setItem(r, 0, cell(d["split"], align_right=False))
            table.setItem(r, 1, cell(f"{d['n']}"))
            for c, k in enumerate(("gb", "ld", "fb", "pu",
                                   "pull", "cent", "oppo", "shift"), start=2):
                table.setItem(r, c, cell(pct(d[k])))
        self._fit_table(table)
        self._sync_flank_widths()

    _FORM_WINDOWS = ((5, "L5"), (10, "L10"), (15, "L15"), (30, "L30"),
                     (None, "Season"))

    def _render_form_windows(self, summary: PropStatSummary):
        """Rolling form beside the spray chart: the prop stat and the
        contact-quality rates over L5/L10/L15/L30/season.

        Rates are POOLED over the window (total barrels / total batted balls)
        rather than averaged across games — a game with one batted ball would
        otherwise carry the same weight as a four-hit night."""
        table = self._tbl_form
        table.setRowCount(0); table.clearSpans()
        games = self._filtered_games(summary)      # venue filter only
        if not games:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Form — no games"])
            self._fit_table(table); self._sync_flank_widths(); return
        stat = summary.stat_label or "Stat"
        headers = ["Window", stat, "EV", "Brl", "xwOBA", "Whf"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        pg = getattr(self, "_pg_statcast", {}) or {}
        rows = [(lab, games[-n:] if n else games)
                for n, lab in self._FORM_WINDOWS]
        rows = [(lab, g) for lab, g in rows if g]
        table.setRowCount(len(rows))
        for r, (lab, gs) in enumerate(rows):
            ev, brl, bbe, xw, sw, wh = [], 0, 0, [], 0, 0
            for g in gs:
                s = pg.get(g.date) or {}
                n = s.get("bbe") or 0
                if n:
                    bbe += n
                    brl += s.get("barrels") or 0
                    if s.get("ev") is not None:
                        ev.append((s["ev"], n))
                    if s.get("xw") is not None:
                        xw.append((s["xw"], n))
                sw += s.get("swings") or 0
                wh += s.get("whiffs") or 0
            wmean = lambda p: (sum(v * k for v, k in p) / sum(k for _, k in p)
                               if p else None)
            table.setItem(r, 0, cell(lab, align_right=False))
            table.setItem(r, 1, cell(f"{sum(x.value for x in gs)/len(gs):.2f}"))
            e = wmean(ev)
            table.setItem(r, 2, cell(f"{e:.1f}" if e else "—"))
            table.setItem(r, 3, cell(f"{brl/bbe:.0%}" if bbe else "—"))
            x = wmean(xw)
            table.setItem(r, 4, cell(f"{x:.3f}".lstrip("0") if x else "—"))
            table.setItem(r, 5, cell(f"{wh/sw:.0%}" if sw else "—"))
        self._fit_table(table)
        self._sync_flank_widths()

    def _render_attack(self, summary: PropStatSummary):
        """Recent-window vs rest-of-season attack profile, on the Form tab."""
        table = self._tbl_attack
        table.setRowCount(0); table.clearSpans()
        rows = getattr(self, "_pitch_rows", None)
        games = self._filtered_games(summary)
        window = self._chart_window_combo.currentData() or 0
        recent = {g.date for g in (games[-window:] if window else games)}
        data = attack_profile(rows, recent) if rows else []
        if len(data) < 2:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(
                ["Attack profile — needs a full season to compare"])
            self._fit_table(table); self._sync_flank_widths(); return
        headers = ["Seen", "FB", "Brk", "Off", "Zone", "F-Str", "Whf"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        pct = lambda v: f"{v:.0%}" if v is not None else "—"
        keys = ("fb", "brk", "off", "zone", "fstr", "whiff")
        table.setRowCount(len(data) + 1)
        for r, d in enumerate(data):
            table.setItem(r, 0, cell(d["split"], align_right=False))
            for c, k in enumerate(keys, start=1):
                table.setItem(r, c, cell(pct(d[k])))
        # delta row — the reason the table exists
        r = len(data)
        lab = cell("\u0394", align_right=False)
        lab.setForeground(QColor("#95A5A6"))
        table.setItem(r, 0, lab)
        for c, k in enumerate(keys, start=1):
            a, b = data[0].get(k), data[1].get(k)
            if a is None or b is None:
                table.setItem(r, c, cell("—")); continue
            d = a - b
            it = cell(f"{d*100:+.0f}")
            # 5 points is the bar for calling a change; below that it is noise
            it.setForeground(QColor("#E67E22" if abs(d) >= 0.05
                                    else "#5D6D7E"))
            table.setItem(r, c, it)
        self._fit_table(table)
        self._sync_flank_widths()

    def _render_direction(self, summary: PropStatSummary):
        """Spray direction by pitch location, on the Form tab."""
        table = self._tbl_dir
        table.setRowCount(0); table.clearSpans()
        rows = getattr(self, "_pitch_rows", None)
        games = self._filtered_games(summary)
        window = self._chart_window_combo.currentData() or 0
        recent = {g.date for g in (games[-window:] if window else games)}
        data = spray_by_location(rows, recent) if rows else []
        if not data:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Direction — no located contact"])
            self._fit_table(table); self._sync_flank_widths(); return
        headers = ["Pitched", "BBE", "Pull", "Cent", "Oppo", "Seen", "\u0394Seen"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        pct = lambda v: f"{v:.0%}" if v is not None else "—"
        table.setRowCount(len(data))
        for r, d in enumerate(data):
            table.setItem(r, 0, cell(d["band"], align_right=False))
            table.setItem(r, 1, cell(f"{d['bbe']}"))
            for c, k in enumerate(("pull", "cent", "oppo", "seen"), start=2):
                table.setItem(r, c, cell(pct(d[k])))
            ds = d["dseen"]
            it = cell(f"{ds*100:+.0f}" if ds is not None else "—")
            # same 5-point bar as the attack table: below that it is noise
            it.setForeground(QColor("#E67E22" if ds is not None
                                    and abs(ds) >= 0.05 else "#5D6D7E"))
            table.setItem(r, 6, it)
        self._fit_table(table)
        self._sync_flank_widths()

    def _render_rest(self):
        """Days-of-rest splits on the Form tab."""
        table = self._tbl_rest
        table.setRowCount(0); table.clearSpans()
        rows = getattr(self, "_pitch_rows", None)
        data = rest_splits(rows) if rows else []
        if not data:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Rest — no data"])
            self._fit_table(table); self._sync_flank_widths(); return
        headers = ["Rest", "G", "BBE", "EV", "Brl", "xwOBA", "Whf"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        table.setRowCount(len(data))
        for r, d in enumerate(data):
            table.setItem(r, 0, cell(d["split"], align_right=False))
            table.setItem(r, 1, cell(f"{d['g']}"))
            table.setItem(r, 2, cell(f"{d['bbe']}"))
            table.setItem(r, 3, cell(f"{d['ev']:.1f}"))
            table.setItem(r, 4, cell(f"{d['brl']:.0%}"))
            table.setItem(r, 5, cell(f"{d['xw']:.3f}".lstrip("0")))
            table.setItem(r, 6, cell(f"{d['whiff']:.0%}"
                                     if d["whiff"] is not None else "—"))
        self._fit_table(table)
        self._sync_flank_widths()

    def _render_gamelog(self, summary: PropStatSummary):
        """Game log on the Context tab: the prop value per game joined to that
        game's Statcast, newest first.

        The Form plot shows the SHAPE of recent form; there was nowhere to
        read the actual numbers. Joined on date — `summary.games` carries the
        opponent and the prop value, `_pg_statcast` the batted-ball detail,
        and neither has both."""
        table = self._tbl_gamelog
        table.setRowCount(0)
        table.clearSpans()
        games = self._filtered_games(summary)
        window = self._chart_window_combo.currentData() or 0
        if window:
            games = games[-window:]
        if not games:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Game log — no games in window"])
            self._fit_table(table)
            self._sync_flank_widths()
            return
        stat = summary.stat_label or "Stat"
        headers = ["Date", "Opp", stat, "BBE", "EV", "Max", "Brl", "xwOBAcon",
                   "Whf%"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        line = summary.line
        pg = getattr(self, "_pg_statcast", {}) or {}
        table.setRowCount(len(games))
        for r, g in enumerate(reversed(games)):      # newest first
            s = pg.get(g.date) or {}
            table.setItem(r, 0, cell(g.date[5:], align_right=False))
            table.setItem(r, 1, cell(("@" if not g.is_home else "")
                                     + g.opponent, align_right=False))
            v = cell(f"{g.value:g}")
            if line is not None:
                v.setForeground(QColor(*(COLOR_OVER if g.value > line
                                         else COLOR_UNDER)))
            table.setItem(r, 2, v)
            table.setItem(r, 3, cell(f"{s['bbe']}" if s.get("bbe") else "—"))
            table.setItem(r, 4, cell(f"{s['ev']:.1f}" if s.get("ev") else "—"))
            table.setItem(r, 5, cell(f"{s['maxev']:.1f}"
                                     if s.get("maxev") else "—"))
            table.setItem(r, 6, cell(f"{s['barrels']}"
                                     if s.get("bbe") else "—"))
            table.setItem(r, 7, cell(f"{s['xw']:.3f}".lstrip("0")
                                     if s.get("xw") else "—"))
            table.setItem(r, 8, cell(f"{s['whiff']:.0%}"
                                     if s.get("whiff") is not None else "—"))
        self._fit_table(table)
        # _fit_table pins height to content, which is right for a 6-row table
        # and wrong here: with the window set to "All" this is 59+ rows and
        # would add ~1000px to a panel that already scrolls. Bound it and let
        # THIS table scroll internally instead — no rows are dropped.
        cap = 24 * 17 + table.horizontalHeader().height() + 2 * table.frameWidth()
        if table._natural_h > cap:
            table.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            table._natural_h = cap
            table.setFixedHeight(cap)
        else:
            table.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._sync_flank_widths()

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
        # Same race, and the spray chart lost it silently: the park wall is
        # drawn from `ctx.venue`, but only the SPRAY POINTS arriving triggered
        # a render. Whenever the context landed second the chart came up as a
        # bare scatter with no wall arc and no venue in the legend — which is
        # most of the chart's point, since the whole question is which of
        # those flies clear TONIGHT's fence.
        if self._spray_points and self._spray_action.isChecked():
            self._render_spray()

    # --------------------------------------------------------- pitch splits

    def set_pitch_splits_loading(self):
        self._pitch_toggle_row = -1
        self._tbl_pitch.clearSpans()
        for tbl, name in ((self._tbl_pitch, "Matchup"),
                          (self._tbl_velo, "Velo Bands"),
                          (self._tbl_pbot, "PitchingBot"),
                          (self._tbl_situ, "Situational")):
            tbl.setRowCount(0)
            tbl.setColumnCount(1)
            tbl.setHorizontalHeaderLabels([f"{name} — loading…"])
            tbl.setToolTip("")
            self._fit_table(tbl)
        self._spray_plot.getPlotItem().setTitle(None)
        self._set_spray_legend(
            "<span style='color:#7F8C8D; font-size:7pt'>loading…</span>")
        self._splits = None
        self._velo_splits = None
        self._splits_type = None
        self._situ_data = None
        self._fg_split_data = None
        # BMIELKE is the SLOWEST thing on this panel (it needs the full pitch
        # detail plus the prior season's), so leaving the previous player's
        # index up until it lands means the chip spends a second or two
        # attributing one hitter's contact quality to another — and it is not
        # obviously stale, because it is a plausible number either way. This
        # showed as James Wood reading 104: CJ Abrams' correct value, still on
        # screen. Clear it with everything else.
        self._bmielke = None
        self._pg_statcast = None
        self._arsenal = None          # opposing SP arsenal (batter view)
        self._arsenal_pitcher = None
        self._arsenal_stuff = None
        self._sp_card = None
        self._sp_card_name = ""
        self._spray_points = None
        self._swing_row.hide()

    def set_pitch_rows(self, rows: List[dict]):
        """Raw cached pitch detail — the attack profile needs per-pitch rows
        with dates, which none of the aggregated feeds preserve."""
        self._pitch_rows = rows
        self._render_rest()
        if self._summary is not None:
            self._render_attack(self._summary)
            self._render_direction(self._summary)

    def set_spray(self, points: List[tuple]):
        """Season spray points [(x_ft, y_ft, cat)] for the shown player."""
        self._spray_points = points
        if self._spray_action.isChecked():
            self._render_spray()

    def set_bmielke(self, bm: Optional[dict]):
        """BMIELKE for the shown batter — recomputed on the next chip render."""
        self._bmielke = bm
        self.show_swing(getattr(self, "_swing_fgb", None))

    # Park-adjusted index rows: (label, index key, raw key, raw format,
    # higher_is_better). Directionality is per row and NOT uniform — a high
    # K+ is bad and a high ISO+ is good, so a single colour rule would paint
    # half the table backwards.
    _PLUS_ROWS = (
        ("AVG",   "avg_plus",   "xavg",     "f3",  True),
        ("OBP",   "obp_plus",   None,       None,  True),
        ("SLG",   "slg_plus",   "xslg",     "f3",  True),
        ("ISO",   "iso_plus",   None,       None,  True),
        ("BABIP", "babip_plus", None,       None,  True),
        ("BB%",   "bb_plus",    None,       None,  True),
        ("K%",    "k_plus",     None,       None,  False),
        ("Hard%", "hard_plus",  "hard",     "pct", True),
        ("Soft%", "soft_plus",  "soft",     "pct", False),
        ("GB%",   "gb_plus",    None,       None,  None),
        ("FB%",   "fb_plus",    None,       None,  None),
        ("LD%",   "ld_plus",    None,       None,  True),
        ("HR/FB", "hrfb_plus",  "hr_fb",    "pct", True),
        ("Pull%", "pull_plus",  "pull_pct", "pct", None),
        ("Cent%", "cent_plus",  None,       None,  None),
        ("Oppo%", "oppo_plus",  None,       None,  None),
    )

    def _render_plus(self, fgb: Optional[dict]):
        """Park- and league-adjusted index family (100 = average)."""
        table = self._tbl_plus
        table.setRowCount(0)
        rows = [r for r in self._PLUS_ROWS
                if (fgb or {}).get(r[1]) is not None]
        if not rows:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Park-adjusted — no data"])
            self._fit_table(table)
            return
        headers = ["vs park", "Idx", "Raw"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        table.setRowCount(len(rows))
        for r, (label, ikey, rkey, rfmt, good_high) in enumerate(rows):
            idx = fgb.get(ikey)
            raw = fgb.get(rkey) if rkey else None
            if rfmt == "pct" and isinstance(raw, (int, float)):
                raw_s = f"{raw:.1%}"
            elif rfmt == "f3" and isinstance(raw, (int, float)):
                raw_s = f"{raw:.3f}".lstrip("0")
            else:
                raw_s = ""
            it_idx = cell(f"{idx:.0f}")
            # good_high None = a STYLE stat (GB/FB/Pull/Cent/Oppo). There is
            # no better direction for those, so they stay neutral rather than
            # implying one.
            if good_high is not None:
                edge = (idx - 100) * (1 if good_high else -1)
                if edge >= 15:
                    it_idx.setForeground(QColor("#2ECC71"))
                elif edge <= -15:
                    it_idx.setForeground(QColor("#E74C3C"))
            table.setItem(r, 0, cell(label, align_right=False))
            table.setItem(r, 1, it_idx)
            table.setItem(r, 2, cell(raw_s))
        table.resizeRowsToContents()
        self._fit_table(table)

    # Savant attack zones, keyed to the FanGraphs `sc*` columns.
    #
    # THE LETTERS WERE DERIVED, NOT GUESSED. FanGraphs documents no key for
    # these. Identification rests on four properties that hold to 1e-8 across
    # all 240 hitters with 250+ PA:
    #   * scH + scS + scC + scW shares  == 1.0000 exactly (they partition)
    #   * scS == scSI + scSO exactly    (Shadow splits in/out of zone)
    #   * scH + scSI == scZ exactly     (Heart + Shadow-in == in-zone)
    #   * scC + scW + scSO == scO       (the rest == out-of-zone)
    # and the league profile matches Savant's published attack-zone marks:
    # swing rate falls 71 -> 54 -> 26 -> 7 and contact 89 -> 78 -> 48 -> 13
    # across Heart/Shadow/Chase/Waste.
    #
    # (label, sc prefix, indent, league seen%, league swing%, league contact%,
    #  swing_high_is_good)
    _ATTACK_ZONES = (
        ("Heart",    "H",  False, 25.4, 71.4, 89.3, True),
        ("Shadow",   "S",  False, 41.6, 53.8, 78.1, None),
        ("· in zn",  "SI", True,  22.0, 60.1, 82.9, True),
        ("· out",    "SO", True,  19.5, 46.7, 71.1, False),
        ("Chase",    "C",  False, 23.0, 25.8, 48.3, False),
        ("Waste",    "W",  False,  9.9,  7.1, 12.8, False),
    )

    def _render_attack_zones(self, fgb_row: Optional[dict]):
        """Statcast attack zones off the raw FanGraphs board row."""
        table = self._tbl_az
        table.setRowCount(0)
        r0 = fgb_row or {}
        rows = [z for z in self._ATTACK_ZONES
                if r0.get(f"sc{z[1]}-Zone%") is not None]
        if not rows:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Attack zones — no data"])
            self._fit_table(table)
            return
        headers = ["Attack", "Seen", "Sw", "Con"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        table.setRowCount(len(rows))
        for r, (label, pre, indent, lg_seen, lg_sw, lg_con, sw_good) in \
                enumerate(rows):
            seen = (r0.get(f"sc{pre}-Zone%") or 0) * 100
            sw = (r0.get(f"sc{pre}-Swing%") or 0) * 100
            con = (r0.get(f"sc{pre}-Contact%") or 0) * 100
            lab = cell(label, align_right=False)
            if indent:
                lab.setForeground(QColor("#95A5A6"))
            else:
                fnt = lab.font(); fnt.setBold(True); lab.setFont(fnt)
            table.setItem(r, 0, lab)

            def mark(v, lg, good_high, tip):
                it = cell(f"{v:.0f}")
                it.setToolTip(f"{tip}: {v:.1f}% vs league {lg:.1f}%")
                if good_high is None:
                    return it
                edge = (v - lg) * (1 if good_high else -1)
                # Half a league sd on these rates is ~3 points; 4 keeps the
                # colour for differences that are actually visible.
                if edge >= 4:
                    it.setForeground(QColor("#2ECC71"))
                elif edge <= -4:
                    it.setForeground(QColor("#E74C3C"))
                return it

            # Seen% is the PITCHER's choice, not the hitter's — how often he
            # is attacked there. Left uncoloured: neither direction is the
            # hitter being good or bad.
            table.setItem(r, 1, mark(seen, lg_seen, None, "seen"))
            table.setItem(r, 2, mark(sw, lg_sw, sw_good, "swing"))
            table.setItem(r, 3, mark(con, lg_con, True, "contact"))
        table.resizeRowsToContents()
        self._fit_table(table)

    # Batter vs pitch TYPE, from the FanGraphs pfx block.
    # (label, FG code) — FG splits sweepers out as ST, unlike its Stuff+ side
    # which folds them into SL (see FG_PITCH_CODES).
    _PFX_TYPES = (("4-Seam", "FA"), ("Sinker", "SI"), ("Cutter", "FC"),
                  ("Slider", "SL"), ("Sweeper", "ST"), ("Curve", "CU"),
                  ("Knuckle Cv", "KC"), ("Change", "CH"), ("Splitter", "FS"))

    # MINIMUM PITCHES SEEN before a per-type run value is worth printing.
    # These figures are NOT sample-gated at source and the tails are wild —
    # a hitter reads +43.3 RV/100 on a knuckle curve he saw 1.7% of the time.
    # Measured sd of RV/100 against pitches seen (240 hitters, 250+ PA):
    #
    #   <25   25-50  50-75  75-100  100-150  150-250  250+
    #   6.60   3.99   3.19    2.42     1.99     1.69   1.35
    #
    # The curve elbows at ~100, where sd first drops under ~2 runs/100 and
    # then flattens. Below it the value still SHOWS, greyed with a dot, so a
    # thin type is visibly thin rather than silently missing.
    PFX_RV_MIN_PITCHES = 100

    def _render_pfx(self, fgb: Optional[dict]):
        """Run value per 100 pitches by pitch type, for the batter."""
        table = self._tbl_pfx
        table.setRowCount(0)
        f = fgb or {}
        total = f.get("pitches") or 0
        rows = []
        for label, code in self._PFX_TYPES:
            use = f.get(f"pfx{code}%")
            rv = f.get(f"pfxw{code}/C")
            if not use or rv is None:
                continue
            n = use * total
            rows.append((label, use, n, f.get(f"pfxv{code}"), rv))
        rows.sort(key=lambda r: -r[1])
        if not rows:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["vs pitch type — no data"])
            self._fit_table(table)
            return
        headers = ["vs type", "Use", "#", "Velo", "RV/100"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell
        table.setRowCount(len(rows))
        for r, (label, use, n, velo, rv) in enumerate(rows):
            thin = n < self.PFX_RV_MIN_PITCHES
            table.setItem(r, 0, cell(("· " if thin else "") + label,
                                     align_right=False))
            table.setItem(r, 1, cell(f"{use:.0%}"))
            table.setItem(r, 2, cell(f"{n:.0f}"))
            table.setItem(r, 3, cell(f"{velo:.1f}" if velo else "—"))
            it = cell(f"{rv:+.1f}")
            if thin:
                it.setForeground(QColor("#5D6D7E"))
                it.setToolTip(
                    f"{n:.0f} pitches — under the {self.PFX_RV_MIN_PITCHES} "
                    f"needed for this to mean much. At this sample the "
                    f"spread is ±3-7 runs/100 on noise alone.")
            else:
                it.setForeground(QColor("#2ECC71") if rv >= 1.0
                                 else QColor("#E74C3C") if rv <= -1.0
                                 else QColor("#BDC3C7"))
                it.setToolTip(f"{n:.0f} pitches seen. League spread at this "
                              f"sample is about ±2 runs/100.")
            table.setItem(r, 4, it)
        table.resizeRowsToContents()
        self._fit_table(table)

    def _render_bio(self, fgb: Optional[dict]):
        """Stance geometry + baserunning components."""
        table = self._tbl_bio
        table.setRowCount(0)
        f = fgb or {}
        pairs = []
        for label, key, fmt in (
                ("Depth in box", "depth_in_box", "{:.1f}\""),
                ("Off plate", "dist_off_plate", "{:.1f}\""),
                ("Tilt", "tilt", "{:.1f}°"),
                ("Swing len", "swing_length", "{:.1f}′"),
                ("Attack ang", "attack_angle", "{:+.1f}°"),
                ("Attack dir", "attack_dir", "{:+.1f}°"),
                ("Spd", "spd", "{:.1f}"),
                ("BsR", "bsr", "{:+.1f}"),
                ("UBR", "ubr", "{:+.1f}"),
                ("wSB", "wsb", "{:+.1f}"),
                ("XBR", "xbr", "{:+.1f}"),
                ("TTO%", "tto", "pct")):
            v = f.get(key)
            if v is None:
                continue
            pairs.append((label, f"{v:.1%}" if fmt == "pct"
                          else fmt.format(v)))
        if not pairs:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Stance / running — no data"])
            self._fit_table(table)
            return
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Stance / run", ""])
        cell = self._cell
        table.setRowCount(len(pairs))
        for r, (label, val) in enumerate(pairs):
            table.setItem(r, 0, cell(label, align_right=False))
            table.setItem(r, 1, cell(val))
        table.resizeRowsToContents()
        self._fit_table(table)

    def _render_leverage(self, fgb: Optional[dict]):
        """Situational value — WPA / WPA-LI / Clutch / pLI / RE24."""
        table = self._tbl_lev
        table.setRowCount(0)
        f = fgb or {}
        pairs = []
        for label, key, fmt in (("WPA", "wpa", "{:+.2f}"),
                                ("WPA/LI", "wpa_li", "{:+.2f}"),
                                ("Clutch", "clutch", "{:+.2f}"),
                                ("pLI", "pli", "{:.2f}"),
                                ("RE24", "re24", "{:+.1f}"),
                                ("REW", "rew", "{:+.2f}"),
                                ("wRAA", "wraa", "{:+.1f}")):
            v = f.get(key)
            if v is not None:
                pairs.append((label, fmt.format(v), key, v))
        if not pairs:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Situational — no data"])
            self._fit_table(table)
            return
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Situational", ""])
        cell = self._cell
        table.setRowCount(len(pairs))
        for r, (label, val, key, v) in enumerate(pairs):
            it = cell(val)
            # pLI is a LEVEL, not a good/bad quantity — batting in high
            # leverage is a lineup slot, not a virtue. Everything else is
            # signed so that positive is good.
            if key != "pli":
                it.setForeground(QColor("#2ECC71") if v > 0
                                 else QColor("#E74C3C") if v < 0
                                 else QColor("#BDC3C7"))
            table.setItem(r, 0, cell(label, align_right=False))
            table.setItem(r, 1, it)
        table.resizeRowsToContents()
        self._fit_table(table)

    # PitchingBot is on a 20-80 SCOUTING scale, NOT the 100-centred scale the
    # Stuff+ family uses — pb_stuff runs 38-66 with a mean of 52. Colouring it
    # against 100 would simply never fire, and colouring Loc+ against 50 would
    # paint every cell green. Marks measured off the 2026 board, starters with
    # >=40 IP (n=300; per-pitch n=289), as (mean, sd):
    _PBOT_MARKS = {
        ("ALL", "s"): (52.2, 5.0), ("ALL", "c"): (53.7, 3.8),
        ("ALL", "o"): (53.8, 3.9), ("ALL", "l"): (101.0, 5.9),
        ("P", "s"): (50.6, 8.8), ("P", "c"): (52.6, 7.6),
        ("P", "o"): (52.3, 8.2), ("P", "l"): (100.9, 10.5),
    }

    def show_sp_pitchingbot(self, fgp: Optional[dict], name: str = ""):
        """Opposing starter's PitchingBot grades, per pitch."""
        table = self._tbl_pbot
        table.setRowCount(0)
        f = fgp or {}
        per = f.get("pb_per_pitch") or {}
        loc = f.get("loc_per_pitch") or {}
        if not per and f.get("pb_overall") is None:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["PitchingBot — no data"])
            self._fit_table(table)
            return
        headers = ["Bot", "Stf", "Cmd", "Ovr", "Loc+"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        cell = self._cell

        def band(v, scope, key):
            it = cell("—" if v is None else f"{v:.0f}")
            if v is None:
                return it
            mean, sd = self._PBOT_MARKS[(scope, key)]
            z = (v - mean) / sd
            it.setForeground(QColor("#2ECC71") if z >= 1
                             else QColor("#E74C3C") if z <= -1
                             else QColor("#BDC3C7"))
            it.setToolTip(f"{v:.1f} — {z:+.1f} sd vs league "
                          f"({mean:.1f} ± {sd:.1f})")
            return it

        rows = [("ALL", "ALL", f.get("pb_stuff"), f.get("pb_command"),
                 f.get("pb_overall"), f.get("location"))]
        for code, d in sorted(per.items(),
                              key=lambda kv: -(kv[1].get("o") or 0)):
            rows.append(("P", code, d.get("s"), d.get("c"), d.get("o"),
                         loc.get(code)))
        table.setRowCount(len(rows))
        for r, (scope, label, s, c, o, l) in enumerate(rows):
            lab = cell(label, align_right=False)
            if r == 0:
                fnt = lab.font(); fnt.setBold(True); lab.setFont(fnt)
            table.setItem(r, 0, lab)
            for col, (v, key) in enumerate(
                    ((s, "s"), (c, "c"), (o, "o"), (l, "l")), start=1):
                table.setItem(r, col, band(v, scope, key))
        xrv, pera = f.get("pb_xrv100"), f.get("pb_era")
        tail = []
        if xrv is not None:
            tail.append(f"xRV/100 {xrv:+.2f}")
        if pera is not None:
            tail.append(f"botERA {pera:.2f}")
        if f.get("xera") is not None:
            tail.append(f"xERA {f['xera']:.2f}")
        if tail:
            table.setToolTip(table.toolTip() + "\n\n"
                             + (name + ": " if name else "")
                             + " · ".join(tail))
        table.resizeRowsToContents()
        self._fit_table(table)

    def show_swing(self, fgb: Optional[dict]):
        """Swing-tracking chip row under the matchup strip (batters):
        label-over-value badges with hot/cold coloring.

        Also drives the three tables that read the same FanGraphs row —
        park-adjusted indices, stance/baserunning and situational value —
        so they cannot get out of step with the chips above them."""
        self._swing_fgb = fgb
        self._render_plus(fgb)
        self._render_bio(fgb)
        self._render_leverage(fgb)
        self._render_attack_zones(fgb)
        self._render_pfx(fgb)
        self._sync_flank_widths()
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

        # BMIELKE leads the row — it is the summary the rest of these chips
        # are inputs to. It is the better number than xwOBAcon for the first
        # ~120 batted balls and an equal one after, so above the cap it greys
        # to mark "no longer an edge" rather than "no longer correct".
        bm = getattr(self, "_bmielke", None)
        if bm:
            # Above the cap the chip goes grey — but the caption says
            # "= xwOBAcon", NOT "use xwOBAcon", because parity is all that is
            # measured. Against plain xwOBAcon on the frozen 2026 test it wins
            # significantly through 120 batted balls (+0.263 at 25, +0.156 at
            # 80, +0.067 at 120) and then TIES (-0.000 at 150, -0.014 at 180,
            # both intervals straddling zero). It is never significantly worse
            # at any sample size. So the grey means "this has stopped being
            # your edge", not "this is now the wrong number".
            stale = bm["bbe"] > BMIELKE_MAX_BBE
            col = ("#7F8C8D" if stale else
                   "#2ECC71" if bm["index"] >= 110 else
                   "#E74C3C" if bm["index"] <= 90 else "#F4D03F")
            # the unit shows how much of the estimate is his OUTCOMES rather
            # than his swing — 14% at 25 batted balls, 63% by 250
            chip("BMIELKE", f"{bm['index']:.0f}"+("*" if stale else ""),
                 "= xwOBAcon" if stale
                 else f"{bm['outcome_weight']*100:.0f}% obs", col)
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
        # Full-width row now, so 7 chips fit on one line at ~70px each instead
        # of wrapping to two cramped lines of 4. Trailing stretch column keeps
        # them left-packed if there are fewer.
        n = self._SWING_CHIPS_PER_ROW
        for i, w in enumerate(chips):
            lay.addWidget(w, i // n, i % n)
        lay.setColumnStretch(n, 1)
        if bm:
            self._swing_row.setToolTip(
                "BMIELKE — Bat-Motion Index: Estimate from Leading "
                "Kinetic Evidence.\n\n"
                "A read on CONTACT QUALITY. 100 = league average, 10 = one "
                "standard deviation.\n\n"
                "Five swing terms (fast-swing rate, peak exit velocity, whiff "
                "rate, attack angle, contact depth) plus his own xwOBAcon "
                f"SHRUNK by how much of it there is — here "
                f"{bm['outcome_weight']*100:.0f}% weight on "
                f"{bm['bbe']} batted balls ({bm['wobacon']:.3f}), against a "
                f"prior of {bm['prior']:.3f}"
                + (f" set by his own last season ({bm['prior_weight']*100:.0f}% "
                   "of the way off league average).\n\n"
                   if bm['prior_weight'] else " (league average — no prior "
                   "season on file).\n\n") +
                "It exists because swing evidence LEADS batted-ball evidence "
                "— it saturates almost at once (r=+0.52 on 80 swings, +0.51 "
                "on a full season) while batted-ball evidence needs 200+ "
                "balls to catch up. On a frozen model tested on a season it "
                "never saw, predicting rest-of-season contact quality, it "
                "beats xwOBAcon by:\n"
                "   +0.263 at  25 batted balls\n"
                "   +0.275 at  30\n"
                "   +0.156 at  80\n"
                "   +0.067 at 120   (all intervals excluding zero)\n"
                "   -0.000 at 150   tie\n"
                "   -0.014 at 180   tie\n"
                "So it is the better number for roughly the first 120 balls "
                "in play — April into early June for a regular — and an equal "
                "one after. It is never significantly worse at any sample "
                "size. It also beats the previous version of itself by "
                "+0.077 / +0.077 / +0.052 at 25 / 30 / 80.\n\n"
                "SHORT TERM it holds up too, which is the horizon that "
                "matters here. Predicting the NEXT balls in play rather than "
                "the rest of the season (frozen, 2026):\n"
                "   next ~3 games   xwOBAcon +0.26   BMIELKE +0.36\n"
                "   next ~5 games   xwOBAcon +0.32   BMIELKE +0.43\n"
                "   next ~8 games   xwOBAcon +0.37   BMIELKE +0.50\n"
                "   next ~17 games  xwOBAcon +0.45   BMIELKE +0.60\n"
                "Those look low because a handful of batted balls is mostly "
                "noise — the most any predictor could score is 0.39/0.51/"
                "0.60/0.65 respectively, so BMIELKE is taking 83-94% of all "
                "the signal that exists. The gap left is the target's noise, "
                "not the model's error.\n\n"
                "There is no hot-hand term and that is deliberate: recent "
                "form MINUS his season line predicts the next 15-100 balls "
                "at r=-0.05 to -0.13. Streaks do not carry.\n\n"
                "It measures contact QUALITY, not hitter value: whiff "
                  "carries a positive weight because swing-and-miss predicts "
                  "harder contact on the balls they do hit. Contact hitters "
                  "grade low here and that is not a criticism of them.")
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
            plot.setTitle(None)
            self._set_spray_legend(
                "<span style='color:#7F8C8D; font-size:7pt'>loading…</span>")
            return
        if not points:
            plot.setTitle(None)
            self._set_spray_legend(
                "<span style='color:#7F8C8D; font-size:7pt'>no batted"
                " balls</span>")
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
            print(f"EffortMLB: spray wall draw failed: {e}")
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

        # No plot TITLE — the counts live in the vertical legend strip beside
        # the chart. A title row costs height, and this plot is aspect-locked
        # so height it cannot use anyway.
        plot.setTitle(None)
        self._set_spray_legend(
            "<div style='font-size:8pt; line-height:132%'>"
            f"<span style='color:#F1C40F'>{counts['HR']} HR</span><br>"
            f"<span style='color:#3498DB'>{counts['XBH']} XBH</span><br>"
            f"<span style='color:#2ECC71'>{counts['1B']} 1B</span><br>"
            f"<span style='color:#7F8C8D'>{counts['OUT']} out</span>"
            + (f"<br><span style='color:#7F8C8D; font-size:6pt'>"
               f"{wall_note.replace('<br>', '')}</span>" if wall_note else "")
            + "</div>")

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
            # The banner plots per-game xwOBAcon now, so it has to redraw when
            # the Statcast half lands — it arrives on a separate request after
            # the summary that first drew it.
            self._render_banner(self._summary)
            self._render_trend(self._summary)
            # The game log joins prop values to these aggregates on date, so
            # it has to redraw when the Statcast half lands (it arrives after
            # the summary, on a separate request).
            self._render_gamelog(self._summary)
            self._render_form_windows(self._summary)
            self._render_attack(self._summary)
            self._render_direction(self._summary)

    def _update_chart(self, summary: PropStatSummary):
        self._render_banner(summary)
        self._render_trend(summary)
        self._render_gamelog(summary)
        self._render_form_windows(summary)
        self._render_attack(summary)
        self._render_direction(summary)

    # -------------------------------------------------------------- banner

    def _render_banner(self, summary: PropStatSummary):
        """Per-game CONTACT QUALITY, with the prop outcome as the colour.

        This strip used to plot the prop stat per game with the over/under
        count beside it — which was two problems. It duplicated the trend plot
        directly below it (same values, same bars), and a hit rate is largely
        a property of the LINE rather than of the hitter: move the line half a
        base and "7 of 15" becomes "11 of 15" without him swinging differently.

        So the bar is now that game's xwOBAcon — did he actually hit the ball
        well — and whether the prop cleared is demoted to the colour. A row of
        tall red bars is the interesting case: he is squaring it up and the
        line is not paying, which is the one pattern the old strip could not
        show at all."""
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
                "<span style='font-size:7pt;color:#7F8C8D'>"
                "No games in window</span>")
            plot.addItem(self._banner_text)
            self._banner_text.setPos(0, 1)
            return
        pg_stat = getattr(self, "_pg_statcast", {}) or {}
        xs, hs, brushes, quality = [], [], [], []
        for i, g in enumerate(games):
            s_ = pg_stat.get(g.date) or {}
            xw = s_.get("xw")
            xs.append(i)
            # A game with no balls in play still gets a stub so the spacing
            # matches the trend plot's game-for-game layout underneath.
            hs.append(xw if xw else 0.02)
            if xw:
                quality.append(xw)
            brushes.append(pg.mkBrush(
                *(COLOR_OVER if g.value > line else COLOR_UNDER),
                130 if xw else 45))
        plot.addItem(pg.BarGraphItem(x=xs, height=hs, width=0.72,
                                     brushes=brushes, pen=None))
        lg = BMIELKE_LG_WOBACON
        plot.addItem(pg.InfiniteLine(pos=lg, angle=0,
                                     pen=pg.mkPen(*COLOR_LINE, 90, width=1)))
        ymax = max(max(hs), lg) * 1.32 + 0.02
        plot.setYRange(0, ymax, padding=0)
        plot.setXRange(-0.6, len(games) - 0.4, padding=0)
        mean_q = (sum(quality) / len(quality)) if quality else None
        overs = sum(1 for g in games if g.value > line)
        rc = ("#7FB88F" if mean_q and mean_q >= lg else "#B08080")
        self._banner_text.setHtml(
            "<span style='font-size:7pt; color:#7F8C8D;'>xwOBAcon by game"
            + (f" · <span style='color:{rc};'>{mean_q:.3f}</span>".replace(
                "0.", ".") if mean_q else "")
            + f" vs lg {lg:.3f}".replace("0.", ".")
            + f" · bars green when {summary.stat_label or 'stat'} "
              f"cleared {line:g} ({overs}/{len(games)})"
            + ("" if venue == "all" else f" · {venue}") + "</span>")
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
        "dhh": lambda v: f"{v:.0%}",
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
        # same domain as HH% ON PURPOSE — the two are meant to be read
        # against each other, and separate domains would hide the gap
        "dhh": (0.20, 0.65),     # spray-adjusted hard-hit rate
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
        if key == "dhh":
            bbe = rec.get("bbe") or 0
            v = rec.get("dhh")
            if not bbe or v is None:
                return None
            # show the count AND the plain rate beside it — the gap between
            # them is the whole reason this series exists
            hh = rec.get("hh")
            gap = "" if hh is None else f"  (HH {hh:.0%})"
            return f"SprayHard: {v*bbe:.0f}/{bbe}{gap}"
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
        # Drop the per-row stretch from the previous player so a shorter stack
        # (or a plain message row) doesn't inherit stale empty-row spacing
        for r in range(self._pct_grid.rowCount()):
            self._pct_grid.setRowStretch(r, 0)

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

        # Single stack that fills the column height: each bar gets an equal
        # share of the vertical space (row stretch), so the group spans the
        # full height alongside the splits table instead of a squat block.
        for i, bar in enumerate(bars):
            self._pct_grid.addWidget(bar, i, 0)
            self._pct_grid.setRowStretch(i, 1)

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

class ManagerBoardTab(QWidget):
    """League board of managerial game-decision tendencies.

    One row per team: how deep the starter goes, how the pen gets churned,
    the offensive/defensive levers the manager pulls, and — so the tendencies
    can be read against an outcome rather than admired on their own — that
    team's actual bullpen ERA/SIERA. The footer correlates every tendency
    against pen run prevention across the 30 clubs.

    Loads lazily on first show: a season of play-by-play per team is ~110
    requests, so the board fills in team by team rather than blocking."""

    # Emitted when the first full load settles (success OR failure) — the
    # startup overlay waits on it. Always emitted exactly once, or the window
    # would stay behind the loader.
    load_finished = pyqtSignal()

    # (key into the tendency dict, header, decimals, is_pct)
    _COLS = [
        ("sp_ip", "SP IP", 2, False), ("sp_pitches", "NP", 0, False),
        ("sp_short", "<5IP", 0, True), ("sp_mid_pull", "MidP", 0, True),
        ("opener_rate", "Opn", 0, True),
        ("pitchers_per_game", "P/G", 2, False),
        ("relief_ip", "RelIP", 1, False),
        ("rp_mid_inning", "RPmid", 0, True),
        ("rp_short_stint", "≤3BF", 0, True),
        ("rp_multi_inning", "Multi", 0, True),
        ("rp_close", "Close", 0, True),
        ("sp_tto3", "TTO3", 0, True), ("ir_strand", "IRS", 0, True),
        ("bq_per_start", "BQ/GS", 2, False),
    ]
    _DCOLS = [
        ("pinch_hit", "PH", 2, False), ("sac_bunt", "Bnt", 2, False),
        ("sb_att", "SB", 2, False), ("sb_rate", "SB%", 0, True),
        ("ibb", "IBB", 2, False), ("mound_visit", "MV", 1, False),
        ("def_move", "DefM", 1, False),
    ]

    # ABS challenges — new in 2026. NOT a managerial lever: only the batter,
    # pitcher or catcher may challenge, instantly, with no time to look into
    # the dugout. It is a club-discipline trait, and the league splits it
    # almost evenly between catchers (404 of 794 sampled) and batters (374),
    # with pitchers barely participating (16).
    _ABSCOLS = [
        ("abs_per_game", "Chal", 1, False),
        ("abs_rate", "Ovr%", 0, True),
        ("abs_rate_against", "OvrA%", 0, True),
        ("abs_cat_rate", "CatOv%", 0, True),
        ("abs_bat_rate", "BatOv%", 0, True),
        ("abs_late_close", "LateC", 0, True),
        ("abs_net_flips", "Flips", 0, False),
    ]

    # Priced with RE24 — these are the columns that say whether a lever
    # actually gained runs, rather than just how often it was pulled.
    _RVCOLS = [
        ("bunt_per", "BuntRV", 3, False), ("sb_per", "SBrv", 3, False),
        ("ibb_per", "IBBrv", 3, False), ("deploy_gap", "Deploy", 2, False),
    ]

    _GOOD = QColor(46, 204, 113)
    _BAD = QColor(231, 76, 60)

    def __init__(self, stats: Optional[MLBPropStats] = None, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._loaded = False
        self._rows: Dict[str, dict] = {}
        self._pen: Dict[str, dict] = {}
        self._siera: Dict[int, float] = {}
        self._rv: Dict[str, dict] = {}
        self._highlight: set = set()
        self._build_ui()

    def set_stats_backend(self, stats: MLBPropStats):
        self._stats = stats

    def set_highlight(self, abbrs):
        """Teams in the selected game — tinted so they're findable."""
        self._highlight = {a for a in abbrs if a}
        self._repaint_highlight()
        # The ladder and the hook curve are BOTH drawn per selected game off
        # _highlight, so repainting only the table left them showing the
        # previous game's clubs — a chart labelled STL/TOR under a PIT @ CIN
        # header. Harmless-looking and completely wrong. Only worth doing
        # once the board has actually loaded.
        if self._rows:
            self._render_plot()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        self._status = QLabel("Manager tendencies load on first view…")
        self._status.setStyleSheet("color: #7F8C8D; font-size: 9pt;")
        root.addWidget(self._status)

        heads = (["Team", "G"] + [h for _k, h, _d, _p in self._COLS]
                 + [h for _k, h, _d, _p in self._DCOLS]
                 + [h for _k, h, _d, _p in self._ABSCOLS]
                 + [h for _k, h, _d, _p in self._RVCOLS]
                 + ["penERA", "penSIERA"])
        self._table = QTableWidget()
        self._table.setColumnCount(len(heads))
        self._table.setHorizontalHeaderLabels(heads)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(18)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.setStyleSheet(_STATS_TABLE_QSS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setMinimumSectionSize(20)
        root.addWidget(self._table, stretch=1)

        # Sorted-bar view of any column on the board — the table answers
        # "what is this club's number", the chart answers "who is unusual"
        bar_row = QHBoxLayout()
        bar_row.setSpacing(6)
        self._plot_combo = QComboBox()
        self._plot_combo.setFixedHeight(20)
        # the two purpose-built views first, then any column as sorted bars
        self._plot_combo.addItems([self.VIEW_LADDER, self.VIEW_HOOK])
        self._plot_combo.insertSeparator(2)
        for _k, h, _d, _p in (self._COLS + self._DCOLS + self._ABSCOLS
                              + self._RVCOLS):
            self._plot_combo.addItem(h)
        self._plot_combo.addItem("penERA")
        self._plot_combo.setCurrentText(self.VIEW_LADDER)
        self._plot_combo.currentTextChanged.connect(
            lambda *_: self._render_plot())
        bar_row.addWidget(QLabel("Chart:"))
        bar_row.addWidget(self._plot_combo)
        bar_row.addStretch()
        root.addLayout(bar_row)

        self._plot = pg.PlotWidget(background="#151a21")
        self._plot.setFixedHeight(215)
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        root.addWidget(self._plot)

        self._corr = QLabel("")
        self._corr.setWordWrap(True)
        self._corr.setTextFormat(Qt.TextFormat.RichText)
        self._corr.setStyleSheet("color: #BDC3C7; font-size: 8pt;")
        root.addWidget(self._corr)

    VIEW_LADDER = "Deployment ladder"
    VIEW_HOOK = "Hook curve"
    # colours for the two clubs in tonight's game
    _TEAM_COLORS = ("#dc9437", "#5DADE2")

    def _axis_style(self, p, size=6):
        f = QFont()
        f.setPointSize(size)
        for axis in ("bottom", "left"):
            a = p.getAxis(axis)
            a.setPen(pg.mkPen(90, 100, 115))
            a.setTextPen(pg.mkPen(140, 150, 162))
            a.setStyle(tickFont=f)

    def _plot_ladder(self, p):
        """Each reliever placed by how good he is against how much of the
        late-and-close work he gets. A well-run pen slopes down to the right:
        the better the arm, the bigger the spots. A flat or rising cloud is a
        manager whose ladder is inverted."""
        names = {}
        try:
            names = {r["id"]: r["name"]
                     for r in (self._stats._roster or {}).values()}
        except Exception:
            pass
        teams = [t for t in sorted(self._highlight) if t in self._rows]
        if not teams:
            p.setTitle("Deployment ladder — select a game",
                       color="#95A5A6", size="8pt")
            return
        p.addLegend(offset=(-8, 8), labelTextSize="7pt")
        any_pt = False
        for ti, abbr in enumerate(teams[:2]):
            colour = QColor(self._TEAM_COLORS[ti % 2])
            xs, ys = [], []
            for pid, v in ((self._rows[abbr].get("by_pitcher")) or {}).items():
                s = self._siera.get(int(pid))
                if s is None or v["n"] < 8:
                    continue
                xs.append(v.get("high_lev", 0.0) * 100)
                ys.append(s)
                txt = pg.TextItem(
                    (names.get(int(pid), "").split()[-1] or "")[:9],
                    color=colour, anchor=(0.5, 1.25))
                txt.setPos(xs[-1], ys[-1])
                p.addItem(txt)
            if not xs:
                continue
            any_pt = True
            c = QColor(colour)
            p.addItem(pg.ScatterPlotItem(
                x=xs, y=ys, size=10, brush=pg.mkBrush(c),
                pen=pg.mkPen(20, 25, 32, width=2), name=abbr))
            # least-squares line: its slope IS the deployment story
            if len(xs) >= 3:
                import numpy as np
                b, a = np.polyfit(xs, ys, 1)
                x0, x1 = min(xs), max(xs)
                pen = pg.mkPen(c, width=2, style=Qt.PenStyle.DashLine)
                p.addItem(pg.PlotCurveItem([x0, x1], [a + b * x0, a + b * x1],
                                           pen=pen))
        if not any_pt:
            p.setTitle("Deployment ladder — no reliever SIERA yet",
                       color="#95A5A6", size="8pt")
            return
        p.getAxis("left").setLabel("SIERA (lower = better arm)",
                                   **{"color": "#7F8C8D", "font-size": "7pt"})
        p.getAxis("bottom").setLabel("% of his outings late & close",
                                     **{"color": "#7F8C8D",
                                        "font-size": "7pt"})
        p.setTitle("Deployment ladder — down-and-right means the good arms "
                   "get the big spots", color="#95A5A6", size="8pt")
        vb = p.getViewBox()
        vb.enableAutoRange()
        vb.updateAutoRange()
        (_x0, _x1), (y0, y1) = vb.viewRange()
        vb.setYRange(y0, y1 + (y1 - y0) * 0.12, padding=0)

    def _plot_hook(self, p):
        """Survival curve: how likely the starter is still out there after N
        batters. The vertical marks are where the lineup turns over — the
        third time through is the decision the research says matters."""
        league = []
        for d in self._rows.values():
            league.extend(d.get("sp_bf_list") or [])
        if not league:
            p.setTitle("Hook curve — no data yet", color="#95A5A6",
                       size="8pt")
            return
        xs = list(range(1, 33))

        def survival(vals):
            n = len(vals)
            return [sum(1 for v in vals if v >= x) / n for x in xs] if n else []

        p.addLegend(offset=(-8, 8), labelTextSize="7pt")
        p.addItem(pg.PlotCurveItem(xs, survival(league),
                                   pen=pg.mkPen(130, 140, 155, width=2),
                                   name="league"))
        for ti, abbr in enumerate([t for t in sorted(self._highlight)
                                   if t in self._rows][:2]):
            vals = self._rows[abbr].get("sp_bf_list") or []
            if not vals:
                continue
            p.addItem(pg.PlotCurveItem(
                xs, survival(vals),
                pen=pg.mkPen(QColor(self._TEAM_COLORS[ti % 2]), width=2),
                name=abbr))
        for x, lbl in ((10, "2nd time"), (19, "3rd time")):
            p.addItem(pg.InfiniteLine(
                pos=x, angle=90,
                pen=pg.mkPen(120, 130, 145, width=1,
                             style=Qt.PenStyle.DashLine)))
            t = pg.TextItem(lbl, color="#7F8C8D", anchor=(0, 0))
            t.setPos(x + 0.3, 1.0)
            p.addItem(t)
        p.getAxis("left").setLabel("P(starter still in)",
                                   **{"color": "#7F8C8D", "font-size": "7pt"})
        p.getAxis("bottom").setLabel("batters faced",
                                     **{"color": "#7F8C8D",
                                        "font-size": "7pt"})
        p.getViewBox().setRange(xRange=(1, 32), yRange=(0, 1.05), padding=0)
        p.setTitle("Hook curve — where each manager lets go",
                   color="#95A5A6", size="8pt")

    def _render_plot(self):
        """Sorted bars of the selected column, this game's clubs lit."""
        p = self._plot.getPlotItem()
        p.clear()
        try:
            p.legend.clear()
        except Exception:
            pass
        label = self._plot_combo.currentText()
        # clear first: labels set by one view otherwise bleed into the next
        for axis in ("bottom", "left"):
            p.getAxis(axis).setLabel("")
        if label in (self.VIEW_LADDER, self.VIEW_HOOK):
            self._axis_style(p, 7)
            p.getViewBox().enableAutoRange()
            (self._plot_ladder if label == self.VIEW_LADDER
             else self._plot_hook)(p)
            return
        lookup = {h: (k, src) for k, h, _d, _pc, src in
                  [(k, h, d, pc, "t") for k, h, d, pc in self._COLS]
                  + [(k, h, d, pc, "d") for k, h, d, pc in self._DCOLS]
                  + [(k, h, d, pc, "a") for k, h, d, pc in self._ABSCOLS]
                  + [(k, h, d, pc, "r") for k, h, d, pc in self._RVCOLS]}
        vals = []
        for abbr, d in self._rows.items():
            if label == "penERA":
                v = (self._pen.get(abbr) or {}).get("era")
            else:
                key, src = lookup.get(label, (None, None))
                if key is None:
                    continue
                bag = (d if src == "t" else (d.get("decisions") or {})
                       if src == "d" else (d.get("abs") or {})
                       if src == "a" else self._rv.get(abbr) or {})
                v = bag.get(key)
            if isinstance(v, (int, float)):
                vals.append((abbr, float(v)))
        if not vals:
            return
        vals.sort(key=lambda t: t[1])
        xs = list(range(len(vals)))
        ys = [v for _a, v in vals]
        brushes = [pg.mkBrush("#dc9437") if a in self._highlight
                   else pg.mkBrush(70, 100, 130) for a, _v in vals]
        p.addItem(pg.BarGraphItem(x=xs, height=ys, width=0.72,
                                  brushes=brushes, pen=None))
        ax = p.getAxis("bottom")
        ax.setTicks([[(i, a) for i, (a, _v) in enumerate(vals)]])
        f = QFont()
        f.setPointSize(6)
        ax.setStyle(tickFont=f)
        for axis in ("bottom", "left"):
            a2 = p.getAxis(axis)
            a2.setPen(pg.mkPen(90, 100, 115))
            a2.setTextPen(pg.mkPen(140, 150, 162))
        p.getAxis("left").setStyle(tickFont=f)
        p.addItem(pg.InfiniteLine(pos=0, angle=0,
                                  pen=pg.mkPen(120, 130, 145, width=1)))
        p.setTitle(label, color="#95A5A6", size="8pt")

    def showEvent(self, a0):
        super().showEvent(a0)
        self.ensure_loaded()

    def ensure_loaded(self):
        """Kick the load without needing to be shown.

        The startup overlay HIDES the whole UI while it builds, so showEvent
        never fires during startup and this board would sit empty until the
        overlay's watchdog gave up. Idempotent, so the showEvent path is
        still correct for a normal first view."""
        if not self._loaded and self._stats is not None:
            self._loaded = True
            asyncio.create_task(self._load())

    # ------------------------------------------------------------ loading

    async def _load(self):
        try:
            await self._load_inner()
        finally:
            # Exactly once, on every path — the startup overlay is waiting.
            self.load_finished.emit()

    async def _load_inner(self):
        try:
            async with self._stats.http() as session:
                if not await self._stats.ensure_roster(session):
                    return
                self._pen = await self._team_pen_quality()
                # Restore today's league tables BEFORE the per-club pass: a
                # club served from the slate cache returns without walking
                # play-by-play, so nothing else would fill them.
                self._stats.load_league_tables()
                teams = sorted(set(self._stats._teams.values()))
                done = [0]

                # ONE league-wide play-by-play pass before the per-club fan
                # out. Each game is downloaded once here instead of once per
                # club, which is where the cold-start cost of this tab was:
                # ~1,650 games were being fetched ~3,300 times. After this
                # the per-club calls below all hit the cache it just wrote.
                self._status.setText("Walking the league's play-by-play…")

                def _prog(n, total):
                    self._status.setText(
                        f"Play-by-play {n}/{total} games…")

                try:
                    await self._stats.prefetch_manager_tendencies(
                        session, _prog)
                except Exception as e:
                    # Non-fatal: the per-club path below still works, it is
                    # just back to being slow.
                    print(f"ManagerBoard: league prefetch failed: {e}")

                async def one(abbr):
                    try:
                        d = await self._stats.get_manager_tendencies(
                            session, abbr)
                    except Exception as e:
                        print(f"ManagerBoard: {abbr} failed: {e}")
                        d = None
                    done[0] += 1
                    if d:
                        self._rows[abbr] = d
                        self._add_row(abbr, d)
                    self._status.setText(
                        f"Loaded {done[0]}/{len(teams)} clubs…")
                    # Hand the loop back between clubs. Warm, every one of
                    # these is a cache hit, so all 30 rows would otherwise be
                    # built in one unbroken block with no repaint in between
                    # — which is exactly when the startup overlay freezes.
                    await asyncio.sleep(0)

                await asyncio.gather(*(one(t) for t in teams))
        except Exception as e:
            self._status.setText(f"Manager board failed: {e}")
            return
        # RE24 needs the whole league before the rare base-out cells are
        # trustworthy, so decisions are priced only once every club is in
        self._score_decisions()
        # ...which is also the only moment the league tables are complete
        self._stats.save_league_tables()
        self._status.setText(
            f"Manager & bullpen tendencies — {len(self._rows)} clubs, "
            f"{self._stats.season}. Click a header to sort.")
        self._render_correlations()
        self._repaint_highlight()
        self._render_plot()

    def _score_decisions(self):
        """Price every club's levers with the now-complete league RE24."""
        re24 = self._stats.run_expectancy()
        if len(re24) < 20:
            return
        for abbr, d in self._rows.items():
            rv: Dict[str, float] = {}
            scored = score_rv_events(d.get("rv_events") or [], re24)
            g = max(1, (d.get("decisions") or {}).get("games") or 1)
            for kind, key in (("bunt", "bunt_per"), ("ibb", "ibb_per")):
                if kind in scored:
                    rv[key] = scored[kind]["per"]
                    rv[kind + "_n"] = scored[kind]["n"]
            sb = steal_run_value(d.get("sb_by_base") or {},
                                 d.get("cs_by_base") or {}, re24)
            if sb:
                rv["sb_per"] = sb["per"]
                rv["sb_total"] = sb["rv"]
            gap = self._deployment_gap(d)
            if gap is not None:
                rv["deploy_gap"] = gap
            self._rv[abbr] = rv
        # repaint the RV columns now that they exist
        self._table.setRowCount(0)
        for abbr, d in self._rows.items():
            self._add_row(abbr, d)

    def _deployment_gap(self, d: dict) -> Optional[float]:
        """SIERA of the arms this manager uses late-and-close minus the arms
        he uses elsewhere. Negative = the good ones get the big spots.

        This is the cleanest managerial-skill measure here: it holds the
        roster fixed and asks only where the talent was spent."""
        hi_w, hi_s, lo_w, lo_s = 0.0, 0.0, 0.0, 0.0
        for pid, v in (d.get("by_pitcher") or {}).items():
            siera = self._siera.get(int(pid))
            if siera is None:
                continue
            n_hi = v["n"] * v.get("high_lev", 0.0)
            n_lo = v["n"] - n_hi
            hi_w += n_hi
            hi_s += n_hi * siera
            lo_w += n_lo
            lo_s += n_lo * siera
        if hi_w < 20 or lo_w < 20:
            return None
        return (hi_s / hi_w) - (lo_s / lo_w)

    async def _team_pen_quality(self) -> Dict[str, dict]:
        """Each club's RELIEF corps from the (cached) FG board — IP-weighted
        ERA and SIERA over pitchers who are not primarily starters. Far
        cheaper than 30 full bullpen reports and it gives the board an
        outcome column to read the tendencies against."""
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            None, fetch_fg_leaders_sync, "pit", "0", self._stats.season)
        agg: Dict[str, List] = {}
        for r in rows:
            tm = r.get("TeamNameAbb")
            tm = FG_TEAM_ALIAS.get(tm, tm)
            try:
                ip, g, gs = (float(r.get("IP") or 0), float(r.get("G") or 0),
                             float(r.get("GS") or 0))
                era, siera = float(r["ERA"]), float(r["SIERA"])
            except (TypeError, ValueError, KeyError):
                continue
            try:
                self._siera[int(r["xMLBAMID"])] = siera
            except (KeyError, TypeError, ValueError):
                pass
            if not tm or ip < 5 or (g and gs / g >= 0.5):
                continue
            agg.setdefault(tm, []).append((ip, era, siera))
        out = {}
        for tm, vals in agg.items():
            tot = sum(v[0] for v in vals)
            if tot:
                out[tm] = {
                    "era": sum(v[0] * v[1] for v in vals) / tot,
                    "siera": sum(v[0] * v[2] for v in vals) / tot,
                }
        return out

    # ------------------------------------------------------------- render

    @staticmethod
    def _num(val, dec, is_pct):
        """Sortable numeric cell — the display string would sort as text."""
        it = QTableWidgetItem()
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter)
        if val is None:
            it.setText("")
            return it
        it.setData(Qt.ItemDataRole.EditRole, float(val))
        it.setText(f"{val * 100:.{dec}f}%" if is_pct else f"{val:.{dec}f}")
        return it

    def _add_row(self, abbr: str, d: dict):
        t = self._table
        t.setSortingEnabled(False)
        r = t.rowCount()
        t.insertRow(r)
        name = QTableWidgetItem(abbr)
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        t.setItem(r, 0, name)
        t.setItem(r, 1, self._num(d.get("games"), 0, False))
        c = 2
        for key, _h, dec, pct in self._COLS:
            t.setItem(r, c, self._num(d.get(key), dec, pct))
            c += 1
        dec_d = d.get("decisions") or {}
        for key, _h, dec, pct in self._DCOLS:
            t.setItem(r, c, self._num(dec_d.get(key), dec, pct))
            c += 1
        abs_d = d.get("abs") or {}
        for key, _h, dec, pct in self._ABSCOLS:
            it = self._num(abs_d.get(key), dec, pct)
            v = abs_d.get(key)
            # An overturn rate is a coin-flip baseline: the league sits near
            # 55%, so colour against that rather than against zero. OvrA% is
            # the OPPONENTS succeeding, so its sign is inverted.
            if v is not None and key in ("abs_rate", "abs_rate_against",
                                         "abs_cat_rate", "abs_bat_rate"):
                good = v < 0.55 if key == "abs_rate_against" else v > 0.55
                it.setForeground(self._GOOD if good else self._BAD)
            t.setItem(r, c, it)
            c += 1
        rv = self._rv.get(abbr) or {}
        for key, _h, dec, pct in self._RVCOLS:
            it = self._num(rv.get(key), dec, pct)
            v = rv.get(key)
            if v is not None:
                # bunts/steals are batting-side (up is good); IBB is priced
                # from the pitching side and Deploy is a SIERA gap, where
                # NEGATIVE means the good arms get the big spots
                good = v < 0 if key == "deploy_gap" else v > 0
                it.setForeground(self._GOOD if good else self._BAD)
            t.setItem(r, c, it)
            c += 1
        pen = self._pen.get(abbr) or {}
        t.setItem(r, c, self._num(pen.get("era"), 2, False))
        t.setItem(r, c + 1, self._num(pen.get("siera"), 2, False))
        t.setSortingEnabled(True)

    def _repaint_highlight(self):
        for r in range(self._table.rowCount()):
            it = self._table.item(r, 0)
            if it is None:
                continue
            on = it.text() in self._highlight
            for c in range(self._table.columnCount()):
                cell = self._table.item(r, c)
                if cell is not None:
                    cell.setBackground(QColor("#1E2A38") if on
                                       else QColor(0, 0, 0, 0))

    def _render_correlations(self):
        """Do any of these levers actually go with better relief pitching?"""
        import numpy as np
        cols = self._COLS + self._DCOLS
        pen_era = []
        rows = []
        for abbr, d in self._rows.items():
            pen = self._pen.get(abbr)
            if not pen:
                continue
            dec = d.get("decisions") or {}
            vals = [(d.get(k) if k in d else dec.get(k))
                    for k, _h, _dd, _p in cols]
            if any(v is None for v in vals):
                continue
            rows.append(vals)
            pen_era.append(pen["era"])
        if len(rows) < 10:
            self._corr.setText("")
            return
        arr = np.array(rows, dtype=float)
        y = np.array(pen_era, dtype=float)
        out = []
        for i, (_k, h, _d, _p) in enumerate(cols):
            if arr[:, i].std() < 1e-9:
                continue
            out.append((h, float(np.corrcoef(arr[:, i], y)[0, 1])))
        out.sort(key=lambda t: -abs(t[1]))
        top = out[:6]
        body = " · ".join(
            f"<b style='color:{'#E74C3C' if r > 0 else '#2ECC71'}'>{h}</b> "
            f"{r:+.2f}" for h, r in top)
        self._corr.setText(
            f"<span style='color:#7F8C8D'>Tendency vs bullpen ERA across "
            f"{len(rows)} clubs (positive = goes with a WORSE pen; "
            f"association only, a manager's hand is partly forced by the "
            f"arms he has):</span><br>{body}")


class _NumItem(QTableWidgetItem):
    """Table cell that DISPLAYS formatted text but SORTS numerically.

    The obvious approach — `setText("0.287")` then `setData(EditRole, 0.287)`
    — does not work: on a QTableWidgetItem the display and edit roles share
    storage, so the EditRole write replaces the formatted string and the cell
    renders the raw float (`0.287498`). Keep the number somewhere else and
    compare on that instead."""

    _V = Qt.ItemDataRole.UserRole + 11

    def __init__(self, text: str, value: Optional[float]):
        super().__init__(text)
        if value is not None:
            self.setData(self._V, float(value))
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        a, b = self.data(self._V), other.data(self._V)
        if a is None and b is None:
            return super().__lt__(other)
        # blanks sort to the bottom whichever way the column is pointed
        if a is None:
            return False
        if b is None:
            return True
        return float(a) < float(b)


class ProjectionsTab(QWidget):
    """Rest-of-season projections, defaulting to tonight's participants.

    Everything else in this window is descriptive — what a pitcher HAS done,
    where his contact HAS gone. This is the only forward-looking surface, so
    it is deliberately kept separate rather than mixed into the SP card where
    it would read as another measurement.

    Scope defaults to tonight's slate rather than the league: a 700-row board
    is a FanGraphs page, and the reason to have it here is the men actually
    playing. The league view is one combo away for context."""

    # (json key, header, decimals) — rate stats first, counting after, since
    # a rest-of-season counting line is mostly a playing-time estimate
    _BAT = [("PA", "PA", 0), ("wRC+", "wRC+", 0), ("AVG", "AVG", 3),
            ("OBP", "OBP", 3), ("SLG", "SLG", 3), ("ISO", "ISO", 3),
            ("K%", "K%", 1), ("BB%", "BB%", 1), ("HR", "HR", 0),
            ("R", "R", 0), ("RBI", "RBI", 0), ("SB", "SB", 0),
            ("BABIP", "BABIP", 3), ("Spd", "Spd", 1), ("WAR", "WAR", 1)]
    _PIT = [("IP", "IP", 1), ("ERA", "ERA", 2), ("FIP", "FIP", 2),
            ("WHIP", "WHIP", 2), ("K/9", "K/9", 2), ("BB/9", "BB/9", 2),
            ("K%", "K%", 1), ("BB%", "BB%", 1), ("K-BB%", "K-BB%", 1),
            ("HR/9", "HR/9", 2), ("GB%", "GB%", 1), ("LOB%", "LOB%", 1),
            ("BABIP", "BABIP", 3), ("QS", "QS", 0), ("SV", "SV", 0),
            ("HLD", "HLD", 0), ("WAR", "WAR", 1)]
    # keys FanGraphs ships as a fraction but labels as a percentage
    _PCT_KEYS = {"K%", "BB%", "K-BB%", "GB%", "LOB%"}

    def __init__(self, stats, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._loaded = False
        self._slate_ids: Dict[int, tuple] = {}   # pid -> (abbr, role)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._sys = QComboBox()
        self._sys.setFixedHeight(20)
        for label, key in FG_PROJ_SYSTEMS:
            self._sys.addItem(label, key)
        self._side = QComboBox()
        self._side.setFixedHeight(20)
        self._side.addItem("Hitters", "bat")
        self._side.addItem("Pitchers", "pit")
        self._scope = QComboBox()
        self._scope.setFixedHeight(20)
        # "This game" first and default: on a full 15-game card "tonight's
        # slate" is all 30 clubs, i.e. the same 601 rows as "all of MLB",
        # which is a FanGraphs page rather than a reason to be in here.
        self._scope.addItem("This game", "game")
        self._scope.addItem("Tonight's slate", "slate")
        self._scope.addItem("All of MLB", "all")
        for c in (self._sys, self._side, self._scope):
            c.currentIndexChanged.connect(lambda *_: self._reload())
        bar.addWidget(QLabel("System:"))
        bar.addWidget(self._sys)
        bar.addWidget(self._side)
        bar.addWidget(self._scope)
        bar.addStretch()
        root.addLayout(bar)

        self._status = QLabel("Projections load on first view…")
        self._status.setStyleSheet("color: #7F8C8D; font-size: 9pt;")
        root.addWidget(self._status)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(18)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.setStyleSheet(_STATS_TABLE_QSS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setMinimumSectionSize(20)
        root.addWidget(self._table, stretch=1)

    # ------------------------------------------------------------- loading

    def set_game(self, teams, ids):
        """Tonight's two clubs and the player ids in them (rail + probables).
        `ids` = {pid: (abbr, role)}; role is 'B' or 'P'."""
        self._game_teams = tuple(t for t in (teams or ()) if t)
        self._slate_ids.update(ids or {})
        if self._loaded and self._scope.currentData() == "game":
            self._reload()

    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded:
            self._loaded = True
            asyncio.create_task(self._load())

    def _reload(self):
        if self._loaded:
            asyncio.create_task(self._load())

    async def _load(self):
        side = self._side.currentData()
        system = self._sys.currentData()
        scope = self._scope.currentData()
        self._status.setText("Loading projections…")
        try:
            async with self._stats.http() as session:
                await self._stats.ensure_roster(session)
                proj = await self._stats.get_projections(
                    session, stats=side, system=system)
                # the slate scope needs today's clubs, which the schedule
                # already has in hand
                games = await self._stats._get_schedule(session)
        except Exception as e:
            self._status.setText(f"Projections failed: {e}")
            return
        if not proj:
            self._status.setText(
                f"No {self._sys.currentText()} {self._side.currentText()} "
                "projections returned — FanGraphs may not publish this "
                "system for this side.")
            self._table.setRowCount(0)
            return

        want_teams = set()
        if scope == "slate":
            for g in games:
                for s in ("home", "away"):
                    t = (g.get("teams", {}).get(s, {}).get("team") or {})
                    ab = self._stats._teams.get(t.get("id"))
                    if ab:
                        want_teams.add(ab)
        elif scope == "game":
            want_teams = set(getattr(self, "_game_teams", ()) or ())
            if not want_teams:
                # tab opened before a game was picked — show the slate
                # rather than an empty table
                scope = "slate"
                for g in games:
                    for s in ("home", "away"):
                        t = (g.get("teams", {}).get(s, {}).get("team") or {})
                        ab = self._stats._teams.get(t.get("id"))
                        if ab:
                            want_teams.add(ab)

        by_id = {r["id"]: r for r in (self._stats._roster or {}).values()}
        rows = []
        for pid, r in proj.items():
            rec = by_id.get(pid)
            abbr = None
            if rec is not None:
                abbr = self._stats._teams.get(rec.get("team_id"))
            if abbr is None:
                # FanGraphs abbreviations differ for seven clubs
                fg = (r.get("Team") or "").strip()
                abbr = FG_TEAM_ALIAS.get(fg, fg)
            if want_teams and abbr not in want_teams:
                continue
            rows.append((pid, abbr, r))
        self._render(rows, side)
        scope_txt = {"slate": "tonight's slate", "game": "this game",
                     "all": "all of MLB"}[scope]
        self._status.setText(
            f"{self._sys.currentText()} · {self._side.currentText()} · "
            f"{scope_txt} — {len(rows)} players. Click a header to sort.")

    # ----------------------------------------------------------- rendering

    def _render(self, rows, side):
        cols = self._BAT if side == "bat" else self._PIT
        heads = ["Player", "Tm"] + [h for _k, h, _d in cols]
        self._table.setSortingEnabled(False)
        self._table.clear()
        self._table.setColumnCount(len(heads))
        self._table.setHorizontalHeaderLabels(heads)
        self._table.setRowCount(len(rows))
        for i, (pid, abbr, r) in enumerate(rows):
            nm = QTableWidgetItem(r.get("PlayerName") or "")
            self._table.setItem(i, 0, nm)
            tm = QTableWidgetItem(abbr or "")
            tm.setForeground(QColor("#7F8C8D"))
            self._table.setItem(i, 1, tm)
            for c, (key, _h, dec) in enumerate(cols, start=2):
                v = r.get(key)
                if isinstance(v, (int, float)):
                    shown = float(v) * 100 if key in self._PCT_KEYS else float(v)
                    txt = f"{shown:.{dec}f}"
                    # rate stats read as .287, not 0.287, like every other
                    # slash line in this window
                    if dec == 3 and txt.startswith("0."):
                        txt = txt[1:]
                    it = _NumItem(txt, shown)
                    self._colour(it, key, shown, side)
                else:
                    it = _NumItem("", None)
                self._table.setItem(i, c, it)
        self._table.setSortingEnabled(True)

    # (bad_at, good_at) — good_at may be BELOW bad_at where low is better,
    # which is how direction is encoded without a second lookup table.
    #
    # K% and BB% are in BOTH tables and mean OPPOSITE things by side: a 26%
    # strikeout rate is a good pitcher and a poor hitter. Bands are therefore
    # keyed per side, not globally.
    _BANDS_BAT = {"wRC+": (95, 115), "OBP": (.310, .350),
                  "SLG": (.390, .450), "ISO": (.130, .200),
                  "AVG": (.240, .275), "K%": (26, 18), "BB%": (6, 10),
                  "WAR": (0.3, 1.2)}
    _BANDS_PIT = {"ERA": (4.40, 3.60), "FIP": (4.40, 3.60),
                  "WHIP": (1.32, 1.15), "K%": (20, 26), "BB%": (9.5, 6.5),
                  "K-BB%": (12, 20), "K/9": (7.5, 10.0), "BB/9": (3.6, 2.4),
                  "HR/9": (1.35, 1.00), "WAR": (0.3, 1.2)}

    def _colour(self, item, key, v, side):
        band = (self._BANDS_BAT if side == "bat" else self._BANDS_PIT).get(key)
        if not band:
            return
        bad_at, good_at = band
        if good_at >= bad_at:                 # higher is better
            good, bad = v >= good_at, v <= bad_at
        else:                                 # lower is better
            good, bad = v <= good_at, v >= bad_at
        if good:
            item.setForeground(QColor("#2ECC71"))
        elif bad:
            item.setForeground(QColor("#E74C3C"))


class PenAvailBar(QWidget):
    """Both pens' availability, one cell per arm, on the header row.

    Fills the 671x17 strip left empty beside the `AVAIL SIERA / PEN IP`
    label — the only sizeable gap left in the pitcher column.

    That label already compares the two clubs, but only as four numbers, and
    the table below shows ONE club at a time. What neither can say is the
    SHAPE of a pen: `2.92` is the same figure whether it comes from eight
    rested arms or from four rested and four that pitched yesterday. One
    cell per reliever, coloured by status, answers "who can he actually call
    tonight" for both clubs at once and at a glance."""

    _COLORS = [("FRESH", "#2ECC71"), ("", "#5D8AA8"), ("USED YDAY", "#E67E22"),
               ("DOUBTFUL", "#E59866"), ("TAXED", "#E74C3C"),
               ("UNAVAIL", "#566573")]
    _ORDER = [s for s, _ in _COLORS]
    # "A" for the unflagged arms, not a dot — a "4·" tally read as a
    # truncated number rather than as a category
    _SHORT = {"FRESH": "F", "": "A", "USED YDAY": "Y", "DOUBTFUL": "D",
              "TAXED": "T", "UNAVAIL": "U"}
    _CELL = 13.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(17)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Fixed)
        self._clubs = []          # [(abbr, [status,...])]
        self.setToolTip(
            "Every arm in both pens tonight, one cell each, ordered by "
            "leverage — the highest-leverage reliever is leftmost.\n\n"
            "green = fresh (no work in three days)   blue = available\n"
            "orange = pitched yesterday   amber = doubtful\n"
            "red = taxed   grey = unavailable\n\n"
            "The AVAIL SIERA figure beside this cannot show shape: the same "
            "2.92 comes from eight rested arms or from four rested and four "
            "that threw yesterday.")

    def set_pens(self, clubs):
        """clubs = [(abbr, [reliever dict, ...]), ...] in matchup order."""
        out = []
        for abbr, rows in (clubs or []):
            if not rows:
                continue
            ordered = sorted(rows,
                             key=lambda r: -((r.get("fg") or {}).get("gmli")
                                             or 0))
            out.append((abbr, [(r.get("status") or "") for r in ordered]))
        self._clubs = out
        # occupy the row only when there is something to draw — the panel
        # height budget counts this row, so an always-visible empty bar
        # would reserve 17px of nothing
        self.setVisible(bool(out))
        self.update()

    def paintEvent(self, _e):
        if not self._clubs:
            return
        w, h = self.width(), self.height()
        if w < 120:
            return                      # too narrow to say anything
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        f = QFont(); f.setPointSize(6)
        p.setFont(f)
        colors = dict(self._COLORS)
        fm = p.fontMetrics()
        # Pack LEFT with a fixed cell width rather than splitting the strip
        # in halves: an even split right-aligned each tally to its own half
        # and opened a dead gap between the clubs. Cells are the same size
        # for both pens, so the bars are directly comparable in length —
        # which is the point.
        cell = self._CELL
        x = 4.0
        for abbr, statuses in self._clubs:
            counts = {}
            for s in statuses:
                counts[s] = counts.get(s, 0) + 1
            tally = " ".join(f"{counts[s]}{self._SHORT[s]}"
                             for s in self._ORDER if counts.get(s))
            bw = cell * len(statuses)
            need = 26 + bw + 6 + fm.horizontalAdvance(tally) + 16
            if x + need > w:
                break                   # ran out of strip; draw no partials
            p.setPen(QColor("#95A5A6"))
            p.drawText(QRectF(x, 0, 24, h),
                       Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter, abbr)
            bx = x + 26
            for j, s in enumerate(statuses):
                r = QRectF(bx + j * cell + 0.5, 3.0, cell - 1.5, h - 6.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(colors.get(s, "#5D8AA8")))
                p.drawRect(r)
            p.setPen(QColor("#7F8C8D"))
            p.drawText(QRectF(bx + bw + 6, 0, fm.horizontalAdvance(tally) + 4,
                              h),
                       Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter, tally)
            x += need
        p.end()


class PenUsageStrip(QWidget):
    """Each reliever's last week of work, drawn beside his row in the table.

    Lives in the block the pen table leaves empty — the table hugs its
    content at 855px inside a 976px (rail open) / 1065px (collapsed) panel,
    so there is 121-210px of free panel to its right and nothing else in the
    window wants a tall narrow shape.

    The table already carries Yd / -2 / -3 / L3 / L7 as NUMBERS, but
    `np_by_day` holds the full daily sequence and the numbers cannot show a
    PATTERN. Three straight days of 15 and one day of 45 both read as L3 45;
    they are not the same arm tonight. One cell per day, oldest on the left,
    today on the right — normal reading order, so the eye ends on the most
    recent outing.

    Row geometry is read back from the table rather than assumed, so the
    strip stays aligned if row heights ever change."""

    _DAYS = 7
    _CELL_MIN = 9

    def __init__(self, table: QTableWidget, parent=None):
        super().__init__(parent)
        self._table = table
        self._rows: List[dict] = []
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Ignored)
        self.setToolTip(
            "Each reliever's last seven days of work — one cell per day, "
            "oldest on the left, YESTERDAY hard against the table.\n\n"
            "Brightness is pitch count: hollow = did not pitch, dim = light "
            "(under 15), mid = a normal outing, hot = 25+.\n\n"
            "The table's Yd / -2 / -3 / L3 / L7 columns carry the same "
            "numbers, but three straight days of 15 and one day of 45 both "
            "read as L3 45 — and they are not the same arm tonight.")

    def set_rows(self, rows: List[dict]):
        self._rows = list(rows or [])
        self.update()

    def paintEvent(self, _e):
        t = self._table
        if not self._rows or not t.rowCount():
            return
        w = self.width()
        if w < self._CELL_MIN * 3:
            return                       # too narrow to be honest about
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        hdr_h = t.horizontalHeader().height() + t.frameWidth()
        days = max(3, min(self._DAYS, int(w // self._CELL_MIN)))
        cw = w / float(days)

        f = QFont(); f.setPointSize(6)
        p.setFont(f)
        p.setPen(QColor("#7F8C8D"))
        p.drawText(QRectF(0, 0, w, hdr_h), Qt.AlignmentFlag.AlignCenter,
                   f"last {days}d →")

        for i, rec in enumerate(self._rows):
            if i >= t.rowCount():
                break
            y = hdr_h + t.rowViewportPosition(i)
            rh = t.rowHeight(i)
            # Skip only rows that start past the bottom. Testing the row's
            # END dropped the whole cell for a row hanging a few pixels over,
            # so the last reliever silently lost his week whenever this strip
            # came out marginally shorter than the table — which is what a
            # layout measured before Qt resolved it does. QPainter clips the
            # overhang by itself; a partially drawn last row is honest, an
            # absent one is not.
            if rh <= 0 or y >= self.height():
                continue
            # newest first in the data; draw oldest LEFT so time runs toward
            # the table, i.e. toward tonight
            nps = [n for _, n in (rec.get("np_by_day") or [])][:days]
            nps = list(reversed(nps + [0] * (days - len(nps))))
            for d, n in enumerate(nps):
                x = d * cw
                r = QRectF(x + 1, y + 3, max(2.0, cw - 2), max(2.0, rh - 6))
                if not n:
                    p.setPen(QColor("#2b3540"))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRect(r)
                    continue
                # one ramp, so a long light week and a single heavy day are
                # distinguishable at a glance
                if n >= 25:
                    c = QColor("#E74C3C")
                elif n >= 15:
                    c = QColor("#E67E22")
                else:
                    c = QColor("#5D8AA8")
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawRect(r)
                if cw >= 16 and rh >= 14:
                    p.setPen(QColor("#0e1318" if n >= 15 else "#DDE4EA"))
                    p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(n))
        p.end()


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
        # both pens of the selected game, for the availability edge
        self._matchup: Optional[tuple] = None
        self._pen_cache: Dict[str, List[dict]] = {}
        self._mgr_cache: Dict[str, Optional[dict]] = {}
        self._sp_ids: Dict[str, int] = {}
        self._relief_ip: Dict[str, Optional[tuple]] = {}
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
        # Matchup strip: the two pens of tonight's game, full panel width.
        # This used to ride inside header section 0 alongside the combo, which
        # cost twice over — the section had to grow to contain the whole
        # banner (~150px stolen from every reliever row, in a table that needs
        # ~882px to avoid a scrollbar) and the text was CLIPPED at the section
        # edge anyway. One 17px strip buys that width back on every row.
        self._edge_label = QLabel("")
        self._edge_label.setTextFormat(Qt.TextFormat.RichText)
        self._edge_label.setFixedHeight(17)
        self._edge_label.setStyleSheet(
            "font-size: 8pt; padding: 0px 6px;"
            "background: #131820; border: 1px solid #222b36;"
            "border-bottom: none;")
        # Hugs its text rather than stretching: it reads as a tab sticking out
        # of the top of the table, not as a full-width banner.
        self._edge_label.setSizePolicy(QSizePolicy.Policy.Maximum,
                                       QSizePolicy.Policy.Fixed)
        self._edge_label.setVisible(False)      # nothing to compare yet
        edge_row = QHBoxLayout()
        edge_row.setContentsMargins(0, 0, 0, 0)
        edge_row.setSpacing(0)
        edge_row.addWidget(self._edge_label)
        # 671x17 sat empty to the right of that label — the last real gap in
        # the pitcher column. Both pens are already in _pen_cache.
        self._avail_bar = PenAvailBar()
        edge_row.addWidget(self._avail_bar, stretch=1)
        root.addLayout(edge_row)
        # The table hugs its content width, so it will not stretch here; the
        # usage strip takes the panel width left over beside it (121px rail
        # open, 210px collapsed) and renders nothing below ~27px.
        table_row = QHBoxLayout()
        table_row.setContentsMargins(0, 0, 0, 0)
        table_row.setSpacing(3)
        table_row.addWidget(self._table)
        self._usage = PenUsageStrip(self._table)
        table_row.addWidget(self._usage, stretch=1)
        root.addLayout(table_row)
        root.addStretch(0)

        # In-header controls: combo + "PEN #n" rank riding on section 0. Both
        # fit inside the width the reliever NAMES already need, so they are
        # free; the matchup text, which does not, lives in the strip above.
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
        # The h-scrollbar allowance in _cap_panel_height depends on the
        # viewport width, which is not settled during the first render — so
        # re-cap whenever the bar's range changes (it appears or goes away).
        self._table.horizontalScrollBar().rangeChanged.connect(
            lambda *_: self._cap_panel_height())
        self._place_hdr_box()

    def _place_hdr_box(self):
        hdr = self._table.horizontalHeader()
        self._hdr_box.setGeometry(hdr.sectionViewportPosition(0), 0,
                                  hdr.sectionSize(0), hdr.height())

    # ------------------------------------------------------------- control

    def set_teams(self, abbrs: List[str]):
        """Populate the team combo once the roster/team list is known."""
        # Restore from _current_team, NOT from currentText(): the team list
        # arrives asynchronously and usually LATER than the first
        # show_team() from game selection, at which point the combo is still
        # empty and currentText() is "". Repopulating then left the combo
        # displaying whatever sorted first (ATH) while the table below showed
        # the team that was actually loaded — the label and the rows
        # disagreed, with no error.
        current = self._current_team or self._team_combo.currentText()
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

    # Starts of regression weight pulling a starter's own leash toward his
    # manager's norm — a man with three starts shouldn't set tonight's
    # expectation on his own, one with twenty should.
    _LEASH_SHRINK = 5.0

    def set_matchup(self, away: str, home: str,
                    sp_ids: Optional[Dict[str, int]] = None):
        """The two pens in today's game — drives the availability edge in the
        header. Both reports are cached, so flipping the combo between them
        costs nothing. `sp_ids` maps team abbr -> tonight's probable starter,
        which sharpens the relief-innings estimate."""
        self._matchup = (away, home)
        self._sp_ids = dict(sp_ids or {})
        self._edge_label.setText("")
        asyncio.create_task(self._fetch_matchup(away, home))

    async def _expected_relief_ip(self, abbr: str) -> Optional[tuple]:
        """(relief IP the pen must cover, expected SP IP, source label).

        Tonight's starter beats the team average, but his own IP/GS is noisy
        early, so it is shrunk toward the manager's season norm."""
        m = self._mgr_cache.get(abbr)
        team_ip = (m or {}).get("sp_ip")
        if team_ip is None:
            return None
        pid = (self._sp_ids or {}).get(abbr)
        if not pid:
            return (9.0 - team_ip, team_ip, "team avg")
        try:
            async with self._stats.http() as session:
                form = await self._stats.get_sp_form(session, pid)
        except Exception:
            form = None
        leash = (form or {}).get("leash") or {}
        n, ip = leash.get("starts"), leash.get("ip_per_start")
        if not n or ip is None:
            return (9.0 - team_ip, team_ip, "team avg")
        # An opener must be regressed toward the OPENER baseline. Pulling him
        # toward the club's starter norm (~5.2 IP) is the one case where the
        # shrinkage actively destroys the estimate: it would hand the pen
        # three innings it does not have to cover and hide a bullpen game.
        if leash.get("is_opener"):
            target, label = OPENER_IP_BASELINE, "opener baseline"
        else:
            target, label = team_ip, f"team {team_ip:.2f}"
        exp = (n * ip + self._LEASH_SHRINK * target) / (n + self._LEASH_SHRINK)
        note = "OPENER — " if leash.get("is_opener") else ""
        return (9.0 - exp, exp,
                f"{note}SP {ip:.2f} over {n} GS, shrunk to {label}")

    async def _fetch_matchup(self, away: str, home: str):
        for abbr in (away, home):
            if abbr in self._pen_cache:
                continue
            try:
                async with self._stats.http() as session:
                    self._pen_cache[abbr] = \
                        await self._stats.get_bullpen_report(session, abbr)
            except Exception as e:
                print(f"BullpenPanel: matchup fetch failed for {abbr}: {e}")
                return
        if self._matchup == (away, home):
            self._render_edge()
        # Manager tendencies are a second, slower pass (a season of
        # play-by-play per team) — the pen scores shouldn't wait on them
        for abbr in (away, home):
            if abbr in self._mgr_cache:
                continue
            try:
                async with self._stats.http() as session:
                    self._mgr_cache[abbr] = \
                        await self._stats.get_manager_tendencies(session, abbr)
            except Exception as e:
                print(f"BullpenPanel: tendencies failed for {abbr}: {e}")
                continue
            try:
                self._relief_ip[abbr] = await self._expected_relief_ip(abbr)
            except Exception as e:
                print(f"BullpenPanel: relief-IP estimate failed "
                      f"for {abbr}: {e}")
            if self._matchup == (away, home):
                self._render_edge()
                # deployment tooltips need the tendencies, which arrive after
                # the table was first painted
                if self._current_team == abbr and self._pen_cache.get(abbr):
                    self._render(self._pen_cache[abbr])

    def _render_edge(self):
        """`NYY 3.94 · CHC 4.31` — availability-adjusted, better pen lit."""
        if not self._matchup:
            return
        away, home = self._matchup
        # the shape bar only needs the rows, so it fills even when one club's
        # pen_strength is missing and the numeric edge below cannot render
        self._avail_bar.set_pens(
            [(a, self._pen_cache.get(a) or []) for a in (away, home)])
        scores = {}
        for abbr in (away, home):
            rows = self._pen_cache.get(abbr)
            if rows:
                s = pen_strength(rows)
                if s and s["avail"] is not None:
                    scores[abbr] = s
        if len(scores) < 2:
            self._edge_label.setText("")
            self._edge_label.setVisible(False)
            return
        self._edge_label.setVisible(True)
        best = min(scores, key=lambda a: scores[a]["avail"])
        parts, tips = [], []
        for abbr in (away, home):
            s = scores[abbr]
            colour = "#2ECC71" if abbr == best else "#95A5A6"
            # a pen that lost arms carries a warning triangle + the cost
            worse = (s["delta"] or 0) >= 0.15
            # the pen currently IN the table is underlined, so the strip and
            # the rows below it can never appear to disagree
            shown = abbr == self._current_team
            parts.append(
                f"<span style='color:{colour}'>"
                f"{'<u>' if shown else ''}{abbr}{'</u>' if shown else ''} "
                f"<b>{s['avail']:.2f}</b></span>"
                + (f"<span style='color:#E67E22'> ▲{s['delta']:.2f}</span>"
                   if worse else ""))
            key_out = s.get("out_key") or []
            tips.append(
                f"{abbr}: available {s['avail']:.2f} vs full-strength "
                f"{s['full']:.2f} SIERA"
                + (f"  —  {len(key_out)} of the top {len(PEN_LEVERAGE_LADDER)} "
                   f"out: {', '.join(key_out)}" if key_out
                   else (f"  ({s['n_out']} low-leverage arm(s) out)"
                         if s["out"] else "  (everyone available)")))
            m = self._mgr_cache.get(abbr)
            if m and m.get("relief_ip") is not None:
                # innings the pen actually has to cover behind TONIGHT's
                # starter, not the season average
                rip, exp_ip, src = (self._relief_ip.get(abbr)
                                    or (m["relief_ip"], m["sp_ip"], "team avg"))
                # A bullpen game is the single biggest swing in what this pen
                # is being asked to do tonight — it does not belong in a
                # tooltip only.
                opener = src.startswith("OPENER")
                parts[-1] += (
                    f"<span style='color:{'#E67E22' if opener else '#7F8C8D'}'>"
                    f"/{rip:.1f}ip{' OPN' if opener else ''}</span>")
                tips.append(
                    f"    {abbr} manager ({m['games']}g): starter goes "
                    f"{m['sp_ip']:.2f} IP on {m['sp_pitches']:.0f} pitches, "
                    f"{m['sp_short']:.0%} of starts under 5, "
                    f"{m['sp_mid_pull']:.0%} pulled mid-inning."
                    + (f"\n      Opens {m['opener_rate']:.0%} of games; a real "
                       f"start goes {m['sp_ip_trad']:.2f} IP."
                       if m.get("opener_rate") else "")
                    + f"\n      Tonight the pen covers ~{rip:.1f} IP "
                    f"(expected SP {exp_ip:.2f} — {src}) on "
                    f"{m['rp_per_game']:.1f} arms."
                    f"\n      {m['rp_mid_inning']:.0%} of relief outings enter "
                    f"mid-inning, {m['rp_short_stint']:.0%} face 3 batters or "
                    f"fewer, {m['rp_multi_inning']:.0%} go multi-inning, "
                    f"{m['rp_close']:.0%} arrive within 2 runs.")
                d = m.get("decisions") or {}
                if d:
                    tips.append(
                        f"      per game: {d['pinch_hit']:.1f} PH, "
                        f"{d['sac_bunt']:.2f} bunts, {d['sb_att']:.1f} SB att"
                        + (f" ({d['sb_rate']:.0%})" if d.get("sb_rate")
                           else "")
                        + f", {d['ibb']:.2f} IBB, "
                          f"{d['mound_visit']:.1f} mound visits, "
                          f"{d['def_move']:.1f} def moves.")
        # The strip has to read on its own — the numbers are a SIERA and an
        # innings load, which nothing else on the panel implies.
        self._edge_label.setText(
            "<span style='color:#5b6674'>AVAIL SIERA / PEN IP&nbsp;&nbsp;</span>"
            + "&nbsp;&nbsp;<span style='color:#3d4652'>vs</span>&nbsp;&nbsp;"
              .join(parts))
        self._edge_label.setToolTip(
            "Leverage-weighted pen SIERA, renormalised over the arms who can "
            "actually pitch today (lower is better).\n▲ = how much worse the "
            "pen is than on paper.\n\n" + "\n".join(tips))
        self._cap_panel_height()
        self._place_hdr_box()

    async def _fetch(self, abbr: str):
        try:
            async with self._stats.http() as session:
                rows = await self._stats.get_bullpen_report(session, abbr)
        except Exception as e:
            print(f"BullpenPanel: fetch failed for {abbr}: {e}")
            return
        self._pen_cache[abbr] = rows
        if self._current_team == abbr:
            self._render(rows)
            self._render_edge()

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
            # What game states this manager ACTUALLY brings him into — a
            # closer spent only on 1-3 run leads is a different asset from
            # one who appears in ties.
            dep = ((self._mgr_cache.get(self._current_team) or {})
                   .get("by_pitcher") or {}).get(rec.get("pid"))
            if dep:
                role.setToolTip(
                    f"{rec['name']} — {dep['n']} relief outings\n"
                    f"entered: {dep['save_spot']:.0%} up 1-3, "
                    f"{dep['big_lead']:.0%} up 4+, {dep['tie']:.0%} tied, "
                    f"{dep['trail']:.0%} trailing\n"
                    f"avg inning {dep['avg_inning']:.1f} · "
                    f"{dep['mid_inning']:.0%} enter mid-inning · "
                    f"{dep['avg_bf']:.1f} batters faced")

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
        # Room for the vertical scrollbar's gutter ONLY when there will be
        # one. There never is: _cap_panel_height below pins this table's
        # height to exactly its rows, so it has nothing to scroll and the bar
        # stays at range (0,0). Reserving it unconditionally left a dead
        # ~16px lane inside the frame to the right of the last column — a
        # gutter for a scrollbar that does not exist. (The note this replaces
        # measured content 880 vs viewport 868 back when the height was not
        # pinned; it now measures 851 vs 867, i.e. over-corrected the other
        # way.) Same shape of bug as the horizontal allowance below.
        # No allowance at all. Consulting the live scrollbar here is
        # circular: this runs BEFORE _cap_panel_height pins the height, so
        # the bar's range still reflects the previous (shorter) table and the
        # gutter comes straight back. The bar cannot appear once the height
        # is pinned to exactly the rows, so there is nothing to reserve. If
        # that pinning is ever removed, the last column will clip and this is
        # the line to restore.
        # PIN, don't just cap — third time this lesson has come up in this
        # file. Sharing a row with the usage strip, a maximum alone let the
        # layout squeeze the table to its size hint (256px of a needed 855)
        # and hand the remainder to the strip.
        table.setMinimumWidth(w)
        table.setMaximumWidth(w)
        # Bottom edge hugs the last reliever too. Capping the PANEL as well
        # stops the splitter from growing this section past its data — the
        # surplus height goes to the SP form section instead. Keep room for
        # the h-scrollbar (the pen table is usually wider than the pane).
        self._cap_panel_height()
        self._place_hdr_box()
        # same order the table was just built in, so row i lines up with
        # reliever i beside it
        self._usage.set_rows(rows)

    def refresh_layout(self):
        """Re-measure after the panel has actually been laid out.

        Everything in `_do_cap` is measured off a LAID-OUT table — row
        heights and `viewport().width()` above all — but the startup overlay
        renders this panel while the whole UI is hidden, so those come back
        against a layout Qt never resolved and the height gets pinned near
        `_MIN_PANEL_H`. `PenUsageStrip.paintEvent` then drops every row whose
        bottom falls past its own (too short) height, so relievers lost their
        week of work. Clicking to another game only appeared to fix it: that
        re-rendered while visible.
        """
        if not self._table.rowCount():
            return
        self._cap_panel_height()
        self._usage.update()

    def _cap_panel_height(self):
        """Fixed panel height = data height: the splitter can neither grow
        this section past its rows nor squeeze relievers behind a scrollbar —
        surplus goes to the SP form section.

        Called from BOTH _render and _render_edge: the matchup strip is part
        of the height budget but becomes visible on its own schedule (the
        second pen of the game arrives later), and leaving it out squeezed
        the table by the strip's own height, hiding the last reliever."""
        table = self._table
        if not table.rowCount() or getattr(self, "_capping", False):
            return
        # setMaximumHeight can itself move the scrollbar range, which is
        # wired back to this method — one level of re-entry is enough
        self._capping = True
        try:
            self._do_cap(table)
        finally:
            self._capping = False

    # Enough for the header strip plus ~4 relievers. Below this the panel is
    # not worth showing, and above it the maximum governs anyway.
    _MIN_PANEL_H = 120

    def _do_cap(self, table):
        h = 2 * table.frameWidth() + table.horizontalHeader().height()
        h += sum(table.rowHeight(r) for r in range(table.rowCount()))
        # Room for the horizontal scrollbar ONLY when there will be one.
        # This was unconditional, and the pen table usually fits (content 869
        # vs an 881px viewport at 1900px wide), so it was reserving 14px of
        # dead strip under the last reliever on every render — and because
        # this panel is height-capped, that 14px came straight out of the SP
        # form section above it.
        # Compare COLUMNS to VIEWPORT. Comparing the widget-width figure
        # (frame + padding + columns) against the viewport is off by the
        # frame allowance and falsely trips by a couple of pixels.
        cols_w = sum(table.columnWidth(c)
                     for c in range(table.columnCount()))
        if cols_w > table.viewport().width():
            h += table.horizontalScrollBar().sizeHint().height()
        # Pin, don't cap. Once the table shares a row with the usage strip
        # the layout will happily give it LESS than its rows (it came out
        # 192 against a needed 208), which raises a vertical scrollbar, which
        # narrows the viewport, which raises a horizontal one — the two
        # allowances then chase each other. Fixing the height breaks the loop.
        table.setMinimumHeight(h)
        table.setMaximumHeight(h)
        panel_h = h + 4                # + root layout margins
        # the header row is as tall as whichever of its two widgets is
        # showing — the label hides when a pen's strength is unknown, but
        # the availability bar beside it can still have arms to draw
        panel_h += max(
            self._edge_label.height() if self._edge_label.isVisible() else 0,
            self._avail_bar.height() if self._avail_bar.isVisible() else 0)
        # MAXIMUM pinned, MINIMUM not. The table above stays pinned — that
        # is what breaks the scrollbar chase described there — but pinning
        # the PANEL's minimum too made this widget unshrinkable, and the SP
        # form panel above it is unshrinkable as well (its own minimum is the
        # sum of its left column). Two immovable objects in one column: when
        # the window was a few pixels shorter than their combined minimum,
        # Qt squeezed below the minimum and the SP panel's children
        # physically OVERLAPPED — the arsenal table's last row drawn under
        # the tunnel table's header.
        #
        # With a floor instead, the pen is the one that yields: it clips a
        # reliever off the bottom, which is visible and obvious, rather than
        # corrupting a neighbouring panel's layout. It still gets its full
        # height whenever there is room, because the maximum is unchanged.
        self.setMaximumHeight(panel_h)
        self.setMinimumHeight(min(panel_h, self._MIN_PANEL_H))


class FieldDefenseView(QWidget):
    """Where he gets hit, drawn on the field, with the gloves standing on it.

    This replaces a linear six-wedge strip plus a separate line of glove
    names. Those were two halves of one question — "where does the contact go
    and who is covering it" — and joining them was left to the reader.

    What is on it:
      * each 15-degree spray sector shaded by his contact share, INNER ring
        for ground balls and OUTER for air, which is the (gb|air) x sector
        structure the strip had to flatten away;
      * the fielders at their positions, hue by OAA and radius by magnitude;
      * a short vector off each glove for his DIRECTIONAL range — to3b vs
        to1b for infielders, in vs back for outfielders. Those splits are
        fetched and then averaged away everywhere else in the app, and they
        are the difference between range into the 5-6 hole and range up the
        middle;
      * the catcher on the plate, tinted by framing.

    On load it sweeps his actual recent balls in play out to their real
    landing spots and then stops — the motion shows where the shading came
    from. It does not loop; a permanent animation in a 150px box is noise."""

    # (spray angle degrees, radius as a fraction of the fan) — where each
    # fielder actually stands, not where the scorecard number suggests
    _POS = {
        "3B": (-37.0, 0.44), "SS": (-19.0, 0.53),
        "2B": (19.0, 0.53), "1B": (37.0, 0.44),
        "LF": (-29.0, 0.86), "CF": (0.0, 0.94), "RF": (29.0, 0.86),
    }
    _SECTORS = ("LL", "L3", "LM", "RM", "R1", "RR")
    _INFIELD_R = 0.60          # ground balls live inside this
    _SWEEP_MS = 1100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(58)
        self._sect = {}        # sector -> (gb_share, air_share, defvalue)
        self._gloves = {}
        self._catcher = None
        self._balls = []       # (angle, r, is_hit)
        self._phase = 1.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._t0 = 0.0

    # ---------------------------------------------------------------- data

    def set_data(self, prof, adv, gloves, catcher=None, balls=None):
        self._sect, self._gloves, self._catcher = {}, gloves or {}, catcher
        self._balls = list(balls or [])
        if prof and adv:
            shares, zones = prof["shares"], adv["zones"]
            for sec in self._SECTORS:
                gb = sum(v for k, v in shares.items()
                         if k[1] == sec and k[0] == "gb")
                air = sum(v for k, v in shares.items()
                          if k[1] == sec and k[0] == "air")
                vals = [zones[k] for k in zones if k[1] == sec]
                self._sect[sec] = (gb, air,
                                   sum(vals) / len(vals) if vals else None)
        if self._balls:
            self._phase = 0.0
            self._t0 = time.time()
            self._timer.start(33)
        else:
            self._phase = 1.0
        self.update()

    def _tick(self):
        self._phase = min(1.0, (time.time() - self._t0)
                          / (self._SWEEP_MS / 1000.0))
        if self._phase >= 1.0:
            self._timer.stop()
        self.update()

    def hideEvent(self, e):
        self._timer.stop()          # never animate off-screen
        super().hideEvent(e)

    # -------------------------------------------------------------- paint

    def _geom(self):
        w, h = self.width(), self.height()
        hx, hy = w / 2.0, h - 3.0
        # the fan has to fit both the 45-degree foul lines and the height
        r = min((w / 2.0) / math.sin(math.radians(45.0)), h - 6.0)
        return hx, hy, r

    def _pt(self, ang, rf, hx, hy, r):
        a = math.radians(ang)
        return (hx + rf * r * math.sin(a), hy - rf * r * math.cos(a))

    def paintEvent(self, _e):
        if not self._sect:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hx, hy, r = self._geom()
        full = QRectF(hx - r, hy - r, 2 * r, 2 * r)
        inner = QRectF(hx - r * self._INFIELD_R, hy - r * self._INFIELD_R,
                       2 * r * self._INFIELD_R, 2 * r * self._INFIELD_R)
        p.setPen(Qt.PenStyle.NoPen)
        mx = max([max(g, a) for g, a, _v in self._sect.values()] or [1]) or 1.0
        for i, sec in enumerate(self._SECTORS):
            gb, air, val = self._sect[sec]
            a0 = -45.0 + i * 15.0
            # Qt measures from 3 o'clock counter-clockwise; the fan points up
            start = int((90.0 - (a0 + 15.0)) * 16)
            span = int(15.0 * 16)
            for rect, share in ((full, air), (inner, gb)):
                if share <= 0:
                    continue
                if val is None:
                    c = QColor(80, 88, 100)
                else:
                    t = max(-1.0, min(1.0, val / 3.0))
                    c = (QColor(46, 204, 113) if t >= 0
                         else QColor(231, 76, 60))
                c.setAlpha(int(28 + 150 * (share / mx)))
                p.setBrush(c)
                p.drawPie(rect, start, span)
        # foul lines + the arc, so the shading reads as a field
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QColor(96, 106, 120))
        for a in (-45.0, 45.0):
            x, y = self._pt(a, 1.0, hx, hy, r)
            p.drawLine(QPointF(hx, hy), QPointF(x, y))
        p.drawArc(full, int((90 - 45) * 16), int(90 * 16))

        # his actual batted balls, sweeping out then left as faint dots
        if self._balls:
            for ang, rf, is_hit in self._balls:
                rr = rf * self._phase
                x, y = self._pt(ang, rr, hx, hy, r)
                live = self._phase < 1.0
                c = (QColor(236, 200, 120) if is_hit
                     else QColor(120, 150, 180))
                c.setAlpha(200 if live else 70)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                d = 2.4 if live else 1.6
                p.drawEllipse(QPointF(x, y), d, d)

        # the gloves
        f = p.font()
        f.setPointSize(6)
        p.setFont(f)
        for pos, (ang, rf) in self._POS.items():
            d = self._gloves.get(pos)
            if not d:
                continue
            x, y = self._pt(ang, rf, hx, hy, r)
            oaa = d.get("oaa")
            if oaa is None:
                # in the game but on no fielding board — drawn as a hollow
                # ring so the position reads as UNKNOWN rather than empty
                p.setPen(QColor(150, 158, 168))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(x, y), 3.2, 3.2)
                continue
            t = max(-1.0, min(1.0, oaa / 10.0))
            c = (QColor(46, 204, 113) if t >= 0 else QColor(231, 76, 60))
            c.setAlpha(int(120 + 135 * abs(t)))
            rad = 2.6 + min(3.4, abs(oaa) / 4.0)
            if d.get("est"):
                # estimated off the defensive spectrum, not measured — drawn
                # as an outline so it never reads as a Statcast number
                p.setPen(QColor(c.red(), c.green(), c.blue(), 230))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(x, y), rad, rad)
                continue
            p.setPen(QColor(18, 22, 28))
            p.setBrush(c)
            p.drawEllipse(QPointF(x, y), rad, rad)
            # directional range: which way this glove is actually good
            lean = self._lean(pos, d)
            if lean is not None:
                lx, ly = lean
                p.setPen(QColor(c.red(), c.green(), c.blue(), 210))
                p.drawLine(QPointF(x, y),
                           QPointF(x + lx * 9.0, y + ly * 9.0))
        # catcher on the plate, tinted by framing
        if self._catcher:
            fr = self._catcher.get("framing_runs")
            c = (QColor(130, 138, 148) if fr is None else
                 QColor(46, 204, 113) if fr > 0 else QColor(231, 76, 60))
            p.setPen(QColor(18, 22, 28))
            p.setBrush(c)
            p.drawEllipse(QPointF(hx, hy - 1.5), 2.4, 2.4)
        p.end()

    def _lean(self, pos, d):
        """Unit vector toward the direction this fielder is strongest.

        Infielders lean laterally (to3b vs to1b); outfielders in vs back.
        Returns None when the split is flat or missing."""
        if pos in ("LF", "CF", "RF"):
            a, b = d.get("in"), d.get("back")
            if a is None or b is None or abs(a - b) < 1.0:
                return None
            return (0.0, 1.0 if a > b else -1.0)
        a, b = d.get("to3b"), d.get("to1b")
        if a is None or b is None or abs(a - b) < 1.0:
            return None
        return (-1.0 if a > b else 1.0, 0.0)


class OpponentCard(QWidget):
    """The nine hitters he actually has to get out tonight.

    Deliberately a plain rich-text label rather than a table: it lives in the
    tables row on a stretch, so its width is whatever is left over — 0 with
    the lineup rail open, ~180px once the rail is collapsed. A QTableWidget
    would fight for a minimum width and squeeze the two tables beside it; a
    label just renders narrower.

    This is the panel's complement to the rail: the rail shows both clubs'
    full cards, this shows only the side he faces, ordered as posted."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 0, 0, 0)
        root.setSpacing(0)
        self._lbl = QLabel("")
        self._lbl.setTextFormat(Qt.TextFormat.RichText)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignTop
                               | Qt.AlignmentFlag.AlignLeft)
        self._lbl.setStyleSheet("font-size: 7pt;")
        root.addWidget(self._lbl)
        root.addStretch(1)
        self.setMinimumWidth(0)

    def clear(self):
        self._lbl.setText("")

    # Below this a rate stat is a sample, not a hitter — 359 wRC+ appeared on
    # the card off a handful of trips and read as the best bat in the league.
    _MIN_PA = 60

    @staticmethod
    def _short(name: str) -> str:
        """`E.Hernández`. Surname alone collides: the Dodgers ran Enrique and
        Teoscar Hernández back to back in the same lineup, both rendering as
        an identical bare `Hernández`."""
        parts = (name or "").split()
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0][:10]
        return f"{parts[0][0]}.{parts[-1][:9]}"

    def set_lineup(self, team: str, rows: List[tuple], posted: bool):
        """rows = [(order, name, wrc, pa)] already in batting order."""
        if not rows:
            self._lbl.setText("")
            return
        out = [f"<span style='color:#dc9437;font-weight:bold'>vs {team}</span>"
               f"<span style='color:#7F8C8D'>{'' if posted else ' *'}</span>"]
        for order, name, wrc, pa in rows:
            thin = pa is not None and pa < self._MIN_PA
            if wrc is None:
                col, txt = "#7F8C8D", "—"
            elif thin:
                # shown, but greyed and parenthesised so it cannot be read as
                # an established rate
                col, txt = "#6b7480", f"({wrc:.0f})"
            else:
                col = ("#2ECC71" if wrc >= 120 else "#E74C3C" if wrc <= 85
                       else "#BDC3C7")
                txt = f"{wrc:.0f}"
            out.append(
                f"<span style='color:#5b6674'>{order}</span> "
                f"<span style='color:#95A5A6'>{self._short(name)}</span> "
                f"<span style='color:{col}'>{txt}</span>")
        self._lbl.setText("<br>".join(out))
        self._lbl.setToolTip(
            f"Tonight's {team} lineup in batting order, with wRC+.\n"
            f"(parenthesised + greyed = under {self._MIN_PA} PA, too few to "
            f"read as a rate)\n"
            + ("" if posted else "* Not posted yet — projected from recent "
                                 "starts.\n"))


class ParkCard(QWidget):
    """Tonight's park, as a set of bars rather than one crushed line.

    The defence card had `park HR +31% 2B -7% 3B -33%` on a single 232px row,
    which is legible but gives no sense of scale — +31% and +8% look alike in
    text. Bars against a fixed +-50% axis make the outlier obvious.

    THIS CARD IS WIDTH-GATED ON PURPOSE. It rides the leftover width in the
    tables row, and measured on a 1900px window that leftover is 0 with the
    lineup rail open (starts 348 + TTO 372 fills the 964px panel exactly) and
    ~178px once the rail is collapsed. Rather than fight the two tables for
    space they need, it renders nothing below 46px and appears when the rail
    is pulled in — the park is context you want when you have room for it,
    not something worth crushing the start log to show. The defence card
    keeps its one-line summary either way, so nothing is ever unavailable."""

    # keys as parkfactors.py returns them; values are 100-based indices
    _EVENTS = (("HR", "HR"), ("R", "R"), ("H", "H"), ("2B", "2B"),
               ("3B", "3B"), ("BB", "BB"), ("SO", "SO"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._venue = ""
        self._pf = None
        self.setMinimumWidth(0)
        # Ignored horizontally so it never fights the two content-capped
        # tables beside it — it renders narrower instead, and bails out
        # entirely under 46px. Vertically it must state a height: a
        # Preferred policy with no sizeHint collapses to nothing.
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Fixed)
        self.setFixedHeight(16 + 15 * len(self._EVENTS))

    def clear(self):
        self._pf = None
        self.update()

    def set_park(self, venue: str, pf: Optional[dict]):
        self._venue, self._pf = venue or "", pf
        self.setToolTip(
            f"{self._venue}\n\nPark factors — how much more (or less) of "
            "each event this park yields than a neutral one, 100 = neutral. "
            "Bars are scaled to +-50%.\n\nThese drive the HR/2B/3B line on "
            "the defence card and the expected-damage side of the field "
            "diagram." if pf else "")
        self.update()

    def paintEvent(self, _e):
        w = self.width()
        if w < 46:
            return                # too narrow to say anything honestly
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(); f.setPointSize(6)
        fb = QFont(); fb.setPointSize(6); fb.setBold(True)
        p.setFont(fb)
        p.setPen(QColor("#dc9437"))
        p.drawText(QRectF(2, 0, w - 4, 12), Qt.AlignmentFlag.AlignLeft, "PARK")
        p.setFont(f)
        p.setPen(QColor("#7F8C8D"))
        p.drawText(QRectF(30, 0, w - 32, 12), Qt.AlignmentFlag.AlignLeft,
                   (self._venue or "")[:24])
        if not self._pf:
            p.setPen(QColor("#4a5158"))
            p.drawText(QRectF(2, 16, w - 4, 12),
                       Qt.AlignmentFlag.AlignLeft, "no factors")
            p.end()
            return
        lab_w, y, rowh = 20.0, 16.0, 15.0
        bar_x = lab_w + 4
        bar_w = max(10.0, w - bar_x - 30)
        mid = bar_x + bar_w / 2.0
        for lbl, key in self._EVENTS:
            v = self._pf.get(key)
            if v is None:
                continue
            d = (float(v) - 100.0) / 100.0          # -1 .. +1 ish
            p.setFont(f)
            p.setPen(QColor("#95A5A6"))
            p.drawText(QRectF(2, y, lab_w, rowh - 2),
                       Qt.AlignmentFlag.AlignLeft, lbl)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#22303c"))
            p.drawRect(QRectF(bar_x, y + 3, bar_w, 7))
            frac = max(-1.0, min(1.0, d / 0.5))     # axis is +-50%
            run = abs(frac) * (bar_w / 2.0)
            p.setBrush(QColor("#E67E22" if d > 0 else "#3498DB"))
            if d >= 0:
                p.drawRect(QRectF(mid, y + 3, run, 7))
            else:
                p.drawRect(QRectF(mid - run, y + 3, run, 7))
            p.setPen(QColor("#4a5158"))
            p.drawLine(int(mid), int(y + 2), int(mid), int(y + 11))
            p.setPen(QColor("#ECF0F1"))
            p.drawText(QRectF(bar_x + bar_w + 2, y, 28, rowh - 2),
                       Qt.AlignmentFlag.AlignRight, f"{d*100:+.0f}%")
            y += rowh
        p.end()


class OpposingLineupStrip(QWidget):
    """The nine he faces, one column each, across the foot of the panel.

    This is the `OpponentCard` turned 90 degrees. That card had to live on a
    stretch in the tables row, which gave it 89px of width with the rail open
    — enough for a name and a number and nothing else. The panel's one large
    contiguous hole is the band under the tables (measured: ~830x78 with the
    rail open, ~920x78 collapsed), and nine hitters laid across it get ~92px
    each instead of 89px total.

    The extra room is spent on the two things the card could not show: how
    each man DECIDES (swing/take run value by zone) and where he puts the
    ball when he does swing (pull/straight/oppo, split ground vs air). Both
    are read against tonight's pitcher — the spray bar is what the field
    diagram above is shaded by, and the chase number is the other side of
    his Chase% percentile."""

    _MIN_PA = 60          # below this a rate is a sample, not a hitter
    # Back to 78, the floor at which every row still fits.
    #
    # 94 was taken opportunistically when 16px of slack sat under this strip,
    # to buy a second line per hitter (ground/air split + BBE count). That
    # slack is no longer free: the pitcher column is now sized to its content
    # rather than stretched, so those 16px go straight to the bullpen table
    # underneath — which pins itself to its own row count and was losing its
    # last reliever to the bottom of a 1080px window. A reliever you cannot
    # see costs more than a second line of spray detail.
    _H = 78
    _H_MIN = 78          # every row still fits at this height

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._team = ""
        self._posted = True
        self._hand = None                 # pitcher's throwing hand
        # The extra 16px is OPPORTUNISTIC, not mandatory. The panel's height
        # is whatever the bullpen leaves it, and the bullpen is pinned to its
        # own row count — a nine-arm pen is ~37px taller than an eight-arm
        # one. Taking the height as a hard minimum would have pushed the left
        # column past the panel on those nights and clipped the UmpireCard.
        # Preferred + a 78 floor means the layout hands it back under
        # pressure, and every row still fits at 78.
        self.setMinimumHeight(self._H_MIN)
        self.setMaximumHeight(self._H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)

    def sizeHint(self):
        s = super().sizeHint()
        s.setHeight(self._H)
        return s

    def clear(self):
        self._rows = []
        self.update()

    def set_lineup(self, team, rows, posted, hand=None):
        """rows = [{order, name, wrc, pa, bats, take, ball}] in batting order."""
        self._team, self._rows, self._posted = team, list(rows), posted
        self._hand = hand
        self.setToolTip(
            "The nine he faces tonight, in posted order.\n\n"
            "wRC+ in parentheses is under 60 PA — a sample, not a rate.\n"
            "B/L/R is the side he bats; gold means he has the platoon "
            "advantage over tonight's starter.\n\n"
            "ch = swing/take run value in the CHASE zone: how much a hitter "
            "has GAINED by laying off balls out of the zone. A big positive "
            "is a hitter you cannot expand against. hrt is the same in the "
            "HEART of the plate — how much he punishes what he gets.\n\n"
            "The bar is his batted-ball spray: pull / straight / oppo, left "
            "to right. The brighter upper band is the AIR share of each — "
            "pulled air is where home runs live, so a wide bright left "
            "segment is the profile that hurts in this park.")
        self.update()

    @staticmethod
    def _short(name):
        parts = (name or "").split()
        if not parts:
            return ""
        return parts[0][:10] if len(parts) == 1 else f"{parts[0][0]}.{parts[-1][:9]}"

    def paintEvent(self, _e):
        if not self._rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self._rows)
        # reserve a right gutter for the "vs LAD" tag. Drawn over the last
        # column it landed on top of that hitter's pull-air figure.
        GUT = 26.0
        W = max(1.0, self.width() - GUT)
        cw = W / float(n)
        f_name = QFont(); f_name.setPointSize(7); f_name.setBold(True)
        f_small = QFont(); f_small.setPointSize(6)

        for i, r in enumerate(self._rows):
            x0 = i * cw
            if i % 2:
                p.fillRect(QRectF(x0, 0, cw, self._H), QColor("#141a21"))
            pad = 3.0
            w = cw - 2 * pad

            # --- name row -------------------------------------------------
            p.setFont(f_name)
            p.setPen(QColor("#7F8C8D"))
            p.drawText(QRectF(x0 + pad, 1, 10, 12),
                       Qt.AlignmentFlag.AlignLeft, str(r.get("order", "")))
            p.setPen(QColor("#ECF0F1"))
            p.drawText(QRectF(x0 + pad + 10, 1, w - 22, 12),
                       Qt.AlignmentFlag.AlignLeft, self._short(r.get("name")))
            bats = (r.get("bats") or "")[:1]
            if bats:
                # gold when the hitter holds the platoon edge tonight
                edge = bool(self._hand) and (
                    bats == "S" or (bats == "L" and self._hand == "R")
                    or (bats == "R" and self._hand == "L"))
                p.setPen(QColor("#dc9437" if edge else "#7F8C8D"))
                p.drawText(QRectF(x0 + cw - pad - 10, 1, 10, 12),
                           Qt.AlignmentFlag.AlignRight, bats)

            # --- wRC+ and discipline -------------------------------------
            p.setFont(f_small)
            wrc, pa = r.get("wrc"), r.get("pa")
            thin = pa is not None and pa < self._MIN_PA
            if wrc is None:
                p.setPen(QColor("#7F8C8D")); wtxt = "—"
            else:
                p.setPen(QColor("#6b7480") if thin else
                         QColor("#2ECC71") if wrc >= 115 else
                         QColor("#E74C3C") if wrc <= 85 else QColor("#BDC3C7"))
                wtxt = f"({wrc:.0f})" if thin else f"{wrc:.0f}"
            p.drawText(QRectF(x0 + pad, 14, w * 0.45, 11),
                       Qt.AlignmentFlag.AlignLeft, wtxt)
            take = r.get("take") or {}
            ch = take.get("chase")
            if ch is not None:
                p.setPen(QColor("#2ECC71") if ch > 6 else
                         QColor("#E74C3C") if ch < -2 else QColor("#95A5A6"))
                p.drawText(QRectF(x0 + pad + w * 0.42, 14, w * 0.58 - 1, 11),
                           Qt.AlignmentFlag.AlignRight, f"ch{ch:+.0f}")
            hrt = take.get("heart")
            if hrt is not None:
                p.setPen(QColor("#2ECC71") if hrt > 6 else
                         QColor("#E74C3C") if hrt < -6 else QColor("#95A5A6"))
                p.drawText(QRectF(x0 + pad, 26, w, 11),
                           Qt.AlignmentFlag.AlignRight, f"hrt{hrt:+.0f}")

            # --- spray bar ------------------------------------------------
            ball = r.get("ball") or {}
            pull, strt, oppo = (ball.get("pull"), ball.get("straight"),
                                ball.get("oppo"))
            by, bh = 42.0, 13.0
            if None not in (pull, strt, oppo) and (pull + strt + oppo) > 0:
                tot = pull + strt + oppo
                air = ball.get("air") or 0.0
                segs = ((pull / tot, "#E67E22"), (strt / tot, "#7F8C8D"),
                        (oppo / tot, "#3498DB"))
                xs = x0 + pad
                for frac, col in segs:
                    sw = frac * w
                    c = QColor(col)
                    # ground portion sits darker beneath the air portion, so
                    # one bar carries both axes instead of costing two rows
                    c.setAlpha(90)
                    p.fillRect(QRectF(xs, by, sw, bh), c)
                    c.setAlpha(235)
                    p.fillRect(QRectF(xs, by, sw, bh * max(0.0, min(1.0, air))),
                               c)
                    xs += sw
                p.setPen(QColor("#2C3E50"))
                p.drawRect(QRectF(x0 + pad, by, w, bh))
                pa_ = ball.get("pull_air")
                if pa_ is not None:
                    p.setFont(f_small)
                    p.setPen(QColor("#E67E22") if pa_ > 0.19 else
                             QColor("#7F8C8D"))
                    p.drawText(QRectF(x0 + pad, by + bh + 1, w, 11),
                               Qt.AlignmentFlag.AlignLeft,
                               f"pull air {pa_*100:.0f}%")
                # ground/air split + the sample the bar above is drawn from
                gb_, bbe = ball.get("gb"), ball.get("bbe")
                if gb_ is not None:
                    p.setPen(QColor("#58D68D") if gb_ > 0.48 else
                             QColor("#5499C7") if gb_ < 0.36 else
                             QColor("#7F8C8D"))
                    p.drawText(QRectF(x0 + pad, by + bh + 12, w * 0.55, 11),
                               Qt.AlignmentFlag.AlignLeft,
                               f"gb {gb_*100:.0f}%")
                if bbe:
                    # thin samples greyed harder — the spray bar cannot say
                    # this for itself
                    p.setPen(QColor("#4a5158") if bbe < 80
                             else QColor("#6b7480"))
                    p.drawText(QRectF(x0 + pad, by + bh + 12, w, 11),
                               Qt.AlignmentFlag.AlignRight, f"n{bbe:.0f}")
            else:
                p.setPen(QColor("#4a5158"))
                p.drawText(QRectF(x0 + pad, by, w, bh),
                           Qt.AlignmentFlag.AlignLeft, "no BBE")
            if i:
                p.setPen(QColor("#22303c"))
                p.drawLine(int(x0), 2, int(x0), self._H - 2)
        # tag lives in its own gutter, stacked, so it owns its pixels
        p.setFont(f_small)
        p.setPen(QColor("#7F8C8D"))
        p.drawText(QRectF(W + 2, 14, GUT - 4, 11),
                   Qt.AlignmentFlag.AlignLeft, "vs")
        p.setPen(QColor("#dc9437"))
        p.drawText(QRectF(W + 2, 25, GUT - 4, 11),
                   Qt.AlignmentFlag.AlignLeft,
                   f"{self._team}{'' if self._posted else '*'}")
        p.end()


class DefenseParkCard(QWidget):
    """Who is behind him and where he is pitching, in one block.

    The field diagram carries the contact/glove picture; the column beside it
    lists the same gloves as numbers, because a coloured dot tells you the
    shape and a number tells you the amount, and both are wanted."""

    _FIELD_H = 66

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 1, 0, 0)
        root.setSpacing(1)
        self._hdr = QLabel("")
        self._hdr.setTextFormat(Qt.TextFormat.RichText)
        self._hdr.setStyleSheet("font-size: 8pt;")
        root.addWidget(self._hdr)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        self.field = FieldDefenseView()
        self.field.setFixedHeight(self._FIELD_H)
        row.addWidget(self.field, stretch=1)
        # the numbers, stacked — "vertical, on the right" of the diagram
        self._gloves = QLabel("")
        self._gloves.setTextFormat(Qt.TextFormat.RichText)
        self._gloves.setAlignment(Qt.AlignmentFlag.AlignTop
                                  | Qt.AlignmentFlag.AlignLeft)
        self._gloves.setStyleSheet("font-size: 6pt;")
        # 72, not 64: the column gaps added below need the room, and at 64
        # the surnames clipped to "Dura" / "Abre". The 8px comes out of the
        # field diagram beside it, which is the cheaper loss — it is a
        # schematic, and a truncated NAME is worse than a slightly smaller fan.
        self._gloves.setFixedWidth(72)
        row.addWidget(self._gloves)
        root.addLayout(row)

        self._park = QLabel("")
        self._park.setTextFormat(Qt.TextFormat.RichText)
        self._park.setStyleSheet("font-size: 7pt;")
        root.addWidget(self._park)
        self._catcher = QLabel("")
        self._catcher.setTextFormat(Qt.TextFormat.RichText)
        self._catcher.setStyleSheet("font-size: 7pt;")
        root.addWidget(self._catcher)
        # The running game is a BATTERY property, not a catcher one: a
        # catcher's caught-stealing line is hostage to how well the man in
        # front of him holds runners, so the two belong on adjacent rows.
        self._running = QLabel("")
        self._running.setTextFormat(Qt.TextFormat.RichText)
        self._running.setStyleSheet("font-size: 7pt;")
        root.addWidget(self._running)
        root.addStretch(0)

    def clear(self):
        self._hdr.setText("")
        self._park.setText("")
        self._gloves.setText("")
        self._catcher.setText("")
        self._running.setText("")
        self.field.set_data(None, None, None)

    def _set_running(self, hold: Optional[dict], runners: Optional[list]):
        """The running game as a matchup: how he holds, against who is on.

        `rate_sbx` is attempts per OPPORTUNITY — the only version of an
        attempt rate that is not mostly a measure of how often his club put
        men on base. The secondary lead is the mechanism behind it: a runner
        who gets to walk 16 feet off the bag has taken the base before the
        catcher is involved at all."""
        if not hold:
            self._running.setText("")
            return
        bits = ["<span style='color:#dc9437'>HOLD</span> "]
        rate = hold.get("rate_sbx")
        if rate is not None:
            # league sits near 2.2% of opportunities; wide of that either way
            # is the signal, so colour against it rather than against zero
            c = ("#E74C3C" if rate > 0.028 else "#2ECC71" if rate < 0.016
                 else "#95A5A6")
            bits.append(f"<span style='color:#7F8C8D'>sbx </span>"
                        f"<span style='color:{c}'>{rate*100:.1f}%</span> ")
        lead = hold.get("lead_sec")
        if lead is not None:
            c = ("#E74C3C" if lead > 15.8 else "#2ECC71" if lead < 14.4
                 else "#95A5A6")
            bits.append(f"<span style='color:#7F8C8D'>lead </span>"
                        f"<span style='color:{c}'>{lead:.1f}'</span> ")
        sb, cs, pk = hold.get("sb") or 0, hold.get("cs") or 0, hold.get("pk") or 0
        bits.append(f"<span style='color:#7F8C8D'>{sb}-{cs}"
                    + (f" {pk}pk" if pk else "") + "</span>")
        # the men who will actually be standing there tonight. A lineup with
        # nobody on it is a RESULT — the 2026 Dodgers' best thief is worth
        # +0.3 runs — so say "quiet" rather than render an empty tail and
        # leave it looking like the join failed.
        if runners:
            top = sorted((r for r in runners if (r.get("runs") or 0) > 0.5),
                         key=lambda r: -(r.get("runs") or 0))[:2]
            if top:
                names = " ".join(
                    f"<span style='color:#ECF0F1'>{r['name'].split()[-1]}</span>"
                    f"<span style='color:#E67E22'>+{r['runs']:.1f}</span>"
                    for r in top)
                bits.append(f"<span style='color:#7F8C8D'> vs </span>{names}")
            else:
                bits.append("<span style='color:#7F8C8D'> vs </span>"
                            "<span style='color:#2ECC71'>quiet</span>")
        self._running.setText("".join(bits))
        self._running.setToolTip(
            "The running game for tonight's BATTERY.\n\n"
            "sbx  = stolen-base attempts per opportunity against this "
            "pitcher (league ~2.2%). A raw attempt count mostly measures how "
            "often his club had men on base.\n"
            "lead = the runners' average SECONDARY lead off first, in feet. "
            "This is the mechanism: a long secondary lead concedes the base "
            "before the catcher is ever involved.\n"
            "n-n  = steals allowed and caught, then pickoffs.\n"
            "vs   = the biggest basestealing threats in tonight's opposing "
            "lineup, in runs added by their steals this season.")

    def set_data(self, team, prof, adv, rank, players, posted,
                 catcher=None, balls=None, hold=None, runners=None):
        if not (prof and adv):
            self.clear()
            return
        self.field.set_data(prof, adv, players, catcher, balls)
        a = adv["adv"]
        col = ("#2ECC71" if a > 0.25 else "#E74C3C" if a < -0.25
               else "#95A5A6")
        rk = (f"<span style='color:{col}'>{_ordinal(rank['rank'])}"
              f"/{rank['of']}</span>" if rank else
              f"<span style='color:{col}'>{a:+.2f}</span>")
        # If a starter carries no fielding number his zones drop out of the
        # weighted average, so say so rather than let "3rd/30" imply the
        # whole alignment was priced.
        priced, gloves = adv.get("priced"), adv.get("gloves")
        est_n = adv.get("estimated") or 0
        gap = ("" if not gloves or priced == gloves
               else f" <span style='color:#E67E22'>{priced}/{gloves}"
                    f"{f' +{est_n}~' if est_n else ''}</span>")
        self._hdr.setText(
            f"<span style='color:#dc9437;font-weight:bold'>DEFENCE</span> "
            f"<span style='color:#BDC3C7'>{team or ''}</span> {rk}"
            f"<span style='color:#7F8C8D'> for his contact"
            f"{'' if posted else ' *'}</span>{gap}")
        pk = adv.get("park") or {}
        if pk:
            def part(lbl, key, warn):
                v = pk.get(key)
                if v is None:
                    return ""
                c = ("#E74C3C" if v > warn else "#2ECC71" if v < -warn
                     else "#95A5A6")
                return (f"<span style='color:#7F8C8D'>{lbl} </span>"
                        f"<span style='color:{c}'>{v:+.0f}%</span> ")
            # NOT the same number as the ParkCard beside the tables, and it
            # must not look like it is: that card shows the raw park index,
            # this is the park weighted to the hands HE faces and scaled by
            # how air-heavy HE is. An air-heavy starter in a homer park reads
            # +31% here against the park's own +26%. Label the difference.
            self._park.setText(
                "<span style='color:#7F8C8D'>park·his </span>"
                + part("HR", "HR_exposed", 4) + part("2B", "2B", 5)
                + part("3B", "3B", 15))
            self._park.setToolTip(
                "Tonight's park weighted to THIS pitcher: the handed splits "
                "for the hitters he faces, with HR scaled by how air-heavy "
                "he is relative to the league (a sinkerballer barely rents "
                "the seats).\n\nThe PARK card in the tables row — visible "
                "with the lineup rail collapsed — shows the park's own raw "
                "factors, so the two HR figures differ by design.")
        else:
            self._park.setText("")
        if catcher:
            def rn(lbl, v, warn=2.0):
                if v is None:
                    return ""
                c = ("#2ECC71" if v > warn else "#E74C3C" if v < -warn
                     else "#95A5A6")
                return (f"<span style='color:#7F8C8D'>{lbl} </span>"
                        f"<span style='color:{c}'>{v:+.0f}</span> ")
            pop = catcher.get("pop_time")
            self._catcher.setText(
                f"<span style='color:#7F8C8D'>C "
                f"{catcher.get('name', '').split()[-1]}</span> "
                + rn("frm", catcher.get("framing_runs"), 3.0)
                + rn("blk", catcher.get("blocking_runs"), 1.0)
                + rn("arm", catcher.get("throw_runs"), 1.0)
                + (f"<span style='color:#7F8C8D'>pop {pop:.2f}</span>"
                   if pop else ""))
        else:
            self._catcher.setText("")
        self._set_running(hold, runners)
        if players:
            lines = []
            for pos in ("LF", "CF", "RF", "3B", "SS", "2B", "1B"):
                d = players.get(pos)
                if not d:
                    continue
                v = d.get("oaa")
                nm = surname(d["name"])[:9]
                if v is None:
                    lines.append(
                        f"<span style='color:#5b6674'>{pos}</span>"
                        f"<span style='color:#6b7480'> —  {nm}</span>")
                    continue
                est = d.get("est")
                c = ("#6b7480" if est else
                     "#2ECC71" if v > 2 else "#E74C3C" if v < -2
                     else "#95A5A6")
                lines.append(
                    f"<span style='color:#5b6674'>{pos}</span>"
                    f"<span style='color:{c}'>&nbsp;{'~' if est else ''}"
                    f"{v:+.0f}</span>"
                    f"<span style='color:#7F8C8D'>&nbsp;{nm}</span>")
            # Spacing here is HORIZONTAL only (the &nbsp; gaps above), on
            # purpose. Seven names stacked at 11px do read as a block, but
            # the left column has ~7px of slack in total — everything in it
            # is pinned to its content and the percentile bars sit at their
            # 15px minimum — and this card is directly above the UmpireCard,
            # so any row margin here comes out of that card on a night when
            # the bullpen is deep. The column gaps cost nothing and are what
            # the 64px-wide list actually needed.
            self._gloves.setText("<br>".join(lines))
            # Arm speed and the jump components sit on hover: the column is
            # 64px and full, but two outfielders at the same OAA can get
            # there in very different ways and that is worth being able to
            # look up. Feet vs league on each of reaction / burst / route.
            det = []
            for pos in ("LF", "CF", "RF", "3B", "SS", "2B", "1B"):
                d = players.get(pos) or {}
                mph, j = d.get("arm_mph"), d.get("jump")
                if mph is None and not j:
                    continue
                bits = [f"{pos} {surname(d.get('name', ''))}"]
                if mph is not None:
                    bits.append(f"arm {mph:.0f} mph")
                if j and any(v is not None for v in j.values()):
                    bits.append("jump " + " ".join(
                        f"{k[:3]} {v:+.1f}ft" for k, v in j.items()
                        if v is not None))
                det.append("  ".join(bits))
            self._gloves.setToolTip(
                "Arm speed and outfield jump, neither of which fits the "
                "column:\n\n" + "\n".join(det) if det else "")
        else:
            self._gloves.setText("")
            self._gloves.setToolTip("")
        self.setToolTip(
            "Shading = share of his contact to that sector (inner ring "
            "ground balls, outer air),\ntinted by how the gloves covering it "
            "compare with a league-average defence.\n"
            "Dots = fielders, size by |OAA|; the stub off each one points the "
            "way his RANGE skews\n(to3b vs to1b for infielders, in vs back "
            "for outfielders).\n"
            "The sweep on load is his actual recent balls in play — gold "
            "hits, blue outs.\n"
            "A hollow ring with a ~ is ESTIMATED, not measured: a starter "
            "with no Statcast fielding row\n(first game at the position, or "
            "short of the playing-time minimum), priced from his FanGraphs "
            "fielding\nruns re-based onto tonight's position via the "
            "defensive spectrum.\n"
            + ("" if posted else
               "* Lineup not posted — season primary fielders."))


class _SpreadGauge(QWidget):
    """One value placed on the league's own spread.

    Deliberately NOT a drawing of the strike zone. The whole league fits in
    a 2.4-point band of called-strike rate, which is under two percent of
    linear zone size — a literal zone glyph would be pixel-identical for the
    tightest and widest umpire in baseball and would imply a precision that
    is not there. A position on the observed range is honest at this size."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(9)
        self._v = self._lo = self._hi = None
        self._good_high = True

    def set_value(self, v, lo, hi, good_high=True):
        self._v, self._lo, self._hi = v, lo, hi
        self._good_high = good_high
        self.update()

    def paintEvent(self, _e):
        if self._v is None or self._hi is None or self._hi <= self._lo:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        mid = h / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2C3E50"))
        p.drawRoundedRect(QRectF(0, mid - 1.5, w, 3), 1.5, 1.5)
        # league centre tick
        p.setBrush(QColor("#7F8C8D"))
        cx = w * (0.0 - self._lo) / (self._hi - self._lo) \
            if self._lo < 0 < self._hi else w / 2.0
        p.drawRect(QRectF(cx - 0.5, mid - 3, 1, 6))
        f = max(0.0, min(1.0, (self._v - self._lo) / (self._hi - self._lo)))
        x = f * w
        hot = (f > 0.5) == self._good_high
        p.setBrush(QColor("#2ECC71" if hot else "#E74C3C"))
        p.drawEllipse(QPointF(max(3.0, min(w - 3.0, x)), mid), 3.2, 3.2)
        p.end()


class UmpireCard(QWidget):
    """Tonight's home-plate umpire, and how his zone has actually behaved.

    Three numbers in increasing order of how directly they measure the man:
    the called-strike rate on TAKEN pitches (his zone), the walk rate behind
    it (the effect), and the share of ABS challenges in his games that were
    OVERTURNED — the only one that tests his calls directly, because a
    challenge is only ever spent on a pitch a player believes was missed.

    Every rate is regressed toward the league; an umpire works ~18 plate
    games a season and the raw leaderboard is otherwise sorted by luck."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 0)
        root.setSpacing(1)
        self._hdr = QLabel("")
        self._hdr.setTextFormat(Qt.TextFormat.RichText)
        self._hdr.setStyleSheet("font-size: 8pt;")
        root.addWidget(self._hdr)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(1)
        self._gauges, self._labels = {}, {}
        for r, (key, lbl) in enumerate((("zone", "zone"),
                                        ("calls", "calls"))):
            cap = QLabel(lbl)
            cap.setStyleSheet("color:#7F8C8D; font-size: 6pt;")
            cap.setFixedWidth(24)
            g = _SpreadGauge()
            v = QLabel("")
            v.setTextFormat(Qt.TextFormat.RichText)
            v.setStyleSheet("font-size: 6pt;")
            grid.addWidget(cap, r, 0)
            grid.addWidget(g, r, 1)
            grid.addWidget(v, r, 2)
            grid.setColumnStretch(1, 1)
            self._gauges[key], self._labels[key] = g, v
        root.addLayout(grid)

        self._foot = QLabel("")
        self._foot.setTextFormat(Qt.TextFormat.RichText)
        self._foot.setStyleSheet("font-size: 6pt;")
        root.addWidget(self._foot)
        root.addStretch(0)
        self.clear()

    def clear(self):
        self._hdr.setText(
            "<span style='color:#dc9437;font-weight:bold'>UMPIRE</span>"
            "<span style='color:#7F8C8D'> —</span>")
        self._foot.setText("")
        for k in self._gauges:
            self._gauges[k].set_value(None, None, None)
            self._labels[k].setText("")

    def set_data(self, name: Optional[str], prof: Optional[dict],
                 league: Optional[dict], spread: Optional[dict] = None):
        if not name:
            self.clear()
            return
        if not prof or not league:
            # assigned but not yet profiled — say which, don't blank out
            self._hdr.setText(
                "<span style='color:#dc9437;font-weight:bold'>UMPIRE</span> "
                f"<span style='color:#ECF0F1'>{name}</span>"
                "<span style='color:#7F8C8D'> · no profile yet</span>")
            self._foot.setText("")
            for k in self._gauges:
                self._gauges[k].set_value(None, None, None)
                self._labels[k].setText("")
            return
        self._hdr.setText(
            "<span style='color:#dc9437;font-weight:bold'>UMPIRE</span> "
            f"<span style='color:#ECF0F1'>{name}</span>"
            f"<span style='color:#7F8C8D'> {prof['games']}G</span>")

        sp = spread or {}
        d = prof.get("csr_d") or 0.0
        lo, hi = sp.get("csr_d", (-0.013, 0.013))
        self._gauges["zone"].set_value(d, lo, hi, good_high=True)
        word = "wide" if d > 0.002 else "tight" if d < -0.002 else "neutral"
        col = "#2ECC71" if d > 0.002 else "#E74C3C" if d < -0.002 else "#95A5A6"
        self._labels["zone"].setText(
            f"<span style='color:{col}'>{d*100:+.1f}%</span> "
            f"<span style='color:#7F8C8D'>{word}</span>")

        ov = prof.get("ovr_against")
        lgov = league.get("ovr_against")
        if ov is not None and lgov is not None:
            olo, ohi = sp.get("ovr_against", (0.40, 0.68))
            # a HIGH overturn rate means he was wrong more often -> red
            self._gauges["calls"].set_value(ov, olo, ohi, good_high=False)
            self._labels["calls"].setText(
                f"<span style='color:#ECF0F1'>{ov*100:.0f}%</span>"
                f"<span style='color:#7F8C8D'> ovr, lg {lgov*100:.0f}%</span>")
        k_d, bb_d = prof.get("k_d") or 0.0, prof.get("bb_d") or 0.0
        self._foot.setText(
            f"<span style='color:#7F8C8D'>K</span> "
            f"<span style='color:#ECF0F1'>{prof['k_pct']*100:.1f}%</span> "
            f"<span style='color:{'#2ECC71' if k_d>0 else '#E74C3C'}'>"
            f"{k_d*100:+.1f}</span>"
            f"<span style='color:#7F8C8D'>  BB</span> "
            f"<span style='color:#ECF0F1'>{prof['bb_pct']*100:.1f}%</span> "
            f"<span style='color:{'#E74C3C' if bb_d>0 else '#2ECC71'}'>"
            f"{bb_d*100:+.1f}</span>"
            f"<span style='color:#7F8C8D'>  {prof['chal']} chal</span>")
        self.setToolTip(
            f"{name} — {prof['games']} games behind the plate.\n\n"
            "zone  = called-strike rate on TAKEN pitches vs league "
            f"({league['csr']*100:.1f}%). Swings are excluded — they measure "
            "the hitter, not him.\n"
            "calls = share of ABS challenges in his games that were "
            f"OVERTURNED (league {lgov*100:.1f}% over "
            f"{league.get('games', 0)} games). The only figure here that "
            "tests his calls directly.\n\n"
            "All rates are regressed toward the league mean — an umpire "
            "works ~18 plate games a season, so the raw leaderboard is "
            "sorted by sample size rather than by skill.")


class PitcherFormPanel(QWidget):
    """Starter recent-form section that sits under the bullpen table: the
    game-by-game log (with per-start FB velo + CSW% joined from the Savant
    pitch-detail cache), a leash strip (IP/NP per start, outs distributions,
    days rest), and times-through-order damage. Shows the OPPOSING SP for
    batter props and the player himself for pitcher props — same sync logic
    as the bullpen panel. Data via MLBPropStats.get_sp_form (StatsAPI game
    log, instant) then get_sp_statcast_form (one cached CSV fetch)."""

    # Six fits the height the tables row actually has: the panel left 25px
    # dead under it at four, and a start is the densest row on this panel.
    # Appearances shown collapsed, newest first. 7, not 6: retiring the
    # in-table expander row freed one, and 7 rows makes this table exactly
    # level with the 7-row splits table beside it (both 186px).
    _N_APPS = 7

    def __init__(self, stats: Optional[MLBPropStats] = None, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._pid: Optional[int] = None
        self._hand: Optional[str] = None
        self._form: Optional[dict] = None
        self._sc: Optional[dict] = None
        self._name = ""
        self._context = ""
        self._mv: Optional[dict] = None
        self._card: Optional[dict] = None   # SP deep card (arsenal quick-card)
        self._matchup_batter = None         # current batter for combo viewer
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
        # 7 -> 5: the grouped line adds the E-S pair and two dividers, and the
        # band was already using nearly the full header width. Tightening the
        # gaps pays for them without dropping a stat or wrapping to a 2nd row.
        self._trad_grid.setHorizontalSpacing(5)
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
        sh = self._starts_table.horizontalHeader()
        sh.setSectionsClickable(True)
        sh.sectionClicked.connect(self._on_starts_header_clicked)
        self._tto_table = self._make_table(
            ["Split", "PA", "wOBA", "xwOBA", "K%", "BB%",
             "EV", "HH%", "Brl%", "HR"])
        self._tto_table.setToolTip(
            "Damage splits: times through the order (1/2/3+), platoon "
            "(vs LHB / vs RHB), and windup vs stretch (Empty / Men on).\n\n"
            "Men on is the split the HOLD line on the defence card is about "
            "— a pitcher who loses a tick from the stretch is the one whose "
            "running game costs him twice.")
        # No internal scrollbars: heights are capped to content, and when the
        # splitter squeezes the panel a v-scrollbar would steal column width
        # (clipping CSW behind an h-scrollbar) — older rows clip instead
        for t in (self._starts_table, self._tto_table):
            t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            t.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # the right axis carries its unit as a tick suffix, so it has to be
        # our own AxisItem rather than the one PlotItem would build
        self._plot = pg.PlotWidget(background="#151a21",
                                   axisItems={"right": UnitAxis("right")})
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.setMinimumWidth(180)
        self._plot.setFixedHeight(240)   # own row now — nothing to level with

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
        self._chip_stat_menu = StatMenuButton(self._OVERLAYS,
                                              groups=self._OVERLAY_GROUPS)
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
        # Release geometry gets a scale PER SERIES, not one shared "misc"
        # box: arm angle runs ~26-30 deg and extension ~7.1-7.4 ft, so a
        # single auto-range spanning 7..30 flattened extension into a
        # straight line and hid the only thing it is there to show. Each
        # auto-ranges to itself; the right axis is claimed by priority
        # (mph > rpm > deg > ft) and the loser still draws, exactly as spin
        # already does when velo owns the axis.
        self._vb_arm = pg.ViewBox()
        self._vb_ext = pg.ViewBox()
        for vb in (self._vb_rate, self._vb_velo, self._vb_spin,
                   self._vb_arm, self._vb_ext):
            vb.setMouseEnabled(x=False, y=False)
            vb.setZValue(20)
            p.scene().addItem(vb)
            vb.setXLink(p.getViewBox())
        p.getAxis("left").linkToView(self._vb_rate)
        p.getAxis("right").linkToView(self._vb_velo)
        # A SECOND right axis, outboard of the first, so two unit-ful series
        # can be read at once — FBv in mph beside FF spin in rpm, or arm
        # angle in degrees beside extension in feet. Stops at two: a third
        # would cost more width than the readings are worth, and anything
        # past it still draws, just unlabelled (as spin always has).
        # PlotItem's grid is (row 2, col 0)=left axis, (2,1)=viewbox,
        # (2,2)=right axis — so this goes at (2,3).
        self._axis_r2 = UnitAxis("right")
        f2 = QFont(); f2.setPointSize(6)
        self._axis_r2.setPen(pg.mkPen(90, 100, 115))
        self._axis_r2.setTextPen(pg.mkPen(140, 150, 162))
        self._axis_r2.setStyle(tickFont=f2)
        p.layout.addItem(self._axis_r2, 2, 3)
        self._axis_r2.hide()
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
        # Square, sized to the shapes band it shares with the release plot —
        # an aspect-locked plot given a tall sliver renders the clusters
        # microscopic, so width and height move together
        self._mv_plot.setFixedSize(self._SHAPES_H, self._SHAPES_H)

        # Command band: one small zone square per pitch type, filled per
        # pitcher in _render_shapes (arsenals run 3-6 pitches, so the row is
        # built dynamically rather than declared here). Plots stretch to share
        # the width; aspect-lock keeps the zone square whatever they get.
        self._zone_plots: List[pg.PlotWidget] = []
        self._zone_holder = QWidget()
        self._zone_row = QHBoxLayout(self._zone_holder)
        self._zone_row.setContentsMargins(0, 0, 0, 0)
        self._zone_row.setSpacing(3)

        # Delivery: release points (catcher's view, feet) for every pitch type
        # overlaid on one square — the movement plot shows what the ball does,
        # this shows where it comes from. Tight clusters = repeatable slot;
        # a pitch sitting off on its own is a tipping cue.
        self._rel_plot = shape_plot()
        self._rel_plot.setToolTip(
            "Release point (catcher's view): horizontal vs vertical release "
            "in feet, colored per pitch type. Big dots = per-pitch average — "
            "CLICK a dot to fly the pitch in 3D")
        self._rel_plot.setFixedSize(self._SHAPES_H, self._SHAPES_H)

        # Row 1: form plot, FULL width (the per-start trend is the one thing
        #        here that reads better the more x-axis it gets).
        # Row 2: zone squares (stretch) | movement square | release square.
        # Row 3: starts log | damage splits.
        # Plots band together on top, tables band together beneath — the two
        # graphical rows share a visual language (per-pitch color, clickable
        # mean dots) that the tables don't, so they read as one block.
        # Everything hugs the top; the vertical stretch keeps the splitter's
        # spare height empty below the content instead of inside it.
        # Arsenal quick-card: per-pitch Stuff+ / velo / spin fills the gap
        # between the percentile stack and the tables row
        self._ars_table = self._make_table(
            ["Pitch", "Stf+", "Velo", "Spin", "vLg", "Uq"])
        self._ars_table.setToolTip(
            "Arsenal: FanGraphs Stuff+ with Savant avg velo/spin per pitch. "
            "Pitch cell shows usage%; vLg = spin vs league avg for that pitch."
            "\n\nUq = uniqueness: how far this pitch's shape and approach "
            "angle sit from what its arm slot, velocity, extension and "
            "release point predict (RMS z-score vs the league).\n"
            "Highlighted only on SL/CH/FS/FC/SI — across 2024-26 it predicts "
            "whiff rate on sliders (r=+.23) and changeups (r=+.16) but NOT "
            "on four-seamers, sweepers or curves, so it is shown dim there.")
        self._ars_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._ars_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Tunnel card: pairwise separation at the hitter's commit point vs at
        # the plate. Lives under the arsenal card because it's the same "what
        # is this arsenal" question, and that column had the free height.
        # Rel/dV/dBrk fill the ~87px this table was leaving unused beside it, and
        # they are the levers the ratio cannot distinguish: same slot (Rel),
        # speed gap (dV), and how far apart they finish in movement (dBrk).
        self._tunnel_table = self._make_table(
            ["Tunnel", "Tun", "Plt", "Rto", "Rel", "\u0394V", "\u0394Brk"])
        self._tunnel_table.setToolTip(
            "Pitch tunneling, top 4 pitches by usage. Tun = how far apart the "
            f"pair still is at the commit point ({TUNNEL_COMMIT_Y_FT}ft out, "
            "where the hitter must decide) — SMALLER is better. Plt = how far "
            "apart they finish at the plate — BIGGER is better. Rto = Plt/Tun, "
            "the break-to-tunnel read. Both distances in inches.")
        self._tunnel_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tunnel_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Where he gets hit, shaded by who is standing there. Sits under the
        # arsenal/tunnel stack, which is the column that already answers
        # "what does he throw" — this answers "and where does it go".
        self._opp_card = OpponentCard()     # retained: PlayerDetail reuses it
        self._lineup_strip = OpposingLineupStrip()
        self._park_card = ParkCard()
        self._def_card = DefenseParkCard()
        # The other half of the environment: who is calling the zone. Sits
        # directly under the defence/park block because it answers the same
        # kind of question — what is around him tonight, not what he throws.
        # The left column ended at 735 of 864, so this lands in real slack
        # rather than taking space from anything above.
        self._ump_card = UmpireCard()

        pct_col = QVBoxLayout()
        # 1, not 4. This column BINDS the whole panel's minimum height (734
        # against the right column's 702), and the panel's minimum plus the
        # bullpen's — both are PINNED, neither can yield — came to 1013px
        # against roughly 1005 available in a fullscreen 1080 window. Qt then
        # squeezes below the minimum and the children physically OVERLAP: the
        # arsenal table's last row was drawn under the tunnel table's header.
        # Four gaps at 4px is 12px of pure air; at 1px it is 3.
        pct_col.setSpacing(1)
        pct_col.addWidget(pct_holder)
        pct_col.addWidget(self._ars_table)
        pct_col.addWidget(self._tunnel_table)
        pct_col.addWidget(self._def_card)
        pct_col.addWidget(self._ump_card)
        pct_col.addStretch(1)

        plots_row = QHBoxLayout()
        plots_row.setSpacing(4)
        plots_row.addWidget(self._plot, stretch=1,
                            alignment=Qt.AlignmentFlag.AlignTop)

        tables_row = QHBoxLayout()
        tables_row.setSpacing(4)
        # Both tables are width-capped to their content, so a trailing
        # stretch parked every spare pixel in a dead pocket — most visibly on
        # rail-collapse, where the plots above grew 723->901 while these two
        # stayed 347 and 372 and the freed 178px landed in nothing.
        tables_row.addWidget(self._starts_table,
                             alignment=Qt.AlignmentFlag.AlignTop)
        tables_row.addWidget(self._tto_table,
                             alignment=Qt.AlignmentFlag.AlignTop)
        # The nine hitters moved OUT of this row and down into the strip —
        # 89px of stretch could hold a name and a number and nothing else.
        # What takes the leftover width instead is tonight's park, which was
        # previously one crushed line on the defence card.
        tables_row.addWidget(self._park_card, stretch=1,
                             alignment=Qt.AlignmentFlag.AlignTop)

        # Shape band: the three per-pitch views together — where it finishes
        # (zone), what it does (movement), where it comes from (release).
        # AlignTop keeps the two fixed squares level with the zone row: an
        # unaligned height-capped widget gets vertically centered instead.
        shapes_row = QHBoxLayout()
        shapes_row.setSpacing(4)
        shapes_row.addWidget(self._zone_holder, stretch=1,
                             alignment=Qt.AlignmentFlag.AlignTop)
        shapes_row.addWidget(self._mv_plot,
                             alignment=Qt.AlignmentFlag.AlignTop)
        shapes_row.addWidget(self._rel_plot,
                             alignment=Qt.AlignmentFlag.AlignTop)

        # Tables ride in the same column as the plots, so they tuck directly
        # under the plot band beside the (taller) pct/arsenal column instead
        # of starting a new full-width row below it
        right_col = QVBoxLayout()
        right_col.setSpacing(4)
        right_col.addLayout(plots_row)
        right_col.addLayout(shapes_row)
        right_col.addLayout(tables_row)
        # The panel's one large contiguous hole, measured at the height the
        # app actually runs at: ~830x78 with the rail open, ~920x78 with it
        # collapsed. Nine hitters across it get ~92px each.
        right_col.addWidget(self._lineup_strip)
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
        # The VERTICAL header's minimum section size is font-derived and
        # lands at 21 here, which silently clamps `_fit(row_h=...)` — a
        # requested 20 came back as 21 and two thirds of the trim went
        # missing without any error. Lower the floor so the cap decides.
        t.verticalHeader().setMinimumSectionSize(14)
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
                     context: str = "", venue: str = "", opp: str = ""):
        """Point the panel at a pitcher (no-op when already showing him)."""
        if self._stats is None or not pid:
            return
        self._venue = venue
        self._opp = opp
        self._name = name + (f" ({hand})" if hand else "")
        # kept raw for the uniqueness mirror (arm-side break is sign-flipped
        # between hands); falls back to the FG board's Throws when absent
        self._hand = hand
        self._context = context
        self._update_info()
        if pid == self._pid:
            return
        self._pid = pid
        self._render_pct()
        self._form = self._sc = self._mv = self._card = None
        self._ars_table.setRowCount(0)
        self._tunnel_table.setRowCount(0)
        self._plot_vline.hide()
        self._plot_htext.hide()
        self._headshot.setPixmap(QPixmap())
        self._trad_pairs = []
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
        # The defence/park read moved to DefenseParkCard: on this line it was
        # the newest content and the least visible thing on the panel, tacked
        # onto the end of a run-on string that already carried context+leash.
        self._info_label.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(parts))
        self._info_label.setToolTip("")

    # One band only: the whole season line (traditional + advanced) rides on a
    # single label-over-value row across the header's free width.
    _TRAD_PAIRS_PER_ROW = 999

    # Season line grouped by what each stat is FOR, left to right, with the
    # label tinted per group. Order is deliberate — the 2024-26 study found
    # the projectable stats (Stuff+ persists at .79, K% .72, SIERA .63) beat
    # the results stats at predicting next year, while ERA self-persists at
    # only .23. Regression flags come last and do NOT persist by design:
    # that is what makes them flags (LOB% -> next-year ERA change r=+.40).
    # Groups are separated by a hairline, NOT a second row — the band stays
    # exactly as tall as it was.
    _TRAD_GROUPS = [
        ("#5DADE2", ["Stf+", "Loc+", "Pit+", "SIERA", "xFIP", "K-BB%",
                     "K/9", "BB/9", "xwOBA", "xBA", "xSLG"]),
        ("#7F8C8D", ["W-L", "ERA", "FIP", "WHIP", "IP", "G", "GS", "SO",
                     "HR/9", "AVGa"]),
        ("#E59866", ["E-S", "LOB%", "BABIP", "Luck"]),
    ]
    _TRAD_TIPS = {
        "E-S": ("ERA minus SIERA. The single strongest regression signal "
                "tested (2024-26): r=-0.55 vs next-season ERA CHANGE.\n"
                "POSITIVE = ERA above the skill line, expect it to fall.\n"
                "NEGATIVE = ERA is running under the skill, expect it to rise."),
        "LOB%": ("Strand rate — barely persists year to year (r=.13), which "
                 "is why it flags regression: high LOB% -> next-year ERA "
                 "RISES (r=+.40)."),
        "BABIP": "Batting average on balls in play vs league (▲ above/▼ below).",
        "Luck": "Actual wOBA minus xwOBA; negative = underperforming contact.",
        "Stf+": "FanGraphs Stuff+ — the most persistent metric tested (r=.79).",
    }

    def _render_trad(self, pairs):
        """Season line mini-grid (label over value), like the batter header —
        kept to a single row so the advanced stats fill the empty header
        width rather than stacking a second band."""
        while self._trad_grid.count():
            item = self._trad_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        have = dict(pairs)
        colors = getattr(self, "_trad_value_colors", {}) or {}
        col = 0
        for gi, (tint, labels) in enumerate(self._TRAD_GROUPS):
            present = [l for l in labels if l in have]
            if not present:
                continue
            if col:      # hairline divider, spans both rows -> adds no height
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFixedWidth(1)
                sep.setStyleSheet("color: #34495E; background: #34495E;")
                self._trad_grid.addWidget(sep, 0, col, 2, 1)
                col += 1
            for label in present:
                lbl = QLabel(label)
                lbl.setObjectName("spTradLabel")
                lbl.setStyleSheet(f"color: {tint}; font-size: 6pt;")
                val = QLabel(have[label])
                val.setObjectName("spTradValue")
                if label in colors:
                    val.setStyleSheet(f"color: {colors[label]}; font-size: 8pt;"
                                      " font-weight: bold;")
                tip = self._TRAD_TIPS.get(label)
                if tip:
                    lbl.setToolTip(tip)
                    val.setToolTip(tip)
                for w in (lbl, val):
                    w.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                self._trad_grid.addWidget(lbl, 0, col)
                self._trad_grid.addWidget(val, 1, col)
                col += 1
        # anything the groups don't name still gets shown, after a divider
        rest = [(l, v) for l, v in pairs
                if not any(l in g[1] for g in self._TRAD_GROUPS)]
        for label, value in rest:
            lbl = QLabel(label)
            lbl.setObjectName("spTradLabel")
            val = QLabel(value)
            val.setObjectName("spTradValue")
            for w in (lbl, val):
                w.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._trad_grid.addWidget(lbl, 0, col)
            self._trad_grid.addWidget(val, 1, col)
            col += 1

    async def _fetch(self, pid: int):
        # Stage 1: game log (fast) renders immediately; stage 2 fills the
        # velo/CSW columns and the TTO table from the pitch-detail cache
        try:
            async with self._stats.http() as session:
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
                        self._trad_pairs = list(pairs)
                        self._render_trad(self._trad_pairs)
                except Exception as e:
                    print(f"PitcherFormPanel: season line failed: {e}")
                sc = await self._stats.get_sp_statcast_form(session, pid)
                mv = await self._stats.get_sp_movement(session, pid)
                card = await self._stats.get_sp_deep_card(session, pid)
                await self._load_defense_advantage(session, pid)
        except Exception as e:
            print(f"PitcherFormPanel: fetch failed for {pid}: {e}")
            return
        if self._pid == pid:
            # LOB% comes from the game log — fold it into the statcast dict so
            # the overlay/tooltip read every per-start series the same way
            apps = (form or {}).get("apps") or []
            sc["lob"] = {a["date"]: a["lob"] for a in apps
                         if a.get("lob") is not None}
            self._sc = sc
            self._mv = mv
            self._card = card
            # Fold the FG-board advanced line (SIERA/FIP/xFIP + Pitching+
            # family) into the season line now that the arsenal card resolved
            # it — these used to live only in the batter's opposing-SP readout.
            self._append_fg_trad(card)
            self._render()
            self._render_shapes()
            self._render_arsenal()
            self._render_tunnels()

    async def _load_defense_advantage(self, session, pid: int):
        """How much the gloves behind him are worth versus a league-average
        defence, given the contact he generates — plus this park's effect on
        it. Everything here is already cached (his pitch detail, one OAA
        board, one fielding-run-value page), so it costs almost nothing."""
        self._def_adv = self._def_prof = None
        try:
            rec = next((r for r in (self._stats._roster or {}).values()
                        if r.get("id") == pid), None)
            team = self._stats._teams.get((rec or {}).get("team_id"))
            if not team:
                return
            rows = await self._stats._get_pitch_detail(session, pid, "pitcher")
            prof = sp_contact_profile(rows)
            if not prof:
                return
            # a sample of his ACTUAL balls in play for the load sweep — the
            # shading is an aggregate of exactly these, so showing them
            # arriving is the provenance rather than an effect
            balls = []
            for r_ in rows:
                if not r_.get("bb_type"):
                    continue
                ang = _spray_angle(r_.get("hc_x"), r_.get("hc_y"))
                if ang is None:
                    continue
                dist = r_.get("hit_distance")
                rf = (min(1.0, dist / 400.0) if dist
                      else (0.45 if r_["bb_type"] == "ground_ball" else 0.8))
                balls.append((ang, rf,
                              r_.get("event") in ("single", "double",
                                                  "triple", "home_run")))
            if len(balls) > 70:
                step = len(balls) / 70.0
                balls = [balls[int(k * step)] for k in range(70)]
            # tonight's posted card if there is one, season primaries if not
            posted = False
            lineup_ids = None
            posted_fielders = None
            try:
                maps = await self._stats.get_lineup_maps(session)
                m = maps.get(team) or {}
                if m.get("posted") and m.get("slots"):
                    lineup_ids = {int(k) for k in m["slots"]}
                    posted_fielders = m.get("fielders") or {}
                    posted = True
            except Exception:
                pass
            dfn = await self._stats.get_team_defense(
                session, team, lineup_ids, posted_fielders)
            if posted and len(dfn) < 4:
                # posted card but too few of them are on the fielding board —
                # fall back rather than price a two-man defence
                dfn = await self._stats.get_team_defense(session, team)
                posted = False
            lg_def = await self._stats.league_zone_defense(session)
            park = None
            venue = getattr(self, "_venue", "")
            if venue:
                park = await self._stats.get_park_factors(venue)
            all_def = await self._stats.all_team_defense(session)
            try:
                catcher = await self._stats.get_team_catcher(
                    session, team, lineup_ids)
            except Exception:
                catcher = None
            try:
                await self._load_opponent(session, pid)
            except Exception as e:
                print(f"PitcherFormPanel: opponent card failed: {e}")
            # the running game: his hold, against the men who will be on base
            hold, runners = None, None
            try:
                hold = await self._stats.get_pitcher_running_game(session, pid)
                opp = getattr(self, "_opp", "")
                if opp:
                    steal = await self._stats.get_basestealing(session)
                    omaps = await self._stats.get_lineup_maps(session)
                    om = omaps.get(opp) or {}
                    ids = ([int(k) for k in (om.get("slots") or {})]
                           if om.get("posted") else [])
                    by_id = {r["id"]: r for r in
                             (self._stats._roster or {}).values()}
                    runners = []
                    for bid in ids:
                        s = steal.get(bid)
                        if not s:
                            continue
                        nm = (by_id.get(bid) or {}).get("name") or ""
                        if nm:
                            runners.append({"name": nm, "runs": s.get("runs"),
                                            "sb": s.get("sb")})
            except Exception as e:
                print(f"PitcherFormPanel: running game failed for {pid}: {e}")
            if self._pid == pid:
                self._def_prof = prof
                self._def_team = team
                self._def_players = dfn
                self._def_posted = posted
                self._def_adv = defense_advantage(prof, dfn, lg_def, park)
                self._def_rank = defense_rank(prof, all_def, lg_def, team)
                if getattr(self, "_def_card", None):
                    self._def_card.set_data(team, prof, self._def_adv,
                                            self._def_rank, dfn, posted,
                                            catcher, balls, hold, runners)
                if getattr(self, "_park_card", None):
                    # `park` is {'All'|'R'|'L': row}; the card shows the
                    # unsplit view — the handed rows already drive the
                    # defence advantage above
                    self._park_card.set_park(
                        venue, (park or {}).get("All")
                        or next(iter((park or {}).values()), None))
                self._update_info()
        except Exception as e:
            print(f"PitcherFormPanel: defence advantage failed for {pid}: {e}")

    async def _load_opponent(self, session, pid: int):
        """The nine he faces tonight, in posted batting order, with wRC+."""
        opp = getattr(self, "_opp", "")
        if not opp:
            return
        maps = await self._stats.get_lineup_maps(session)
        m = maps.get(opp) or {}
        slots = m.get("slots") or {}
        posted = bool(m.get("posted") and slots)
        if not posted:
            self._opp_card.clear()
            self._lineup_strip.clear()
            return
        by_id = {r["id"]: r for r in (self._stats._roster or {}).values()}
        # the two hitter boards the strip is built on — whole-board fetches,
        # cached, so this is one request each per session rather than per man
        try:
            take_board = await self._stats.get_swing_take(session)
            ball_board = await self._stats.get_batted_ball(session)
        except Exception as e:
            print(f"PitcherFormPanel: hitter boards failed: {e}")
            take_board, ball_board = {}, {}
        strip_rows = []
        rows = []
        for bid, order in sorted(slots.items(), key=lambda kv: kv[1]):
            rec = by_id.get(int(bid)) or {}
            wrc, pa, fgname = None, None, None
            try:
                fg = await self._stats.get_fg_batting(int(bid))
                if fg:
                    fgname = fg.get("name")
                    if fg.get("wrcplus") is not None:
                        wrc = float(fg["wrcplus"])
                    if fg.get("pa") is not None:
                        pa = float(fg["pa"])
            except (TypeError, ValueError, KeyError):
                pass
            # roster is the better name source, but it misses call-ups the FG
            # board already has — an unnamed row reading just "7  131" is
            # worse than either
            nm = rec.get("name") or fgname or ""
            rows.append((order, nm, wrc, pa))
            strip_rows.append({
                "order": order, "name": nm, "wrc": wrc, "pa": pa,
                "bats": rec.get("bats") or rec.get("bat_side"),
                "take": take_board.get(int(bid)) or {},
                "ball": ball_board.get(int(bid)) or {},
            })
        if self._pid == pid:
            self._opp_card.set_lineup(opp, rows, posted)
            self._lineup_strip.set_lineup(opp, strip_rows, posted,
                                          getattr(self, "_hand", None))

    def _append_fg_trad(self, card):
        """Extend the (StatsAPI) season line with FanGraphs advanced stats
        pulled from the arsenal card, then repaint the mini-grid."""
        fg = (card or {}).get("fg") or {}
        if not fg or not getattr(self, "_trad_pairs", None):
            return
        f2 = lambda v: f"{v:.2f}" if isinstance(v, (int, float)) else None
        f0 = lambda v: f"{v:.0f}" if isinstance(v, (int, float)) else None
        extra = [
            ("SIERA", f2(fg.get("siera"))),
            ("FIP", f2(fg.get("fip"))),
            ("xFIP", f2(fg.get("xfip"))),
            ("Stf+", f0(fg.get("stuff"))),
            ("Loc+", f0(fg.get("location"))),
            ("Pit+", f0(fg.get("pitching"))),
        ]
        # ERA minus SIERA. The ERA is taken from the DISPLAYED pair, not from
        # fg["era"]: the FG board lags StatsAPI by a start or two (Warren read
        # 4.41/102.0 IP on FG vs 4.14/108.2 IP live), so sourcing it from FG
        # printed a gap that contradicted the ERA sitting two columns to its
        # left. SIERA only exists on the FG side, so some mixing is
        # unavoidable — better that the number reconciles with its neighbours.
        siera = fg.get("siera")
        era = None
        for lbl, v in self._trad_pairs:
            if lbl == "ERA":
                try:
                    era = float(v)
                except (TypeError, ValueError):
                    era = None
                break
        self._trad_value_colors = {}
        if isinstance(era, (int, float)) and isinstance(siera, (int, float)):
            gap = era - siera
            extra.append(("E-S", f"{gap:+.2f}"))
            if gap >= 0.30:
                self._trad_value_colors["E-S"] = "#2ECC71"
            elif gap <= -0.30:
                self._trad_value_colors["E-S"] = "#E74C3C"
        have = {lbl for lbl, _ in self._trad_pairs}
        merged = list(self._trad_pairs) + [(lbl, v) for lbl, v in extra
                                           if v is not None and lbl not in have]
        self._render_trad(merged)

    # -------------------------------------------------------------- render

    _GREEN = QColor(46, 204, 113)
    _RED = QColor(231, 76, 60)
    _ORANGE = QColor(230, 126, 34)

    # (key, chip label, color) — "k" draws the per-start bars on the main
    # viewbox; velo plots on the right axis in mph; the rest share the left
    # axis (rates + xwOBA live in the same 0-0.6 band)
    # Ordered skill -> contact-quality -> luck. Z-Con%/Chase%/Zone% are the
    # plate-discipline additions: Z-Contact% carries forward better year over
    # year (r=.71) than anything else the panel shows, and the luck block is
    # deliberately kept — LOB%/BABIP don't persist, which is exactly why they
    # flag a pitcher whose ERA is about to move (LOB% -> next-year ERA CHANGE
    # r=+.40).
    _OVERLAYS = [("k", "K", "#2ECC71"),
                 # K-BB% is the per-start skill line raw K count cannot be:
                 # a 7-K start over 4 innings and one over 7 both read "7".
                 # Gold: every other rate line is already blue/green/violet
                 # or red, and a second green sat indistinguishably next to
                 # Z-Con% in the legend.
                 ("kbb", "K-BB%", "#F4D03F"),
                 ("velo", "FBv", "#E67E22"), ("csw", "CSW%", "#82C4E0"),
                 ("whiff", "Whiff%", "#AF7AC5"),
                 # lime, not orange: FBv (#E67E22) is on by default and the
                 # two were indistinguishable in the legend
                 ("zcon", "Z-Con%", "#48C9B0"), ("chase", "Chase%", "#A9DC76"),
                 ("zone", "Zone%", "#7FB3D5"),
                 # neutral grey: strike-throwing is the plainest thing on
                 # here, and every coloured slot near it was taken
                 ("fstr", "F-Strike%", "#D5DBDB"),
                 ("xw", "xwOBA", "#E74C3C"),
                 # actual wOBA under xwOBA: the GAP is the story, and xwOBA
                 # alone cannot show it. Same red family ON PURPOSE — they
                 # are the same quantity — and separated by a DASH rather
                 # than a second hue, which is what says "these two belong
                 # together" instead of "these are unrelated series".
                 ("woba", "wOBA", "#F1948A"),
                 ("ev", "EV", "#F1C40F"), ("hh", "HH%", "#1ABC9C"),
                 ("brl", "Brl%", "#FF6FB5"), ("lob", "LOB%", "#E59866"),
                 ("gb", "GB%", "#58D68D"), ("fb", "FlyBall%", "#5499C7"),
                 # release geometry — own axis, own scale (deg / ft)
                 ("arm", "ArmAng", "#D7BDE2"), ("ext", "Ext", "#F5B7B1")]

    # Menu grouping: 19 overlays plus the per-pitch spin entries is past the
    # point where a flat list is scannable. Labels are inserted as disabled
    # actions before the first key of each group.
    _OVERLAY_GROUPS = [("k", "MISSING BATS"), ("zcon", "DISCIPLINE"),
                       ("xw", "CONTACT / LUCK"), ("arm", "RELEASE")]
    # Drawn dashed: the ACTUAL half of an actual-vs-expected pair, so the two
    # read as one quantity rather than two unrelated lines.
    _DASHED = {"woba"}

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
        self._vb_arm.setGeometry(rect)
        self._vb_ext.setGeometry(rect)

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
               "brl": lambda v: f"Brl: {v:.0%}",
               "lob": lambda v: f"LOB: {v:.0%}",
               "gb": lambda v: f"GB: {v:.0%}",
               "fb": lambda v: f"FB: {v:.0%}",
               "zcon": lambda v: f"Z-Con: {v:.0%}",
               "chase": lambda v: f"Chase: {v:.0%}",
               "zone": lambda v: f"Zone: {v:.0%}",
               # The axes carry only a symbol now, so the SPELLED-OUT unit
               # lives here — this readout is what the axis stopped saying.
               "kbb": lambda v: f"K-BB: {v:.0%}",
               "woba": lambda v: f"wOBA: {v:.3f}".replace("0.", "."),
               "fstr": lambda v: f"F-Strike: {v:.0%}",
               "arm": lambda v: f"Arm angle: {v:.1f}°",
               "ext": lambda v: f"Extension: {v:.2f} ft"}
        # FB velo is averaged over the fastballs thrown that start; show the
        # count so a small-sample blip reads as such
        fbn = (sc.get("fbn") or {}).get(a["date"])
        for key, _label, color in self._OVERLAYS:
            if key == "k" or not self._chip_btns[key].isChecked():
                continue
            if key == "kbb":
                # computed from the game log, not the Statcast join
                v = (None if not a.get("bf") else
                     ((a.get("k") or 0) - (a.get("bb") or 0)) / a["bf"])
            else:
                v = (sc.get(key) or {}).get(a["date"])
            if v is None:
                continue
            f = fmt.get(key)
            if f is None:
                # a new overlay without a formatter must not take the hover
                # readout down with it — this lookup used to be fmt[key]
                continue
            txt = f(v)
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

    # Below this many of a pitch type the uniqueness z-scores are noise — a
    # 20-pitch sample scored 4.3 in testing, which would read as the most
    # distinctive pitch in the league
    _UNIQ_MIN_N = 50

    def _pitch_uniqueness_map(self) -> Dict[str, tuple]:
        """{pitch_code: (uniqueness dict, n)} for this pitcher's arsenal."""
        throws = self._hand or ((self._card or {}).get("fg") or {}).get(
            "throws")
        arm = (self._card or {}).get("arm_angle")
        out = {}
        for pt in ((self._mv or {}).get("pitches") or []):
            if (pt.get("n") or 0) < self._UNIQ_MIN_N:
                continue
            u = pitch_uniqueness(pt, throws, arm)
            if u:
                out[PITCH_ABBREV.get(pt["pitch"], "")] = (u, pt["n"])
        return out

    def _render_arsenal(self):
        """Per-pitch Stuff+ / velo / spin quick-card from the SP deep card."""
        table = self._ars_table
        rows = (self._card or {}).get("rows") or []
        uniq = self._pitch_uniqueness_map()
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
            # Uniqueness: dim on the pitch types where the 3-season study
            # found no relationship to results, so a big number there doesn't
            # read as an edge it isn't
            u_rec = uniq.get(pt)
            if u_rec is None:
                uq_item = cell("")
            else:
                u, un = u_rec
                uq_item = cell(f"{u['uniq']:.1f}")
                if pt in UNIQ_PREDICTIVE:
                    if u["uniq"] >= 1.5:
                        uq_item.setForeground(self._GREEN)
                        f2 = uq_item.font()
                        f2.setBold(True)
                        uq_item.setFont(f2)
                else:
                    uq_item.setForeground(QColor("#5D6D7E"))
                uq_item.setToolTip(
                    f"{row.get('pitch', pt)} — {un} thrown\n"
                    f"iVB {u['z_ivb']:+.1f}σ · arm-side break "
                    f"{u['z_hb_arm']:+.1f}σ · approach angle "
                    f"{u['z_vaa']:+.1f}σ vs slot"
                    + ("" if pt in UNIQ_PREDICTIVE else
                       f"\n(no demonstrated whiff link on {pt})"))
            cells = [name_item,
                     stuff_item,
                     cell("" if velo is None else f"{velo:.1f}"),
                     cell("" if spin is None else f"{spin:.0f}"),
                     dspin_item,
                     uq_item]
            for c, it in enumerate(cells):
                table.setItem(r, c, it)
        self._fit(table, cap_height=True, row_h=self._STACK_ROW_H)
        # spin menu was just rebuilt for this arsenal — re-render so any
        # carried-over spin selections draw
        self._render_plot()

    def _render_tunnels(self):
        """Pairwise tunnel card off the movement kinematics (no extra fetch —
        get_sp_movement already carries the mean Hawk-Eye state per pitch)."""
        table = self._tunnel_table
        table.setRowCount(0)
        pairs = compute_tunnels((self._mv or {}).get("pitches") or [],
                                max_pitches=self._N_ZONES)
        if not pairs:
            self._fit(table, cap_height=True)
            return
        table.setRowCount(len(pairs))
        for r, t in enumerate(pairs):
            ca = PITCH_ABBREV.get(t["a"], t["a"])
            cb = PITCH_ABBREV.get(t["b"], t["b"])
            pair = QTableWidgetItem(f"{ca}-{cb}")
            f = pair.font()
            f.setBold(True)
            pair.setFont(f)
            rel_txt = "—" if t["release"] is None else f"{t['release']:.1f}in"
            pair.setToolTip(f"{t['a']} vs {t['b']}\n"
                            f"Release separation: {rel_txt}\n"
                            f"{t['n']} pitches (the smaller of the two)")

            def cell(text):
                it = QTableWidgetItem(text)
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
                return it

            # Tight at the commit point is the good outcome for Tun; wide at
            # the plate is the good outcome for Plt
            tun_item = cell(f"{t['tunnel']:.1f}")
            if t["tunnel"] <= 4.0:
                tun_item.setForeground(self._GREEN)
            elif t["tunnel"] >= 8.0:
                tun_item.setForeground(self._RED)
            plt_item = cell(f"{t['plate']:.0f}")
            if t["plate"] >= 14.0:
                plt_item.setForeground(self._GREEN)
            elif t["plate"] <= 6.0:
                plt_item.setForeground(self._RED)
            rto_item = cell("—" if t["ratio"] is None else f"{t['ratio']:.1f}")
            if t["ratio"] is not None:
                if t["ratio"] >= 3.0:
                    rto_item.setForeground(self._GREEN)
                elif t["ratio"] <= 1.5:
                    rto_item.setForeground(self._RED)
            rel_item = cell("—" if t["release"] is None
                            else f"{t['release']:.1f}")
            # Released from the same slot is the whole point of a tunnel
            if t["release"] is not None:
                if t["release"] <= 2.0:
                    rel_item.setForeground(self._GREEN)
                elif t["release"] >= 5.0:
                    rel_item.setForeground(self._RED)
            dv_item = cell("—" if t.get("dvelo") is None
                           else f"{t['dvelo']:.0f}")
            if t.get("dvelo") is not None and t["dvelo"] >= 8.0:
                dv_item.setForeground(self._GREEN)
            db_item = cell("—" if t.get("dbreak") is None
                           else f"{t['dbreak']:.0f}")
            if t.get("dbreak") is not None and t["dbreak"] >= 15.0:
                db_item.setForeground(self._GREEN)
            for c, it in enumerate((pair, tun_item, plt_item, rto_item,
                                    rel_item, dv_item, db_item)):
                table.setItem(r, c, it)
        self._fit(table, cap_height=True, row_h=self._STACK_ROW_H)

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
            if key == "kbb":
                # from the game log, not the Statcast join — batters faced is
                # the right denominator and only the log carries it
                out = []
                for a in apps:
                    bf = a.get("bf")
                    out.append(np.nan if not bf else
                               ((a.get("k") or 0) - (a.get("bb") or 0)) / bf)
                return np.array(out, dtype=float)
            vals = [(sc.get(key) or {}).get(a["date"]) for a in apps]
            return np.array([np.nan if v is None else float(v)
                             for v in vals])

        def add_line(vb, key, color):
            y = series(key)
            if not np.isfinite(y).any():
                return False
            pen = pg.mkPen(color, width=2)
            if key in self._DASHED:
                pen.setStyle(Qt.PenStyle.DashLine)
            vb.addItem(pg.PlotCurveItem(
                x=x, y=y, connect="finite", pen=pen))
            fin = y[np.isfinite(y)]
            lo, hi = float(fin.min()), float(fin.max())
            pad = max((hi - lo) * 0.15, 0.01)
            vb._auto_range = (lo - pad, hi + pad)
            return True

        colors = dict((k, c) for k, _, c in self._OVERLAYS)
        rate_keys = [k for k in ("csw", "whiff", "xw", "woba", "hh", "brl",
                                 "lob", "gb", "fb", "zcon", "chase", "zone",
                                 "fstr", "kbb")
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
        # Release geometry — one scale each, so a 0.3ft extension wobble is
        # as visible as a 4-degree slot change
        arm_on = ext_on = False
        for key, vb in (("arm", self._vb_arm), ("ext", self._vb_ext)):
            vb.clear()
            if self._chip_btns[key].isChecked() and add_line(vb, key,
                                                             colors[key]):
                lo_r, hi_r = vb._auto_range
                vb.setYRange(lo_r, hi_r, padding=0)
                if key == "arm":
                    arm_on = True
                else:
                    ext_on = True
        # Two right axes, four candidates, fixed priority. Whichever two are
        # active get a labelled scale; a third still draws unlabelled.
        # Symbols, not words. A rotated "deg"/"ft" label costs the same
        # horizontal strip as the tick numbers themselves, and the crosshair
        # readout already spells every unit out in full — that is where the
        # explanation belongs, not stacked down the side of the plot.
        active = [(vb, unit) for on, vb, unit in
                  ((velo_on, self._vb_velo, "mph"),
                   (spin_on, self._vb_spin, "rpm"),
                   (arm_on, self._vb_arm, "°"),
                   (ext_on, self._vb_ext, "′")) if on]
        def apply_unit(axis, unit):
            """A one-glyph unit rides the ticks (`28°`); a word stays a
            label. Suffixing every tick with "mph" would be noise, and
            rotating a prime into a label makes it look like a tick mark."""
            if unit and len(unit) == 1:
                axis.set_suffix(unit)
                axis.setLabel(None)
            else:
                axis.set_suffix("")
                axis.setLabel(unit or None)

        right_axis = p.getAxis("right")
        apply_unit(right_axis, active[0][1] if active else None)
        if active:
            right_axis.linkToView(active[0][0])
        if len(active) > 1:
            self._axis_r2.linkToView(active[1][0])
            apply_unit(self._axis_r2, active[1][1])
            self._axis_r2.show()
        else:
            apply_unit(self._axis_r2, None)
            self._axis_r2.hide()
        p.showAxis("left", rate_on)
        p.showAxis("right", bool(active))
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
        # Expander lives in the FIRST HEADER CELL, not in a spanning row of
        # its own. A drawer row cost a full row of table height, moved
        # between the top and the bottom depending on which way it pointed,
        # and pushed this table out of line with the splits table beside it.
        # A glyph on the "Date" header is always in the same place and costs
        # no rows at all.
        self._starts_toggle_row = -1        # row-click path retired
        hdr_item = table.horizontalHeaderItem(0)
        if hdr_item is None:
            hdr_item = QTableWidgetItem()
            table.setHorizontalHeaderItem(0, hdr_item)
        if has_toggle:
            hdr_item.setText(("▾ Date" if self._show_all_starts else "▸ Date"))
            hdr_item.setToolTip(
                f"Showing all {len(apps)} appearances — click to collapse."
                if self._show_all_starts else
                f"Showing the last {self._N_APPS} of {len(apps)} — "
                "click to show them all.")
        else:
            hdr_item.setText("Date")
            hdr_item.setToolTip("")
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

    def _on_starts_header_clicked(self, section: int):
        """Column 0's header doubles as the show-all-starts toggle."""
        if section == 0 and (self._form or {}).get("apps"):
            if len(self._form["apps"]) > self._N_APPS:
                self._show_all_starts = not self._show_all_starts
                self._render()

    # -------------------------------------------------- pitch-shape plots

    def _on_mean_clicked(self, _item, points, *_):
        if points:
            self._open_flight_viewer(points[0].data())

    def set_matchup_batter(self, name: str, swing: dict):
        """The batter currently shown in the Player Detail panel — dropped
        into the box when the arsenal flight viewer opens (combo view)."""
        self._matchup_batter = dict(swing or {}, name=name) if swing else None

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
            PITCH_COLORS, PITCH_ABBREV, start_pitch=start_pitch,
            arm_angle=(self._card or {}).get("arm_angle"),
            batter=self._matchup_batter)
        self._flight_win.show()

    # Height of the shapes band — the zone squares, the movement square and
    # the release square all share it, and all three are ASPECT-LOCKED, so
    # this is the one number that sets how big any of them draws.
    #
    # 150, was 186. This band is the only compressible thing in the panel's
    # RIGHT column (the form plot is a time series and the two tables are
    # pinned to their rows), and the 36px it gives back go straight to the
    # bullpen table below the panel — the pen is pinned to its rows and takes
    # whatever the SP panel leaves, so a 10-man pen was losing its last
    # reliever off the bottom of the window. The squares are aspect-locked and
    # width-bound in this row, so at 150 the movement and release plots lose
    # nothing but letterbox, and the zone strip gets the 72px the other two
    # gave up.
    _SHAPES_H = 150
    # Row height for the two stacked left-column tables (arsenal, tunnel).
    # 20, against the 23 `resizeRowsToContents` gives them.
    #
    # Trimming the shapes band alone only helps a 3-4 pitch starter: it
    # shortens the RIGHT column, and for a 5-6 pitch arsenal the LEFT column
    # is what binds the panel (measured: Mahle 4 pitches → left 701 / right
    # 714; Fried 6 pitches → left 747 / right 714). These two tables carry 12
    # rows between them for a full arsenal, so 3px off each row is the same
    # ~36px the shapes band gave back — taken from padding rather than from a
    # dropped pitch or a dropped tunnel pair. Below ~18 the 8pt text starts
    # to crowd the gridlines.
    _STACK_ROW_H = 20
    # Half-plate in feet — the zone squares' horizontal reference
    _ZONE_HALF_W = _ZONE_HALF_W_FT
    # Zone squares shown, most-used first (the strip shares its row with the
    # movement + release squares, so it can't hold a full 6-pitch arsenal)
    _N_ZONES = 4

    def _clear_zones(self):
        while self._zone_row.count():
            item = self._zone_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._zone_plots = []

    def _render_shapes(self):
        mv_p = self._mv_plot.getPlotItem()
        mv_p.clear()
        self._rel_plot.getPlotItem().clear()
        self._clear_zones()
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
        self._render_zones(pitches)
        self._render_release(pitches)

    def _render_zones(self, pitches: List[dict]):
        """Command band: one square per pitch type showing where it finishes,
        against THIS pitcher's average zone (sz_top/sz_bot vary by the hitters
        he faces, so a league-generic box would misread his edges). Axes are
        hidden — the zone rectangle is the scale."""
        zt = (self._mv or {}).get("sz_top", 3.4)
        zb = (self._mv or {}).get("sz_bot", 1.6)
        zx = self._ZONE_HALF_W
        # Aspect-locked in a box that's taller than it is wide, so WIDTH sets
        # the ft-per-pixel scale and the y window comes out bigger than asked.
        # Keep x tight (±1.15ft ≈ a zone and a half) or the box renders too
        # small to read a cluster against; wild pitches clip, which is fine.
        X_LIM, Z_LO, Z_HI = 1.15, 0.9, 3.9

        # Sharing the band with the movement + release squares leaves ~350px
        # for this strip, so a 6-pitch arsenal would get ~55px each — too
        # narrow for the zone box to read. Cap at the top 4 by usage (already
        # usage-sorted upstream); the tail is 2-4% junk that the arsenal table
        # and movement plot still account for.
        for pt in pitches[:self._N_ZONES]:
            code = PITCH_ABBREV.get(pt["pitch"], pt["pitch"])
            color = QColor(PITCH_COLORS.get(code, _PITCH_COLOR_DEFAULT))
            w = pg.PlotWidget(background="#151a21")
            w.setMenuEnabled(False)
            w.hideButtons()
            w.setAspectLocked(True)
            w.setFixedHeight(self._SHAPES_H)
            # Low floor: the band also carries the movement + release squares
            # now, so a 6-pitch arsenal has to fit ~55px each. Anything higher
            # forces the whole middle column wider and blows the splitter
            # budget the pen table depends on.
            w.setMinimumWidth(52)
            # Cap the stretch so a 3-pitch arsenal doesn't blow the squares up
            # into acres of empty letterbox either side of the zone
            w.setMaximumWidth(190)
            p = w.getPlotItem()
            p.getViewBox().setMouseEnabled(x=False, y=False)
            p.hideAxis("left")
            p.hideAxis("bottom")

            loc = pt.get("loc") or []
            in_zone = sum(1 for x, z in loc
                          if abs(x) <= zx and zb <= z <= zt)
            w.setToolTip(
                f"{pt['pitch']} — {pt['n']} thrown"
                + (f", {in_zone / len(loc):.0%} in zone" if loc else "")
                + "\nPlate location, catcher's view. Box = this pitcher's "
                  "average strike zone.")

            # Zone box + thirds: the grid is what makes a cluster readable as
            # "arm-side corner" rather than just "low-ish"
            thirds = pg.mkPen(70, 80, 94, width=1, style=Qt.PenStyle.DotLine)
            for f in (1 / 3, 2 / 3):
                p.addItem(pg.PlotCurveItem(
                    [-zx + 2 * zx * f] * 2, [zb, zt], pen=thirds))
                p.addItem(pg.PlotCurveItem(
                    [-zx, zx], [zb + (zt - zb) * f] * 2, pen=thirds))
            p.addItem(pg.PlotCurveItem(
                [-zx, zx, zx, -zx, -zx], [zb, zb, zt, zt, zb],
                pen=pg.mkPen(125, 138, 155, width=1)))

            if loc:
                c = QColor(color)
                c.setAlpha(60)
                p.addItem(pg.ScatterPlotItem(
                    x=[l[0] for l in loc], y=[l[1] for l in loc],
                    size=3, brush=pg.mkBrush(c), pen=None))
                mx = _avg([l[0] for l in loc])
                mz = _avg([l[1] for l in loc])
                mean = pg.ScatterPlotItem(
                    x=[mx], y=[mz], size=10, brush=pg.mkBrush(color),
                    pen=pg.mkPen(20, 25, 32, width=2), data=[pt["pitch"]])
                mean.sigClicked.connect(self._on_mean_clicked)
                p.addItem(mean)

            vb = p.getViewBox()
            vb.setRange(xRange=(-X_LIM, X_LIM), yRange=(Z_LO, Z_HI),
                        padding=0)
            # Centred header, NOT corner-anchored: these plots get their width
            # from the layout, and aspect-lock only ever widens a range, so a
            # corner pinned at build time drifts off-screen once the row is
            # laid out. x=0 and the requested top stay in view either way.
            head = pg.TextItem(
                f"{code} {pt['n']}", color=color, anchor=(0.5, 0))
            head.setPos(0, Z_HI)
            p.addItem(head)
            self._zone_row.addWidget(w)
            self._zone_plots.append(w)
        self._zone_row.addStretch(1)

    def _render_release(self, pitches: List[dict]):
        """Delivery: every pitch type's release points on one square. Axes stay
        visible here — unlike the zone squares there's no drawn reference box,
        and the absolute numbers (how high, how far to the arm side) are the
        read. Extension rides in the corner since it's the third dimension the
        plot can't show."""
        p = self._rel_plot.getPlotItem()
        # Default-constructed then sized: QFont("", 7) (empty family) segfaults
        # Qt's tick-label painter rather than falling back to the app font
        tick_font = QFont()
        tick_font.setPointSize(7)
        for ax in ("left", "bottom"):
            a = p.getAxis(ax)
            a.setPen(pg.mkPen(90, 100, 115))
            a.setTextPen(pg.mkPen(140, 150, 162))
            a.setStyle(tickFont=tick_font)

        xs_all, zs_all = [], []
        ext_n, ext_sum = 0, 0.0
        for pt in pitches:
            code = PITCH_ABBREV.get(pt["pitch"], pt["pitch"])
            color = QColor(PITCH_COLORS.get(code, _PITCH_COLOR_DEFAULT))
            rel = pt.get("rel") or []
            if rel:
                c = QColor(color)
                c.setAlpha(55)
                xs = [r[0] for r in rel]
                zs = [r[1] for r in rel]
                xs_all += xs
                zs_all += zs
                p.addItem(pg.ScatterPlotItem(
                    x=xs, y=zs, size=3, brush=pg.mkBrush(c), pen=None))
            if pt.get("ext") is not None:
                ext_sum += pt["ext"] * pt["n"]
                ext_n += pt["n"]
            if pt.get("mean_rel_x") is None:
                continue
            mean = pg.ScatterPlotItem(
                x=[pt["mean_rel_x"]], y=[pt["mean_rel_z"]], size=10,
                brush=pg.mkBrush(color), pen=pg.mkPen(20, 25, 32, width=2),
                data=[pt["pitch"]])
            mean.sigClicked.connect(self._on_mean_clicked)
            p.addItem(mean)
            label = pg.TextItem(code, color=color, anchor=(0.5, 1.2))
            label.setPos(pt["mean_rel_x"], pt["mean_rel_z"])
            p.addItem(label)

        if not xs_all:
            return
        # Pad to at least a 1.5ft window so a tight slot doesn't zoom into
        # measurement noise and read as scatter
        def span(vals):
            lo, hi = min(vals), max(vals)
            # Floor of 0.4ft so the mean-dot code labels have room — a pitch
            # whose cluster sits at the edge otherwise loses its label
            pad = max(0.4, (1.5 - (hi - lo)) / 2)
            return lo - pad, hi + pad
        xr, zr = span(xs_all), span(zs_all)
        vb = p.getViewBox()
        vb.setRange(xRange=xr, yRange=zr, padding=0)
        if ext_n:
            ext = pg.TextItem(f"Ext {ext_sum / ext_n:.1f}′",
                              color="#82C4E0", anchor=(0, 0))
            (vx0, _vx1), (_vz0, vz1) = vb.viewRange()
            ext.setPos(vx0, vz1)
            p.addItem(ext)
        self._rel_plot.setToolTip(
            "Release point (catcher's view), feet — colored per pitch type.\n"
            + "\n".join(
                f"{PITCH_ABBREV.get(pt['pitch'], pt['pitch'])}: "
                f"{pt['mean_rel_x']:+.2f} x, {pt['mean_rel_z']:.2f} z"
                + (f", {pt['ext']:.1f}′ ext" if pt.get("ext") else "")
                for pt in pitches if pt.get("mean_rel_x") is not None)
            + "\nCLICK a mean dot to fly the pitch in 3D")

    @staticmethod
    def _fit(table: QTableWidget, cap_height: bool = False,
             row_h: Optional[int] = None):
        table.resizeRowsToContents()
        if row_h is not None:
            # resizeRowsToContents lands on 23px for these tables (8pt text
            # plus the QSS cell padding); `row_h` shaves that back without
            # dropping a row. Only ever a CAP — a row whose content genuinely
            # needs less keeps its own height.
            for r in range(table.rowCount()):
                if table.rowHeight(r) > row_h:
                    table.setRowHeight(r, row_h)
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
            # PIN it, don't just cap it. A maximum alone lets a column that
            # is over-subscribed squeeze the table BELOW its content, and
            # with the scrollbars off that silently eats the last row — the
            # arsenal lost CH and the tunnel lost its 6th pair the moment
            # two more cards joined the left column. Same reasoning as the
            # width pin above; it just was never applied to the height.
            table.setMinimumHeight(h)
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

from PyQt6.QtWidgets import (QMainWindow, QSplitter, QSplitterHandle,
                             QTabWidget, QListWidget, QListWidgetItem,
                             QStyledItemDelegate, QStyle)


QML_DIR = Path(__file__).resolve().parent / "qml"


class QuickSeamLoader(QObject):
    """Startup overlay that lives INSIDE the main window but animates on Qt
    Quick's render thread.

    Qt allows no thread but the GUI thread to touch widgets, so a QPainter
    overlay can only animate in the gaps between panel construction — which
    is what made it hitch. The way out is not to move widget work off-thread
    (impossible) but to render the overlay with something that is not a
    widget: a QQuickView, whose threaded render loop keeps drawing while the
    GUI thread is busy.

    !! It must be `createWindowContainer`, and it must NOT be QQuickWidget !!
    Both put Qt Quick in a widget UI, but `QQuickWidget` renders through an
    FBO on the GUI THREAD (documented, and measured: it stalls exactly like
    QPainter did). `createWindowContainer` embeds the QQuickView as a real
    native child window that keeps its own render thread. Measured with the
    GUI thread hard-blocked for 1.5s: 112 render passes still delivered,
    worst gap 14.3ms.

    A separate top-level overlay window also works but was rejected — it did
    not reliably keep stacking above the main window, so the UI was visible
    populating underneath it.
    """

    finished = pyqtSignal()

    def __init__(self, host: QWidget, steps: List[tuple]):
        super().__init__(host)
        self._host = host
        self._steps = list(steps)
        self._pending = {k for k, _ in self._steps}
        from PyQt6.QtQuick import QQuickView
        v = QQuickView()
        # Transparent clear colour so the OpacityAnimator fade dissolves into
        # the finished UI behind it rather than into an opaque plate.
        v.setColor(QColor(0, 0, 0, 0))
        v.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        v.setSource(QUrl.fromLocalFile(str(QML_DIR / "SeamLoader.qml")))
        if v.status() != QQuickView.Status.Ready:
            for e in v.errors():
                print(f"EffortMLB: QML loader error: {e.toString()}")
            raise RuntimeError("SeamLoader.qml failed to load")
        self._view = v
        self._root = v.rootObject()
        self._root.faded.connect(self._on_faded)
        self._container = QWidget.createWindowContainer(v, host)
        self._container.setGeometry(host.rect())
        self._label(self._steps[0][1] if self._steps else "loading…")
        self._container.show()
        self._container.raise_()

    def _label(self, text: str):
        if self._root is not None:
            self._root.setProperty("label", text)

    def setGeometry(self, rect):
        """Named to match SeamLoader so the window can drive either."""
        if self._container is not None:
            self._container.setGeometry(rect)

    def raise_(self):
        if self._container is not None:
            self._container.raise_()

    def step(self, key: str, label: Optional[str] = None):
        self._pending.discard(key)
        if self._root is not None:
            total = max(1, len(self._steps))
            self._root.setProperty(
                "progress", (total - len(self._pending)) / total)
        nxt = label or next((l for k, l in self._steps if k in self._pending),
                            None)
        if nxt:
            self._label(nxt)

    def finish(self):
        if self._root is None:
            return
        self._pending.clear()
        self._root.setProperty("progress", 1.0)
        self._label("ready")
        self._root.setProperty("fading", True)

    def _on_faded(self):
        c, self._container = self._container, None
        self._view = self._root = None
        if c is not None:
            c.hide()
            c.deleteLater()
        self.finished.emit()


class StartupTracker:
    """Records every asyncio task created while the startup overlay is up.

    Hand-listing what to wait for does not work here: panels spawn their own
    fetches from inside plain Qt calls — `PitcherFormPanel.show_pitcher()`
    and `BullpenPanel.show_team()` both `create_task` internally — so the
    overlay lifted while the SP card and the pen table were still filling,
    which is exactly what it exists to prevent. A task factory catches all of
    it, including tasks that later spawn tasks of their own.

    EXCLUDE holds the deliberately long-lived ones. `_show_umpire` backs off
    for up to 57s by design and `_poll_lineups` runs all afternoon; waiting on
    either would mean never lifting the overlay.
    """

    EXCLUDE = ("_show_umpire", "_poll_lineups", "_init_async")

    def __init__(self, loop):
        self._loop = loop
        self._prev = loop.get_task_factory()
        self._tasks: set = set()
        self._on = False

    def install(self):
        def factory(loop, coro, **kw):
            if self._prev is not None:
                t = self._prev(loop, coro, **kw)
            else:
                t = asyncio.Task(coro, loop=loop, **kw)
            if self._on:
                name = getattr(coro, "__qualname__", "") or str(coro)
                if not any(x in name for x in self.EXCLUDE):
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
            return t
        self._on = True
        self._loop.set_task_factory(factory)

    def uninstall(self):
        self._on = False
        try:
            self._loop.set_task_factory(self._prev)
        except Exception:
            pass

    async def drain(self, quiet_rounds: int = 3, gap: float = 0.05):
        """Block until nothing tracked is outstanding, and stays that way.

        The re-check matters: a task finishing is often what spawns the next
        one (a fetch completing then populating a panel), so a single empty
        observation is not proof that startup has settled."""
        me = asyncio.current_task()
        empty = 0
        while empty < quiet_rounds:
            pending = [t for t in self._tasks if not t.done() and t is not me]
            if pending:
                empty = 0
                await asyncio.gather(*pending, return_exceptions=True)
                continue
            empty += 1
            await asyncio.sleep(gap)


class SeamLoader(QWidget):
    """Full-cover startup overlay: a baseball seam inking itself in.

    The seam is the REAL curve on the sphere, not a decorative squiggle:
    z = A·sin(2t) with the xy radius pinned to sqrt(1 - z²), which is
    identically on the unit sphere and is exactly the baseball/tennis seam
    shape. It is rotated in 3-D and orthographically projected each frame, so
    the far side of the stitching correctly dims out as the ball turns.

    !! SMOOTHNESS !!  The qasync loop IS the UI thread (see the loop-blocking
    notes elsewhere in this file), so a CPU-bound stretch of startup work
    cannot be painted through — frames WILL be dropped. Everything here is
    therefore driven off a wall clock rather than a frame counter: a dropped
    frame skips motion forward instead of slowing it down, so the animation
    reads as smooth-with-a-hitch rather than as stuttering slow-motion. The
    other half of that bargain lives in the startup path, which has to yield
    often enough for the repaint to land at all.

    Progress is LERPED toward the step count rather than snapped to it, so a
    milestone landing reads as the seam continuing to ink rather than as a
    jump.
    """

    finished = pyqtSignal()

    SEAM_A = 0.76          # seam amplitude — 0.76 is the baseball proportion
    N = 400                # seam samples (precomputed once)
    STITCHES = 60
    FADE_MS = 460
    IDLE_MS = 26           # a frame this quick means the loop is keeping up
    # ~300ms of unbroken calm. Six frames (100ms) was measured to be too
    # short a window: untracked fan-out work surfaced just after it passed
    # and stalled the fade anyway.
    IDLE_FRAMES = 18
    IDLE_CAP = 4000        # but never wait longer than this to start fading

    BG = QColor("#151a21")
    BALL_HI = QColor("#fbfcfd")
    BALL_MID = QColor("#e4e8ec")
    BALL_LO = QColor("#aab3bd")
    RED = QColor("#e74c3c")
    RED_FRESH = QColor("#ff8a7a")
    RING = QColor("#34495E")
    GRN = QColor("#2ecc71")
    TXT = QColor("#ecf0f1")
    DIM = QColor("#7f8c8d")

    def __init__(self, parent, steps: List[tuple]):
        super().__init__(parent)
        self._steps = list(steps)            # [(key, label), ...]
        self._pending = {k for k, _ in self._steps}
        self._label = self._steps[0][1] if self._steps else "loading…"
        self._shown = 0.0                    # lerped progress actually drawn
        self._fade_start: Optional[int] = None
        self._armed: Optional[int] = None    # finish() requested at (ms)
        self._calm = 0                       # consecutive on-time frames
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last = 0
        # Precompute the seam once — 400 sin/cos pairs per frame is pure
        # waste when the curve never changes, only its orientation does.
        self._seam = [self._seam_pt(i / self.N * 2 * math.pi)
                      for i in range(self.N + 1)]
        self.setAutoFillBackground(False)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self.update)
        self._timer.start(16)

    # ------------------------------------------------------------- geometry

    @classmethod
    def _seam_pt(cls, t: float) -> tuple:
        z = cls.SEAM_A * math.sin(2 * t)
        r = math.sqrt(max(0.0, 1.0 - z * z))
        return (r * math.cos(t), r * math.sin(t), z)

    @staticmethod
    def _rot(p: tuple, ax: float, ay: float) -> tuple:
        x, y, z = p
        c, s = math.cos(ay), math.sin(ay)
        x, z = x * c + z * s, -x * s + z * c
        c, s = math.cos(ax), math.sin(ax)
        y, z = y * c - z * s, y * s + z * c
        return (x, y, z)

    # -------------------------------------------------------------- driving

    def step(self, key: str, label: Optional[str] = None):
        """Mark one startup milestone done. Unknown/repeat keys are ignored
        so a caller can fire defensively without double-counting."""
        if key in self._pending:
            self._pending.discard(key)
        if label:
            self._label = label
        else:
            nxt = next((l for k, l in self._steps if k in self._pending), None)
            if nxt:
                self._label = nxt

    def finish(self):
        """Begin the fade-out once the loop is actually idle. Idempotent.

        The fade is the part of this that gets looked at hardest — it is the
        moment attention is on the overlay rather than on whatever is behind
        it — so it is the one stretch that must not stutter. Startup work is
        not perfectly fenced (a game selection fans out tasks nobody awaits),
        and a 400ms stall landing mid-fade reads as the window hanging right
        as it opens. So arm here and let `paintEvent` start the fade only
        after IDLE_FRAMES consecutive on-time frames prove the loop is free.

        IDLE_CAP bounds the wait: if the loop never goes quiet, fading a bit
        roughly still beats not fading at all."""
        if self._fade_start is None and not self._armed:
            self._armed = self._clock.elapsed()
            self._pending.clear()
            self._label = "ready"

    # ---------------------------------------------------------------- paint

    def paintEvent(self, a0):
        now = self._clock.elapsed()
        dt = max(0.0, (now - self._last) / 1000.0)
        self._last = now
        t = now / 1000.0

        # Wait for the loop to go quiet before committing to the fade.
        if self._armed is not None and self._fade_start is None:
            self._calm = self._calm + 1 if dt * 1000 <= self.IDLE_MS else 0
            if (self._calm >= self.IDLE_FRAMES
                    or now - self._armed >= self.IDLE_CAP):
                self._fade_start = now

        alpha = 1.0
        if self._fade_start is not None:
            alpha = 1.0 - min(1.0, (now - self._fade_start) / self.FADE_MS)
            if alpha <= 0.0:
                self._timer.stop()
                self.hide()
                self.finished.emit()
                self.deleteLater()
                return

        total = max(1, len(self._steps))
        target = (total - len(self._pending)) / total
        # Time-constant lerp, NOT a per-frame constant: the rate must not
        # depend on how many frames actually landed.
        self._shown += (target - self._shown) * min(1.0, dt * 3.2)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setOpacity(alpha)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), self.BG)

        cx, cy = w / 2.0, h / 2.0 - 18
        R = max(46.0, min(w, h) * 0.16)

        # ---- ball body
        g = QRadialGradient(cx - R * 0.4, cy - R * 0.45, R * 1.5)
        g.setColorAt(0.0, self.BALL_HI)
        g.setColorAt(0.55, self.BALL_MID)
        g.setColorAt(1.0, self.BALL_LO)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), R, R)

        # ---- seam, rotated + projected
        ay = t * 0.62
        ax = 0.42 + math.sin(t * 0.31) * 0.16
        pts = [self._rot(q, ax, ay) for q in self._seam]
        inked = self._smooth(self._shown)
        upto = int(inked * self.N)

        pen = QPen()
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        for i in range(upto):
            a, b = pts[i], pts[i + 1]
            front = (a[2] + b[2]) * 0.5 > 0
            col = QColor(self.RED)
            col.setAlphaF((0.95 if front else 0.16) * alpha)
            pen.setColor(col)
            pen.setWidthF(2.1 if front else 1.4)
            p.setPen(pen)
            p.drawLine(QPointF(cx + a[0] * R, cy - a[1] * R),
                       QPointF(cx + b[0] * R, cy - b[1] * R))

        # ---- stitches: V pairs perpendicular to the seam tangent
        s = R * 0.085
        for i in range(self.STITCHES):
            u = i / self.STITCHES
            if u > inked:
                break
            j = int(u * self.N)
            a, b = pts[j], pts[min(j + 1, self.N)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy) or 1e-6
            nx, ny = -dy / L, dx / L
            px, py = cx + a[0] * R, cy - a[1] * R
            front = a[2] > 0
            age = min(1.0, max(0.0, (inked - u) * 26.0))
            col = QColor(self.RED if age >= 1.0 else self.RED_FRESH)
            col.setAlphaF((0.95 if front else 0.14)
                          * (0.55 + 0.45 * (1.0 - age)) * alpha)
            pen.setColor(col)
            pen.setWidthF(1.9 if front else 1.2)
            p.setPen(pen)
            tip = QPointF(px + nx * s, py - ny * s)
            p.drawLine(QPointF(px - nx * s - dx / L * s * 0.5,
                               py + ny * s + dy / L * s * 0.5), tip)
            p.drawLine(QPointF(px - nx * s + dx / L * s * 0.5,
                               py + ny * s - dy / L * s * 0.5), tip)

        # ---- progress ring
        # NoBrush first: the ball's radial gradient is still the active brush,
        # and drawEllipse FILLS. Left set, the ring painted a solid disc of
        # radius 1.34R straight over the seam — which read as "the ball is
        # too big and the stitching never appears".
        p.setBrush(Qt.BrushStyle.NoBrush)
        rr = R * 1.34
        box = QRectF(cx - rr, cy - rr, rr * 2, rr * 2)
        col = QColor(self.RING)
        col.setAlphaF(alpha)
        pen.setColor(col)
        pen.setWidthF(2.5)
        p.setPen(pen)
        p.drawEllipse(box)
        col = QColor(self.GRN)
        col.setAlphaF(alpha)
        pen.setColor(col)
        p.setPen(pen)
        # Qt angles are 1/16 degree, counter-clockwise from 3 o'clock
        p.drawArc(box, 90 * 16, -int(self._smooth(self._shown) * 360 * 16))

        # ---- wordmark + status
        f = QFont(self.font())
        f.setPointSizeF(15.0)
        f.setBold(True)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5.0)
        p.setFont(f)
        col = QColor(self.TXT)
        col.setAlphaF(alpha)
        p.setPen(col)
        p.drawText(QRectF(0, cy + rr + 26, w, 26),
                   int(Qt.AlignmentFlag.AlignHCenter), "EFFORTMLB")
        f.setPointSizeF(9.5)
        f.setBold(False)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.0)
        p.setFont(f)
        col = QColor(self.DIM)
        col.setAlphaF(alpha)
        p.setPen(col)
        p.drawText(QRectF(0, cy + rr + 52, w, 20),
                   int(Qt.AlignmentFlag.AlignHCenter), self._label)

        # ---- step pips
        n = len(self._steps)
        done = n - len(self._pending)
        pw, gap = 16, 6
        tw = n * pw + (n - 1) * gap
        sx = (w - tw) / 2.0
        for i in range(n):
            col = QColor(self.GRN if i < done else self.RING)
            col.setAlphaF((1.0 if i < done else 0.55) * alpha)
            p.fillRect(QRectF(sx + i * (pw + gap), cy + rr + 78, pw, 3), col)
        p.end()

    @staticmethod
    def _smooth(v: float) -> float:
        v = 0.0 if v < 0 else (1.0 if v > 1 else v)
        return v * v * (3 - 2 * v)


class InsetSplitterHandle(QSplitterHandle):
    """A splitter handle that reads as part of the panel on its LEFT rather
    than as a trough between the two panels.

    Qt always places a handle in the gap BETWEEN two widgets, and there is no
    way to move one inside a child. What actually sells "inside the lineup
    panel" is where the panel's BORDER falls: fill the handle with the rail's
    own background and redraw its 1px border down the handle's far edge, and
    the grip is now enclosed by the panel outline instead of floating outside
    it. The rail is visually RAIL_W + handleWidth wide as a result.

    `inset` is set per-handle after construction — the pitcher|tabs handle is
    a genuine resizer between two equals and keeps the plain look.
    """

    BG = QColor("#151a21")        # LineupRail's background
    BORDER = QColor("#2C3E50")    # ...and its border
    GRIP = QColor("#5D6D7E")
    GRIP_HOVER = QColor("#dc9437")

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.inset = False
        self._hover = False

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, a0):
        self._hover = False
        self.update()
        super().leaveEvent(a0)

    def paintEvent(self, a0):
        if not self.inset:
            super().paintEvent(a0)
            return
        p = QPainter(self)
        # Without this the 2.8px grip dots quantise away to nothing.
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect()
        p.fillRect(r, self.BG)
        # the rail's border, moved to the OUTSIDE of the grip. The rail's own
        # right border is suppressed by MLBWindow so this is the only one.
        p.setPen(self.BORDER)
        p.drawLine(r.right(), r.top(), r.right(), r.bottom())
        # grip: a short dotted rule at the vertical centre
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.GRIP_HOVER if self._hover else self.GRIP)
        cx, cy, n, gap = r.center().x() + 0.5, r.center().y(), 7, 4.5
        for i in range(n):
            p.drawEllipse(QPointF(cx, cy + (i - (n - 1) / 2) * gap), 1.4, 1.4)
        p.end()


class InsetSplitter(QSplitter):
    """QSplitter whose handles are `InsetSplitterHandle`. Which of them
    actually draw inset is decided by the caller (see MLBWindow)."""

    def createHandle(self):
        return InsetSplitterHandle(self.orientation(), self)

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
        self._selected = -1
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

    def refresh_games(self, games: List[dict], teams: Dict[int, str]):
        """Re-render from a fresh schedule pull WITHOUT re-selecting.

        The banner owns the game dicts the rest of the window is driven from,
        so a refresh has to replace them, not merely repaint — a lineup that
        posts after launch arrives inside these dicts. Re-emitting
        `game_selected` would reload every panel from scratch, so selection is
        restored silently and the caller decides what actually needs redrawing.
        Matched on gamePk: a postponement or a suspended-game resumption can
        change the slate's length mid-day, and index equality would then move
        the highlight to a different game."""
        pk = None
        if 0 <= self._selected < len(self._games):
            pk = self._games[self._selected].get("gamePk")
        self.set_games(games, teams)
        idx = next((i for i, g in enumerate(games)
                    if g.get("gamePk") == pk), -1) if pk is not None else -1
        if idx >= 0:
            for i, c in enumerate(self._cards):
                c.set_selected(i == idx)
            self._selected = idx

    def current_game(self) -> Optional[dict]:
        if 0 <= self._selected < len(self._games):
            return self._games[self._selected]
        return None

    def select(self, idx: int):
        if not (0 <= idx < len(self._games)):
            return
        self._selected = idx
        for i, c in enumerate(self._cards):
            c.set_selected(i == idx)
        self.game_selected.emit(self._games[idx])


PID_ROLE = Qt.ItemDataRole.UserRole + 1
BATTER_ROLE = Qt.ItemDataRole.UserRole + 2      # True on batter rows
STATS_ROLE = Qt.ItemDataRole.UserRole + 3       # FG value dict or None
POS_ROLE = Qt.ItemDataRole.UserRole + 4         # primary position abbrev


class LineupCardDelegate(QStyledItemDelegate):
    """Paints batter rows as a mugshot + name with a 2x2 grid of value stats
    (wRC+ / Def / WPA / BsR) beneath, color-coded good/bad. Catchers get a
    third row with their receiving numbers (Frm / Blk runs). Header, pitcher,
    and roster-note rows fall back to the default list rendering."""

    ICON = 30
    PAD = 4
    NAME_H = 15
    CELL_H = 13
    _GREY = QColor("#7F8C8D")

    def _is_card(self, index):
        return bool(index.data(BATTER_ROLE))

    @staticmethod
    def _has_receiving(stats) -> bool:
        """Catchers whose framing/blocking board rows arrived get a 3rd row."""
        return bool(stats) and (stats.get("framing_runs") is not None
                                or stats.get("blocking_runs") is not None)

    def sizeHint(self, option, index):
        if self._is_card(index):
            rows = 3 if self._has_receiving(index.data(STATS_ROLE)) else 2
            return QSize(120, self.PAD + self.NAME_H + rows * self.CELL_H + 3)
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

        # 2x2 stat grid (3rd row of receiving stats for catchers)
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
        if self._has_receiving(stats):
            cells += [
                ("Frm", stats.get("framing_runs"),
                 lambda v: f"{v:+.1f}",
                 self._heat(stats.get("framing_runs"), 2.0, -2.0)),
                ("Blk", stats.get("blocking_runs"),
                 lambda v: f"{v:+.1f}",
                 self._heat(stats.get("blocking_runs"), 1.0, -1.0)),
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
    POS_ROLE = POS_ROLE

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
                    pid: Optional[int] = None, pos: str = ""):
        it = QListWidgetItem(text)
        it.setData(Qt.ItemDataRole.UserRole, (name, is_pitcher))
        if pid:
            it.setData(self.PID_ROLE, pid)
        it.setData(self.POS_ROLE, pos)
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
                                     p.get("id"), pos)
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
                                     r["name"], False, r["id"],
                                     r["position"])



# ===========================================================================
# 4. GAME REPLAY
# ===========================================================================
#
# A Gameday-class replay of a finished (or in-progress) game, driven by the
# StatsAPI live feed and rendered on the REAL park outline from weatherman's
# polar wall equations.
#
#   ReplayFeed   - fetch + per-gamePk disk cache of the v1.1 live feed
#   build_replay - feed dict -> ReplayGame (the whole script, precomputed)
#   ReplayTab    - the tab, plus the painters it owns
#
# Everything the animation needs is computed ONCE in build_replay. The qasync
# loop is the UI thread, so nothing here derives geometry inside a timer tick;
# a frame is a lookup, not a calculation.
#
# Why this does NOT reuse `PBP_FIELDS`: that whitelist is tuned for the
# manager board's aggregates and carries none of the pitch coordinates, hit
# data or fielding credits a replay is made of. This is a different endpoint
# (v1.1 feed/live, not v1 playByPlay) with its own whitelist below.

REPLAY_DIR = SAVE_DIR / "replay"
FEED_BASE = "https://statsapi.mlb.com/api/v1.1"
# the feed names bases "1B"/"2B"/"3B"; this fixes their index order
_BASE_ORDER = ("1B", "2B", "3B")

# --- feed whitelist --------------------------------------------------------
# The untrimmed feed is ~0.87MB per game. Every name below is one the parser
# under it actually reads.
#
# !! COUPLING !!  `fields` is a whitelist applied at EVERY depth, and ancestor
# keys must be listed too (`coordinates` as well as `pX`). Consumers all use
# .get() with defaults, so a key you forget to add reads as absent rather than
# raising. If you make the parser read a NEW key, add it here in the SAME edit
# or the feature silently computes nothing.
GUMBO_FIELDS = ",".join((
    "metaData", "timeStamp",
    # gameData
    "gameData", "teams", "home", "away", "abbreviation", "teamName", "id",
    "venue", "name", "datetime", "officialDate", "status", "abstractGameState",
    # plays
    "liveData", "plays", "allPlays",
    "result", "event", "eventType", "description", "rbi",
    "awayScore", "homeScore", "isOut",
    "about", "atBatIndex", "halfInning", "isTopInning", "inning",
    "isScoringPlay", "hasOut", "isComplete",
    "count", "balls", "strikes", "outs",
    "matchup", "batter", "fullName", "batSide", "code", "pitcher", "pitchHand",
    "runners", "movement", "originBase", "start", "end", "outBase", "outNumber",
    "details", "runner", "isScoringEvent", "playIndex",
    "credits", "player", "position", "credit",
    "playEvents", "isPitch", "type", "call", "isInPlay", "isStrike", "isBall",
    "index", "pitchNumber", "isSubstitution",
    "pitchData", "startSpeed", "strikeZoneTop", "strikeZoneBottom",
    "coordinates", "pX", "pZ", "x0", "y0", "z0", "vX0", "vY0", "vZ0",
    "aX", "aY", "aZ", "pfxX", "pfxZ", "extension",
    "hitData", "launchSpeed", "launchAngle", "totalDistance", "trajectory",
    "coordX", "coordY", "location",
    # linescore + boxscore
    "linescore", "innings", "num", "runs", "hits", "errors",
    "boxscore", "players", "person", "battingOrder", "allPositions",
    "seasonStats", "batting", "pitching", "avg", "ops", "homeRuns", "era",
    "strikeOuts", "baseOnBalls", "inningsPitched", "whip", "stats",
    "numberOfPitches", "jerseyNumber",
))


@dataclass
class Pitch:
    """One pitch, with everything needed to draw it and fly it."""
    number: int
    code: str                     # call code: B, C, S, F, X, D, E, ...
    desc: str                     # "Called Strike"
    ptype: str                    # "FF"
    pname: str                    # "4-Seam Fastball"
    mph: Optional[float]
    px: Optional[float]           # plate crossing, feet from centre
    pz: Optional[float]
    sz_top: Optional[float]
    sz_bot: Optional[float]
    balls: int                    # count AFTER this pitch
    strikes: int
    in_play: bool
    # release + constant-acceleration kinematics; same shape pitch_sim eats
    kin: Optional[dict] = None


@dataclass
class Hit:
    ev: Optional[float]
    la: Optional[float]
    distance: Optional[float]
    trajectory: str
    # derived from the spray grid, in the physics convention used everywhere
    # else in this codebase: 0 deg = dead centre, + toward RF
    angle: Optional[float]
    calc_distance: Optional[float]


@dataclass
class Play:
    index: int
    inning: int
    is_top: bool
    event: str
    event_type: str
    desc: str
    outs_before: int
    outs_after: int
    bases_before: Tuple[bool, bool, bool]
    bases_after: Tuple[bool, bool, bool]
    away_score: int
    home_score: int
    scoring: bool
    batter_id: int
    batter: str
    bat_side: str
    pitcher_id: int
    pitcher: str
    pitch_hand: str
    pitches: List[Pitch] = field(default_factory=list)
    hit: Optional[Hit] = None
    # position abbr -> (player id, name), as of THIS play
    defense: Dict[str, Tuple[int, str]] = field(default_factory=dict)
    # fielders credited on the play, in order (position abbr)
    credits: List[str] = field(default_factory=list)
    # every runner's journey this play: {id, name, frm, to, out}, where base
    # numbers are 0 = batter's box, 1/2/3 = bases, 4 = scored
    runner_moves: List[dict] = field(default_factory=list)
    # who is standing on each bag as the play STARTS: {1|2|3: (id, name)}.
    # bases_before only says a bag is occupied; a man who was already there
    # and does not move never appears in runner_moves, so without this he
    # renders as an anonymous empty tile.
    runners_on: Dict[int, Tuple[int, str]] = field(default_factory=dict)
    pitcher_pitches: int = 0      # this pitcher's cumulative pitch count
    # priced from the season's OWN tables by annotate_win_expectancy
    we: Optional[float] = None          # home win expectancy BEFORE the play
    we_delta: Optional[float] = None    # swing this play produced, home POV
    li: Optional[float] = None          # leverage index of the state


@dataclass
class ReplayGame:
    game_pk: int
    away: str
    home: str
    away_id: int
    home_id: int
    venue: str
    date: str
    final: bool
    plays: List[Play]
    innings: List[dict]           # [{num, away, home}]
    totals: Dict[str, dict]       # {'away': {r,h,e}, 'home': {...}}
    lineups: Dict[str, List[dict]]
    season_stats: Dict[int, dict]


# ===========================================================================
# fetch
# ===========================================================================

class ReplayFeed:
    """Fetches and caches the live feed for one game at a time.

    A FINAL game's feed never changes again, so it is cached on disk forever
    and re-read instead of re-fetched — the same reasoning as the season
    play-by-play checkpoint. A game still in progress is never written to
    disk, because its feed is still growing.
    """

    def __init__(self):
        self._mem: Dict[int, dict] = {}

    @staticmethod
    def _path(game_pk: int) -> Path:
        return REPLAY_DIR / f"{game_pk}.json"

    async def get(self, session: aiohttp.ClientSession, game_pk: int,
                  refresh: bool = False) -> Optional[dict]:
        """Feed for one game. `refresh` forces a re-fetch.

        Only FINAL games are memoised or written to disk. A live game's feed
        grows with every pitch, so serving it from cache would freeze the
        replay at whatever the score was the first time you opened it.
        """
        if not refresh and game_pk in self._mem:
            return self._mem[game_pk]
        p = self._path(game_pk)
        if not refresh and p.exists():
            try:
                data = json_loads(p.read_bytes())
                self._mem[game_pk] = data
                return data
            except Exception as e:
                print(f"Replay: cached feed for {game_pk} unreadable ({e})")
        try:
            async with session.get(
                    f"{FEED_BASE}/game/{game_pk}/feed/live",
                    params={"fields": GUMBO_FIELDS},
                    timeout=aiohttp.ClientTimeout(total=45)) as resp:
                if resp.status != 200:
                    print(f"Replay: feed {game_pk} HTTP {resp.status}")
                    return None
                raw = await resp.read()
        except Exception as e:
            print(f"Replay: feed {game_pk} failed: {e}")
            return None
        try:
            data = json_loads(raw)
        except ValueError as e:
            print(f"Replay: feed {game_pk} unparseable: {e}")
            return None
        state = (((data.get("gameData") or {}).get("status") or {})
                 .get("abstractGameState"))
        if state == "Final":
            self._mem[game_pk] = data
            try:
                REPLAY_DIR.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(p)
            except OSError as e:
                print(f"Replay: feed cache write failed: {e}")
        return data


# ===========================================================================
# build
# ===========================================================================

def _spray_to_physics(cx, cy) -> Tuple[Optional[float], Optional[float]]:
    """Savant spray pixel coords -> (distance ft, angle deg).

    Angle is the physics convention used across this codebase: 0 = dead
    centre, + toward RF, - toward LF.
    """
    if cx is None or cy is None:
        return None, None
    dx = (cx - _SPRAY_HP[0]) * _SPRAY_SCALE
    dy = (_SPRAY_HP[1] - cy) * _SPRAY_SCALE
    if dy <= 0:
        return None, None
    return math.hypot(dx, dy), math.degrees(math.atan2(dx, dy))


def _starting_defense(box_side: dict) -> Dict[str, Tuple[int, str]]:
    """The alignment at first pitch.

    `battingOrder` on a boxscore player is slot*100 plus a substitution
    index, so a starter's ends in "00" — the top-level `battingOrder` list is
    the END-of-game order and will happily tell you a pinch hitter started.
    `allPositions[0]` is the position he took first.

    Under the DH the starting PITCHER is in no batting slot at all, so he
    never appears in that walk; `pitchers[0]` is the one who threw first.
    Without this the mound reads empty until the first pitching change.
    """
    players = box_side.get("players") or {}
    out: Dict[str, Tuple[int, str]] = {}
    for p in players.values():
        bo = p.get("battingOrder")
        if not bo or not str(bo).endswith("00"):
            continue
        aps = p.get("allPositions") or []
        if not aps:
            continue
        abbr = aps[0].get("abbreviation")
        if not abbr or abbr == "DH":
            continue
        out[abbr] = (p["person"]["id"], p["person"]["fullName"])
    if "P" not in out:
        for pid in (box_side.get("pitchers") or [])[:1]:
            p = players.get(f"ID{pid}")
            if p:
                out["P"] = (pid, p["person"]["fullName"])
    return out


def build_replay(feed: dict) -> Optional[ReplayGame]:
    gd, ld = feed.get("gameData") or {}, feed.get("liveData") or {}
    all_plays = (ld.get("plays") or {}).get("allPlays") or []
    if not all_plays:
        return None
    teams = gd.get("teams") or {}
    box = (ld.get("boxscore") or {}).get("teams") or {}

    # defense per side, walked forward through substitutions
    defense = {"home": _starting_defense(box.get("home") or {}),
               "away": _starting_defense(box.get("away") or {})}
    names = {}
    for side in ("home", "away"):
        for p in ((box.get(side) or {}).get("players") or {}).values():
            names[p["person"]["id"]] = p["person"]["fullName"]

    bases: List[Optional[int]] = [None, None, None]
    cur_half = None
    pitch_counts: Dict[int, int] = {}
    plays: List[Play] = []

    for raw in all_plays:
        about = raw.get("about") or {}
        res = raw.get("result") or {}
        mu = raw.get("matchup") or {}
        inning, is_top = about.get("inning"), about.get("isTopInning")
        half = (inning, is_top)
        if half != cur_half:
            cur_half = half
            bases = [None, None, None]
        # the side ON DEFENSE for this play
        dside = "home" if is_top else "away"

        bases_before = tuple(b is not None for b in bases)
        runners_on = {i + 1: (bases[i], names.get(bases[i], ""))
                      for i in range(3) if bases[i] is not None}
        outs_before = _outs_before(raw, plays, half)

        # --- substitutions take effect for the NEXT play, but a pitching
        # change mid-at-bat must show immediately, so they are applied as the
        # events are walked rather than after the play.
        pitches: List[Pitch] = []
        balls = strikes = 0
        hit: Optional[Hit] = None
        for e in (raw.get("playEvents") or []):
            if e.get("isSubstitution"):
                _apply_sub(e, defense[dside], names)
                continue
            if not e.get("isPitch"):
                continue
            det = e.get("details") or {}
            code = ((det.get("call") or {}).get("code")
                    or det.get("code") or "")
            in_play = bool(det.get("isInPlay"))
            if det.get("isBall") and not in_play:
                balls = min(4, balls + 1)
            elif det.get("isStrike") and not in_play:
                strikes = min(3, strikes + 1)
            pd = e.get("pitchData") or {}
            co = pd.get("coordinates") or {}
            kin = None
            if co.get("vY0") is not None:
                kin = {k: co.get(k) for k in
                       ("x0", "y0", "z0", "vX0", "vY0", "vZ0", "aX", "aY", "aZ")}
            pt = det.get("type") or {}
            pitches.append(Pitch(
                number=e.get("pitchNumber") or len(pitches) + 1,
                code=code,
                desc=(det.get("call") or {}).get("description")
                or det.get("description") or "",
                ptype=pt.get("code") or "", pname=pt.get("description") or "",
                mph=pd.get("startSpeed"),
                px=co.get("pX"), pz=co.get("pZ"),
                sz_top=pd.get("strikeZoneTop"), sz_bot=pd.get("strikeZoneBottom"),
                balls=balls, strikes=strikes, in_play=in_play, kin=kin))
            hd = e.get("hitData")
            if hd:
                hc = hd.get("coordinates") or {}
                d, ang = _spray_to_physics(hc.get("coordX"), hc.get("coordY"))
                hit = Hit(ev=hd.get("launchSpeed"), la=hd.get("launchAngle"),
                          distance=hd.get("totalDistance"),
                          trajectory=hd.get("trajectory") or "",
                          angle=ang, calc_distance=d)

        pid = (mu.get("pitcher") or {}).get("id")
        if pid is not None:
            pitch_counts[pid] = pitch_counts.get(pid, 0) + len(pitches)

        # --- advance the bases with the play's runner movements
        credits: List[str] = []
        moves: List[dict] = []
        for r in (raw.get("runners") or []):
            mv = r.get("movement") or {}
            det = r.get("details") or {}
            rid = (det.get("runner") or {}).get("id")
            start, end = mv.get("start"), mv.get("end")
            # A retired runner has end=None; where he was RETIRED is in
            # outBase. Without it a man forced at second stands on first for
            # the whole play instead of running into the out.
            dest = end if end else (mv.get("outBase") if mv.get("isOut")
                                    else None)
            moves.append({
                "id": rid,
                "name": ((det.get("runner") or {}).get("fullName") or ""),
                "frm": _base_num(start), "to": _base_num(dest),
                "out": bool(mv.get("isOut"))})
            if start in _BASE_ORDER and bases[_BASE_ORDER.index(start)] == rid:
                bases[_BASE_ORDER.index(start)] = None
            if mv.get("isOut"):
                continue                      # off the bases entirely
            if end in _BASE_ORDER:
                bases[_BASE_ORDER.index(end)] = rid
            # 'score' and None both mean "no longer on a base"
            for c in (r.get("credits") or []):
                ab = (c.get("position") or {}).get("abbreviation")
                if ab and ab not in credits:
                    credits.append(ab)

        live_pa = not about.get("isComplete", True)
        ev = res.get("event") or ""
        desc = res.get("description") or ""
        if live_pa and not ev:
            # the at-bat in progress has no result yet; say so rather than
            # rendering a blank row in the log
            b = pitches[-1].balls if pitches else 0
            k = pitches[-1].strikes if pitches else 0
            ev = "AT BAT"
            desc = (f"{(mu.get('batter') or {}).get('fullName', '')} batting, "
                    f"{b}-{k}")
        plays.append(Play(
            index=about.get("atBatIndex", len(plays)),
            inning=inning or 1, is_top=bool(is_top),
            event=ev, event_type=res.get("eventType") or "",
            desc=desc,
            outs_before=outs_before,
            outs_after=(raw.get("count") or {}).get("outs", outs_before),
            bases_before=bases_before,
            bases_after=tuple(b is not None for b in bases),
            away_score=res.get("awayScore", 0), home_score=res.get("homeScore", 0),
            scoring=bool(about.get("isScoringPlay")),
            batter_id=(mu.get("batter") or {}).get("id") or 0,
            batter=(mu.get("batter") or {}).get("fullName") or "",
            bat_side=(mu.get("batSide") or {}).get("code") or "",
            pitcher_id=pid or 0,
            pitcher=(mu.get("pitcher") or {}).get("fullName") or "",
            pitch_hand=(mu.get("pitchHand") or {}).get("code") or "",
            pitches=pitches, hit=hit,
            defense=dict(defense[dside]), credits=credits,
            runner_moves=_dedupe_moves(moves), runners_on=runners_on,
            pitcher_pitches=pitch_counts.get(pid, 0)))

    ls = ld.get("linescore") or {}
    innings = [{"num": i.get("num"),
                "away": (i.get("away") or {}).get("runs"),
                "home": (i.get("home") or {}).get("runs")}
               for i in (ls.get("innings") or [])]
    lt = ls.get("teams") or {}
    totals = {s: {"r": (lt.get(s) or {}).get("runs", 0),
                  "h": (lt.get(s) or {}).get("hits", 0),
                  "e": (lt.get(s) or {}).get("errors", 0)}
              for s in ("away", "home")}

    lineups, season = {}, {}
    for side in ("away", "home"):
        rows = []
        for p in ((box.get(side) or {}).get("players") or {}).values():
            bo = p.get("battingOrder")
            season[p["person"]["id"]] = p.get("seasonStats") or {}
            if bo and str(bo).endswith("00"):
                aps = p.get("allPositions") or []
                rows.append({"order": int(bo) // 100,
                             "id": p["person"]["id"],
                             "name": p["person"]["fullName"],
                             "pos": aps[0].get("abbreviation") if aps else ""})
        lineups[side] = sorted(rows, key=lambda r: r["order"])

    return ReplayGame(
        game_pk=feed.get("gamePk") or 0,
        away=(teams.get("away") or {}).get("abbreviation") or "AWY",
        home=(teams.get("home") or {}).get("abbreviation") or "HOM",
        away_id=(teams.get("away") or {}).get("id") or 0,
        home_id=(teams.get("home") or {}).get("id") or 0,
        venue=(gd.get("venue") or {}).get("name") or "",
        date=(gd.get("datetime") or {}).get("officialDate") or "",
        final=(((gd.get("status") or {}).get("abstractGameState")) == "Final"),
        plays=plays, innings=innings, totals=totals,
        lineups=lineups, season_stats=season)



def _base_num(b) -> int:
    """Feed base label -> index. 0 is the batter's box, 4 is a run scored."""
    return {None: 0, "1B": 1, "2B": 2, "3B": 3, "score": 4}.get(b, 0)


def _dedupe_moves(moves: List[dict]) -> List[dict]:
    """One entry per runner: where he began the play and where he ended it.

    The feed emits a `runners` row per movement, so a man who goes first to
    third on a single appears twice. Collapsing to (first origin, last
    destination) is what the animation needs — the intermediate hop is on the
    same base path anyway.
    """
    out: Dict[int, dict] = {}
    for m in moves:
        rid = m["id"]
        if rid is None:
            continue
        if rid in out:
            out[rid]["to"] = m["to"]
            out[rid]["out"] = out[rid]["out"] or m["out"]
        else:
            out[rid] = dict(m)
    return [m for m in out.values()
            if (m["to"] != m["frm"] or m["out"])
            # a strikeout/flyout victim goes 0 -> 0: he was never on a base
            # and drawing him would park a tile on home plate
            and not (m["frm"] == 0 and m["to"] == 0)]


def _outs_before(raw: dict, plays: List[Play], half) -> int:
    """Outs at the START of a play.

    The feed's `count.outs` on a play is the count at its END, so the start
    is the previous play's end — unless this is the first play of a half,
    which always starts at zero.
    """
    if not plays:
        return 0
    prev = plays[-1]
    if (prev.inning, prev.is_top) != half:
        return 0
    return prev.outs_after


def _apply_sub(e: dict, d: Dict[str, Tuple[int, str]],
               names: Dict[int, str]) -> None:
    """Fold one substitution action into the DEFENSIVE side's alignment.

    Only defensive changes move a glove. An offensive substitution (pinch
    hitter or runner) changes nobody's position until that player later takes
    the field, which arrives as its own defensive_switch.

    The event does not name a team, so the caller passes the side: a pitching
    change or a defensive switch always belongs to whoever is in the field on
    the play carrying it. Deciding by position instead would put every away
    pitching change on the home club, since "P" is a key in both alignments.
    """
    det = e.get("details") or {}
    if (det.get("eventType") or "") not in (
            "defensive_substitution", "defensive_switch",
            "pitching_substitution"):
        return
    pos = (e.get("position") or {}).get("abbreviation")
    pid = (e.get("player") or {}).get("id")
    if not pos or pid is None or pos == "DH":
        return
    for k, v in list(d.items()):
        if v[0] == pid and k != pos:
            del d[k]                      # he moved; vacate the old spot
    d[pos] = (pid, names.get(pid, ""))



def annotate_win_expectancy(game: ReplayGame, stats) -> bool:
    """Attach win expectancy, its per-play swing, and leverage to every play.

    Priced from the SEASON'S OWN win-expectancy grid — the same tables the
    manager board builds off the play-by-play checkpoint — not an imported
    table. Reads from disk only; never touches the network.

    Note the tables are current-season. Replaying an older game still works,
    but its win probabilities are then priced off this season's league
    behaviour, which is an approximation (WE grids move very little year to
    year) rather than a measurement of that season.
    """
    if stats is None:
        return False
    if not getattr(stats, "_we_acc", None):
        try:
            stats.load_league_tables()
        except Exception:
            return False
    acc = getattr(stats, "_we_acc", None) or {}
    trans = getattr(stats, "_we_trans", None) or {}
    if not acc:
        return False
    # Fitting the WE surface costs ~45ms, and under qasync that is 45ms of
    # frozen UI. A live poll re-annotates every 15s while the league tables
    # it fits are IDENTICAL between polls, so the fit is cached against the
    # table sizes and only redone when the checkpoint actually grows.
    sig = (len(acc), len(trans))
    cached = getattr(stats, "_we_surface", None)
    if cached and cached[0] == sig:
        we, li = cached[1], cached[2]
    else:
        try:
            states = {k for pair in trans for k in pair} | set(acc)
            we = smooth_win_expectancy(acc, states)
            if not we:
                we = {k: v[0] / v[1] for k, v in acc.items() if v[1] >= 30}
            li = build_leverage(acc, trans)
        except Exception as e:
            print(f"Replay: win expectancy unavailable ({e})")
            return False
        stats._we_surface = (sig, we, li)

    def key(pl: Play):
        base = ((1 if pl.bases_before[0] else 0) |
                (2 if pl.bases_before[1] else 0) |
                (4 if pl.bases_before[2] else 0))
        # lead is from the HOME club's view, and the scores on a play are its
        # END state, so the state entering play i uses play i-1's scores
        return we_key(pl.inning, pl.is_top, pl._lead_before, base,
                      pl.outs_before)

    prev_a = prev_h = 0
    for pl in game.plays:
        pl._lead_before = prev_h - prev_a
        prev_a, prev_h = pl.away_score, pl.home_score
    for pl in game.plays:
        k = key(pl)
        pl.we = we.get(k)
        pl.li = li.get(k)
    # the swing of a play is the change in WE it produced: the next state's
    # value minus this one's, with the final play resolving to the result
    final = game.plays[-1]
    outcome = 1.0 if final.home_score > final.away_score else (
        0.0 if final.home_score < final.away_score else None)
    for i, pl in enumerate(game.plays):
        if pl.we is None:
            continue
        nxt = game.plays[i + 1].we if i + 1 < len(game.plays) else outcome
        if nxt is not None:
            pl.we_delta = nxt - pl.we
    return any(pl.we is not None for pl in game.plays)


# ===========================================================================
# park geometry
# ===========================================================================

# MLB renames parks faster than weatherman's table does, and one differs only
# by case. Without this map five of the thirty clubs silently fall back to a
# generic outline — and it fails quietly, because a missing key just means
# "no polar_coords" rather than an error.
VENUE_ALIASES = {
    "oriole park at camden yards": "Camden Yards",
    "rate field": "Guaranteed Rate Field",              # renamed 2025
    "daikin park": "Minute Maid Park",                  # renamed 2025
    "uniqlo field at dodger stadium": "Dodger Stadium",
    "loandepot park": "LoanDepot Park",                 # case only
}

# Fallback for neutral sites and spring parks weatherman has never seen.
GENERIC_WALL = [(0, 330), (15, 365), (30, 395), (45, 400),
                (60, 395), (75, 365), (90, 330)]


def resolve_venue(name: str):
    """Venue name -> the key weatherman's STADIUM_DATA actually uses."""
    if not name:
        return None
    try:
        import weatherman as W
    except Exception:
        return None
    data = W.STADIUM_DATA
    if name in data:
        return name
    low = name.lower()
    alias = VENUE_ALIASES.get(low)
    if alias and alias in data:
        return alias
    for k in data:
        if k.lower() == low:
            return k
    return None


def wall_profile(venue: str, step: float = 1.0):
    """[(angle_from_centre_deg, distance_ft)] for the outfield wall.

    Angle is the convention the rest of this file uses — 0 = dead centre,
    + toward RF — converted from weatherman's polar table, where 0 is the RF
    line and 90 the LF line.
    """
    key = resolve_venue(venue)
    if key is None:
        return [(45.0 - a, d) for a, d in GENERIC_WALL], None
    import weatherman as W
    out = []
    a = 0.0
    while a <= 90.0:
        d = W.get_stadium_wall_distance(key, a)
        if d:
            out.append((45.0 - a, d))
        a += step
    if not out:
        return [(45.0 - a, d) for a, d in GENERIC_WALL], None
    out.sort(key=lambda t: t[0])
    return out, key


def wall_height(venue: str, angle_from_centre: float) -> float:
    key = resolve_venue(venue)
    if key is None:
        return 8.0
    import weatherman as W
    return W.get_stadium_wall_height(key, 45.0 - angle_from_centre)


# ===========================================================================
# widgets
# ===========================================================================

# Replay palette. These are the window's own colours, named locally so the
# replay's painters read as one piece; PITCH_COLORS and the headshot cache
# above are shared with the rest of the file rather than duplicated.
GROUND = "#151a21"
PANEL = "#1a2029"
PANEL_HI = "#1E2A38"
RULE = "#2C3E50"
RULE_DIM = "#243140"
INK = "#c9d6e2"
DIM = "#7d8b9b"
FAINT = "#4e5b68"
OUT_C = "#E74C3C"
SAFE_C = "#2ECC71"
LEV_C = "#F4D03F"
INFO_C = "#3498DB"

TURF = "#16241e"
TURF_HI = "#1b2c24"
DIRT = "#2b2119"

# where each fielder stands: (angle off dead centre, feet from the plate)
# Where each fielder stands: (angle off dead centre, feet from the plate).
# 1B and 3B are deliberately deeper and more toward the middle than a real
# corner plays. A runner on the bag is now a TILE, not a dot, so the corner
# needs to clear ~48ft rather than ~36 — at their true ~110ft depth the two
# tiles overlap at any usable scale. The cost is that the corners read a
# little deep; the benefit is you can see who is standing on the base.
# Six infield tiles plus up to three runner tiles have to share a ~130ft
# radius while the park runs to 400, and a tile is a FIXED pixel size — so the
# infield is where crowding bites. The middle infielders are pulled wider and
# deeper than a real alignment to buy separation from the corners (SS/3B come
# out ~65ft apart instead of ~41), and the corners sit deep enough to clear a
# runner standing on the bag.
FIELDER_SPOTS = {
    "P": (0, 60.5), "C": (0, -9), "1B": (38, 129), "2B": (16, 163),
    "SS": (-16, 163), "3B": (-38, 127), "LF": (-30, 288),
    "CF": (2, 318), "RF": (30, 292),
}
BASE_SPOTS = {"1B": (45, 90), "2B": (0, 127.3), "3B": (-45, 90)}


class HeadshotCache:
    """Disk-backed headshot pixmaps, fetched once and reused.

    Shared by every widget that draws a face, so a player who appears as both
    a fielder puck and a rail card is downloaded once.
    """

    def __init__(self):
        self._pix: Dict[int, QPixmap] = {}
        self._pending: set = set()
        self._nam = QNetworkAccessManager()
        self._nam.finished.connect(self._on_reply)
        self._listeners = []

    def subscribe(self, fn):
        self._listeners.append(fn)

    def get(self, pid: int) -> Optional[QPixmap]:
        if not pid:
            return None
        if pid in self._pix:
            return self._pix[pid]
        f = HEADSHOT_DIR / f"{pid}.png"
        if f.exists():
            pm = QPixmap(str(f))
            if not pm.isNull():
                self._pix[pid] = pm
                return pm
        if pid not in self._pending:
            self._pending.add(pid)
            req = QNetworkRequest(QUrl(HEADSHOT_URL.format(pid=pid)))
            req.setAttribute(QNetworkRequest.Attribute.User, pid)
            self._nam.get(req)
        return None

    def _on_reply(self, reply: QNetworkReply):
        pid = reply.request().attribute(QNetworkRequest.Attribute.User)
        self._pending.discard(pid)
        data = reply.readAll()
        reply.deleteLater()
        pm = QPixmap()
        if pm.loadFromData(data) and not pm.isNull():
            self._pix[pid] = pm
            try:
                HEADSHOT_DIR.mkdir(exist_ok=True)
                pm.save(str(HEADSHOT_DIR / f"{pid}.png"), "PNG")
            except Exception:
                pass
            for fn in self._listeners:
                try:
                    fn()
                except Exception:
                    pass


_HEADSHOTS: Optional[HeadshotCache] = None


def headshots() -> HeadshotCache:
    global _HEADSHOTS
    if _HEADSHOTS is None:
        _HEADSHOTS = HeadshotCache()
    return _HEADSHOTS


def _portrait(pm: QPixmap, w: int) -> QPixmap:
    """The WHOLE headshot scaled into a 2:3 frame — nothing cropped.

    MLB headshots are 120x180. A square frame can only ever show two thirds
    of that, which cut every player off at the chest. Matching the frame to
    the source's own aspect renders the full portrait instead of a slice of
    it, so the only thing that changes with size is scale.
    """
    h = int(round(w * 1.5))
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    src = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
    p.drawPixmap(int((w - src.width()) / 2), int((h - src.height()) / 2), src)
    p.end()
    return out


class FieldView(QWidget):
    """Top-down park view: real wall geometry, fielders, runners, ball.

    The flight of a batted ball is drawn as a STRAIGHT radial line, because
    from above that is what it is — the arc is in the vertical plane and is
    not visible in plan. Height is carried by the ball marker swelling toward
    its apex and by a shrinking ground shadow, the same cue the HR widget's
    2D stadium view uses.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._profile = []
        self._venue_key = None
        self.venue = ""
        self.play: Optional[Play] = None
        self.ball_t = -1.0          # -1 = no ball in flight
        self.ball_angle = 0.0
        self.ball_dist = 0.0
        self.callout = ""
        self.callout_sub = ""
        # 0..1 through the action of the play; drives runners and the fielder
        # converging on the ball. -1 means the play is shown at rest.
        self.action_t = -1.0
        self.speeds: Dict[int, float] = {}
        self._circ: Dict[int, QPixmap] = {}
        headshots().subscribe(self.update)

    def set_venue(self, venue: str):
        if venue == self.venue:
            return
        self.venue = venue
        self._profile, self._venue_key = wall_profile(venue)
        self.update()

    def set_play(self, play: Optional[Play]):
        self.play = play
        self.ball_t = -1.0
        self.action_t = -1.0
        self.callout = self.callout_sub = ""
        self.update()

    # -- base paths -------------------------------------------------------
    def _base_point(self, i: int) -> QPointF:
        """Corner of the diamond by index: 0/4 home, 1/2/3 the bags."""
        if i <= 0 or i >= 4:
            return self._pt(0, 0)
        return self._pt(*BASE_SPOTS[("1B", "2B", "3B")[i - 1]])

    def _runner_point(self, frm: int, to: int, f: float) -> QPointF:
        """Where a runner is, f of the way from base `frm` to base `to`."""
        if to <= frm:
            return self._base_point(frm)
        pos = frm + max(0.0, min(1.0, f)) * (to - frm)
        leg = int(pos)
        frac = pos - leg
        a, b = self._base_point(leg), self._base_point(min(4, leg + 1))
        return QPointF(a.x() + (b.x() - a.x()) * frac,
                       a.y() + (b.y() - a.y()) * frac)

    def _runner_rates(self) -> Dict[int, float]:
        """Bases-per-unit-time for each runner, from HIS sprint speed.

        Normalised so the slowest man on the play arrives exactly as the
        action ends — the point is the relative order of arrivals, which is
        real, not the wall-clock duration, which is compressed anyway.
        """
        if not self.play:
            return {}
        rates = {}
        for m in self.play.runner_moves:
            legs = max(1, m["to"] - m["frm"])
            spd = self.speeds.get(m["id"]) or 27.0     # league average ft/s
            rates[m["id"]] = spd / (90.0 * legs)       # legs per second
        if rates:
            slowest = min(rates.values())
            rates = {k: v / slowest for k, v in rates.items()}
        return rates

    def start_flight(self, angle: float, dist: float):
        self.ball_angle, self.ball_dist, self.ball_t = angle, dist, 0.0

    # -- geometry ---------------------------------------------------------
    def _xform(self):
        prof = self._profile or [(0, 400)]
        max_y = max(d * math.cos(math.radians(a)) for a, d in prof)
        max_x = max(abs(d * math.sin(math.radians(a))) for a, d in prof)
        w, h = self.width(), self.height()
        # side/top leave room for the distance plates that sit OUTSIDE the wall
        top, bot, side = 26, 44, 26
        sc = min((h - top - bot) / max(max_y, 1.0),
                 (w / 2 - side) / max(max_x, 1.0))
        # In a narrow pane the fit is WIDTH-limited, so the park is shorter
        # than the band it lives in. Pinning home plate to the floor then
        # dumps all the slack above the outfield wall as dead sky — measured
        # at 502px in an 891px pane. Centre the drawn park in the band.
        slack = max(0.0, (h - top - bot) - max_y * sc)
        return w / 2.0, top + slack / 2.0 + max_y * sc, sc

    def _pt(self, a: float, d: float) -> QPointF:
        cx, cy, sc = self._x
        r = math.radians(a)
        return QPointF(cx + d * sc * math.sin(r), cy - d * sc * math.cos(r))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(GROUND))
        if not self._profile:
            return
        self._x = self._xform()
        cx, cy, sc = self._x

        # fair territory
        poly = QPolygonF([self._pt(a, d) for a, d in self._profile])
        poly.append(self._pt(0, 0))
        grad = QRadialGradient(QPointF(cx, cy), max(1.0, 420 * sc))
        grad.setColorAt(0.0, QColor(TURF_HI))
        grad.setColorAt(1.0, QColor("#111c17"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPolygon(poly)

        # NOTE: no mown-grass arcs, no warning-track band, no dirt shading.
        # They were decoration, and at this scale the concentric arcs read as
        # a wifi glyph sitting under the park. The polar outline IS the
        # drawing; everything else here is a real marking on a real field.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#4a5c6b"), 2.5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline(QPolygonF([self._pt(a, d) for a, d in self._profile]))

        # infield: base paths as chalk, nothing more
        b1, b2, b3 = (self._pt(*BASE_SPOTS[k]) for k in ("1B", "2B", "3B"))
        p.setPen(QPen(QColor(201, 214, 226, 97), 1.6))
        p.drawPolygon(QPolygonF([self._pt(0, 0), b1, b2, b3]))

        # foul lines out to the poles
        p.setPen(QPen(QColor(201, 214, 226, 80), 1.4))
        for a, d in (self._profile[0], self._profile[-1]):
            p.drawLine(self._pt(0, 0), self._pt(a, d))

        # mound and bases
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(201, 214, 226, 76), 1.2))
        p.drawEllipse(self._pt(0, 60.5), 5.5 * sc, 5.5 * sc)
        # Once the action has played out the bags hold the END state, or the
        # markers contradict the runner tiles standing on them.
        if not self.play:
            occupied = (False, False, False)
        elif self.action_t >= 1.0:
            occupied = self.play.bases_after
        else:
            occupied = self.play.bases_before
        p.setPen(QPen(QColor(RULE), 1))
        for i, k in enumerate(("1B", "2B", "3B")):
            pt = self._pt(*BASE_SPOTS[k])
            p.setBrush(QColor(LEV_C) if occupied[i] else QColor(232, 239, 245))
            p.save()
            p.translate(pt)
            p.rotate(45)
            s = 5.0
            p.drawRect(QRectF(-s, -s, 2 * s, 2 * s))
            p.restore()
        hp = self._pt(0, 0)
        p.setBrush(QColor(232, 239, 245))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([
            QPointF(hp.x() - 5, hp.y() - 5), QPointF(hp.x() + 5, hp.y() - 5),
            QPointF(hp.x() + 5, hp.y()), QPointF(hp.x(), hp.y() + 5),
            QPointF(hp.x() - 5, hp.y())]))

        # Wall distances, in plates OUTSIDE the outline. Drawn INSIDE the
        # wall they crossed it at both corners, where the foul line and the
        # fence converge and there is no clear ground to sit on.
        f = QFont("monospace", 7)
        p.setFont(f)
        fm = QFontMetrics(f)
        marks = [self._profile[0], (0.0, self._wall_at(0.0) or 0),
                 self._profile[-1]]
        for a, d in marks:
            if not d:
                continue
            wall = self._pt(a, d)
            r = math.radians(a)
            txt = f"{d:.0f}"
            bw = fm.horizontalAdvance(txt) + 10
            bh = 15
            # push the plate along the ray out of home plate, so it clears
            # the fence on whatever bearing that corner happens to sit
            box = QRectF(wall.x() + math.sin(r) * 15 - bw / 2,
                         wall.y() - math.cos(r) * 15 - bh / 2, bw, bh)
            if box.left() < 2:
                box.moveLeft(2)
            if box.right() > self.width() - 2:
                box.moveRight(self.width() - 2)
            if box.top() < 2:
                box.moveTop(2)
            p.setBrush(QColor(PANEL))
            p.setPen(QPen(QColor(RULE), 1))
            p.drawRect(box)
            p.setPen(QColor(DIM))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, txt)

        self._draw_fielders(p, sc)
        self._draw_runners(p, sc)
        self._draw_ball(p, sc)
        self._draw_callout(p)
        p.end()

    def _wall_at(self, angle: float) -> Optional[float]:
        best = None
        for a, d in self._profile:
            if best is None or abs(a - angle) < abs(best[0] - angle):
                best = (a, d)
        return best[1] if best else None

    @staticmethod
    def _surname(name: str) -> str:
        """Last name, skipping a generational suffix.

        `name.split()[-1]` turned Julio Rodriguez Jr. into "Jr." — several
        players a game land on this.
        """
        parts = [x for x in (name or "").split() if x]
        if not parts:
            return ""
        if len(parts) > 1 and parts[-1].rstrip(".").upper() in (
                "JR", "SR", "II", "III", "IV", "V"):
            return parts[-2]
        return parts[-1]

    def _plate(self, p: QPainter, cx: float, cy: float, text: str,
               fg: str = DIM, accent: bool = False, side: str = "below"):
        """A name on a filled plate.

        Bare text sat directly on the wall outline and the foul lines, which
        ran straight through the glyphs. The plate is opaque, so wherever a
        label lands it stays readable.
        """
        if not text:
            return
        f = QFont("monospace", 7)
        p.setFont(f)
        fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 8
        h = 12
        if side == "left":
            box = QRectF(cx - w, cy - h / 2, w, h)
        elif side == "right":
            box = QRectF(cx, cy - h / 2, w, h)
        else:
            box = QRectF(cx - w / 2, cy, w, h)
        p.setBrush(QColor(26, 32, 41, 232))
        p.setPen(QPen(QColor(LEV_C if accent else RULE), 1))
        p.drawRect(box)
        p.setPen(QColor(fg))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_fielders(self, p: QPainter, sc: float):
        if not self.play:
            return
        d = max(15, min(24, int(21 * min(1.0, sc / 1.05))))
        hgt = int(round(d * 1.5))
        f = QFont("monospace", 7)
        p.setFont(f)
        for pos, (pid, name) in sorted(self.play.defense.items()):
            spot = FIELDER_SPOTS.get(pos)
            if not spot:
                continue
            pt = self._pt(*spot)
            involved = pos in (self.play.credits or [])
            # The man credited FIRST is the one who got to the ball, so he
            # converges on where the ball actually went. Statcast does not
            # publish fielder tracks, but the start (his position) and the
            # end (the ball's own coordinates) are both real — only the route
            # between them is drawn as a straight line.
            if (involved and self.action_t >= 0.0 and self.play.credits
                    and pos == self.play.credits[0] and self.play.hit
                    and self.play.hit.angle is not None):
                # clamp to the fence: a ball off the wall reports a landing
                # point, and without this the fielder chases it out of the park
                bd = (self.play.hit.calc_distance
                      or self.play.hit.distance or 0)
                wall = self._wall_at(self.play.hit.angle)
                if wall:
                    bd = min(bd, wall - 6)
                tgt = self._pt(self.play.hit.angle, bd)
                f = min(1.0, self.action_t)
                pt = QPointF(pt.x() + (tgt.x() - pt.x()) * f,
                             pt.y() + (tgt.y() - pt.y()) * f)
            pm = self._circ.get(pid)
            if pm is None or pm.width() != d:
                raw = headshots().get(pid)
                if raw is not None:
                    pm = _portrait(raw, d)
                    self._circ[pid] = pm
            box = QRectF(pt.x() - d / 2 - 1.5, pt.y() - hgt / 2 - 1.5,
                         d + 3, hgt + 3)
            p.setPen(QPen(QColor(LEV_C if involved else INFO_C),
                          2.0 if involved else 1.4))
            p.setBrush(QColor(PANEL))
            p.drawRect(box)
            if pm is not None:
                p.drawPixmap(int(pt.x() - d / 2), int(pt.y() - hgt / 2), pm)
            else:
                # headshots arrive asynchronously; an empty ring says nothing,
                # so the position stands in until the face lands
                p.setPen(QColor(DIM))
                p.drawText(QRectF(pt.x() - d / 2, pt.y() - 6, d, 12),
                           Qt.AlignmentFlag.AlignCenter, pos)
            p.setBrush(Qt.BrushStyle.NoBrush)
            short = self._surname(name)
            # The corners wear their plate OUTWARD, beside the tile: below it
            # sits on the runner holding that bag, and above it collides with
            # SS/2B, who are only ~65ft away on a diagram this tight.
            lab = f"{pos} {short}".strip()
            col = INK if involved else DIM
            if pos == "3B":
                self._plate(p, pt.x() - d / 2 - 4, pt.y(), lab, col,
                            accent=involved, side="left")
            elif pos == "1B":
                self._plate(p, pt.x() + d / 2 + 4, pt.y(), lab, col,
                            accent=involved, side="right")
            else:
                self._plate(p, pt.x(), pt.y() + hgt / 2 + 3, lab, col,
                            accent=involved)

    def _draw_runners(self, p: QPainter, sc: float):
        """Baserunners as headshot tiles, on their bags or between them."""
        if not self.play:
            return
        moving = self.action_t >= 0.0
        rates = self._runner_rates() if moving else {}
        drawn = []
        movers = set()
        if moving:
            for m in self.play.runner_moves:
                movers.add(m["id"])
                f = min(1.0, self.action_t * rates.get(m["id"], 1.0))
                if m["out"] and f >= 1.0:
                    continue                     # retired: off the diamond
                if m["to"] >= 4 and f >= 1.0:
                    continue                     # scored: off the diamond
                pt = self._runner_point(m["frm"], m["to"], f)
                drawn.append((pt, m["id"], m["name"],
                              m["out"] and f >= 0.95))
        # Everyone who was ON a bag and did NOT move stays on it — including
        # while the play runs. Drawing only the movers made a runner who
        # holds his base disappear the moment the play was selected.
        for i, on in enumerate(self.play.bases_before):
            if not on:
                continue
            pid, nm = self.play.runners_on.get(i + 1, (0, ""))
            if pid and pid in movers:
                continue
            drawn.append((self._base_point(i + 1), pid, nm, False))
        # a runner tile is smaller than a fielder's so the bag stays visible
        w = max(12, min(18, int(16 * min(1.0, sc / 1.05))))
        hgt = int(round(w * 1.5))
        for pt, pid, name, out in drawn:
            pm = self._circ.get(("r", pid, w))
            if pm is None and pid:
                raw = headshots().get(pid)
                if raw is not None:
                    pm = _portrait(raw, w)
                    self._circ[("r", pid, w)] = pm
            box = QRectF(pt.x() - w / 2 - 1.5, pt.y() - hgt / 2 - 1.5,
                         w + 3, hgt + 3)
            p.setPen(QPen(QColor(OUT_C if out else LEV_C), 2.0))
            p.setBrush(QColor(PANEL))
            p.drawRect(box)
            if pm is not None:
                p.drawPixmap(int(pt.x() - w / 2), int(pt.y() - hgt / 2), pm)
            if name:
                self._plate(p, pt.x(), pt.y() + hgt / 2 + 3,
                            self._surname(name), OUT_C if out else LEV_C,
                            accent=not out)

    def _draw_ball(self, p: QPainter, sc: float):
        if self.ball_t < 0:
            return
        t = min(1.0, self.ball_t)
        end = self._pt(self.ball_angle, self.ball_dist)
        start = self._pt(0, 0)
        cur = QPointF(start.x() + (end.x() - start.x()) * t,
                      start.y() + (end.y() - start.y()) * t)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(LEV_C), 2.2, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(start, cur)
        lift = math.sin(t * math.pi)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, int(115 - lift * 75)))
        p.drawEllipse(QPointF(cur.x(), cur.y() + 3), 3 - lift * 1.6,
                      3 - lift * 1.6)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(cur, 3.4 + lift * 3.6, 3.4 + lift * 3.6)
        if t >= 1.0:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(LEV_C), 1.6))
            p.drawEllipse(end, 6, 6)

    def _draw_callout(self, p: QPainter):
        if not self.callout:
            return
        p.setPen(QColor(LEV_C))
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        p.setFont(f)
        # bottom-left: the top of the frame is the park, and a callout there
        # sat on top of the outfield wall
        h = self.height()
        p.drawText(QRectF(12, h - 44, self.width() - 24, 24),
                   Qt.AlignmentFlag.AlignLeft, self.callout)
        p.setPen(QColor(DIM))
        p.setFont(QFont("monospace", 7))
        p.drawText(QRectF(12, h - 20, self.width() - 24, 14),
                   Qt.AlignmentFlag.AlignLeft, self.callout_sub)


class ZoneView(QWidget):
    """Strike zone from the catcher's view with the at-bat's pitches.

    The zone box is the batter's OWN zone as measured on the last pitch of
    the at-bat — Statcast sets top/bottom per pitch from the hitter's stance,
    so a fixed rulebook rectangle would put pitches on the wrong side of the
    line for tall and short hitters alike.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.pitches: List[Pitch] = []
        self.shown = 0

    def set_pitches(self, pitches: List[Pitch], shown: int = 0):
        self.pitches, self.shown = pitches, shown
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(PANEL))
        w, h = self.width(), self.height()
        top = bot = None
        for pt in self.pitches:
            if pt.sz_top and pt.sz_bot:
                top, bot = pt.sz_top, pt.sz_bot
        top, bot = top or 3.4, bot or 1.6
        half = 0.83
        # A FIXED view, not a fit-to-pitches one. Fitting sounds right until
        # a curveball in the dirt drags the span so wide that the strike zone
        # shrinks to a stamp in the middle — which is what it did. Gameday
        # keeps the zone a constant size and lets the plot area hold the
        # misses; anything past the edge is clamped to it and drawn hollow.
        VX, LO_Z, HI_Z = 1.75, 0.05, 4.75
        span_x, span_z = VX * 2, HI_Z - LO_Z
        sc = min(w / span_x, h / span_z) * 0.94
        cx, cz = w / 2.0, h / 2.0
        mid = (LO_Z + HI_Z) / 2.0

        def px(x):
            return cx + x * sc

        def pz(z):
            return cz - (z - mid) * sc

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(FAINT), 1.4))
        p.drawRect(QRectF(px(-half), pz(top), 2 * half * sc,
                          (top - bot) * sc))
        p.setPen(QPen(QColor(RULE), 0.7))
        for i in (1, 2):
            x = -half + i * 2 * half / 3
            p.drawLine(QPointF(px(x), pz(top)), QPointF(px(x), pz(bot)))
            z = top - i * (top - bot) / 3
            p.drawLine(QPointF(px(-half), pz(z)), QPointF(px(half), pz(z)))

        # home plate in plan, under the zone — the depth cue Gameday uses
        py = pz(LO_Z) - 4
        plate = QPolygonF([QPointF(px(-half), py - 9), QPointF(px(half), py - 9),
                           QPointF(px(half), py - 4), QPointF(cx, py),
                           QPointF(px(-half), py - 4)])
        p.setPen(QPen(QColor(RULE), 1.0))
        p.drawPolygon(plate)

        f = QFont("monospace", 7)
        f.setBold(True)
        p.setFont(f)
        for i, pt in enumerate(self.pitches[:self.shown]):
            if pt.px is None or pt.pz is None:
                continue
            c = QColor(PITCH_COLORS.get(pt.ptype, "#95A5A6"))
            cxp, czp = pt.px, pt.pz
            outside = not (-VX < cxp < VX and LO_Z < czp < HI_Z)
            cxp = max(-VX + 0.13, min(VX - 0.13, cxp))
            czp = max(LO_Z + 0.13, min(HI_Z - 0.13, czp))
            centre = QPointF(px(cxp), pz(czp))
            r = 9.0 if pt.in_play else 8.0
            if outside:
                # clamped: hollow, so a pitch at the edge is never mistaken
                # for one that actually crossed there
                p.setBrush(QColor(PANEL))
                p.setPen(QPen(c, 2.0))
            else:
                p.setBrush(c)
                p.setPen(QPen(QColor(GROUND), 1.4))
            p.drawEllipse(centre, r, r)
            p.setPen(QColor(c if outside else QColor(GROUND)))
            p.drawText(QRectF(centre.x() - 9, centre.y() - 7, 18, 14),
                       Qt.AlignmentFlag.AlignCenter, str(i + 1))
        p.end()


class ScoreStrip(QWidget):
    """Linescore, score, count, outs and the base diamond."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(62)
        self.game: Optional[ReplayGame] = None
        self.play: Optional[Play] = None
        self.count = (0, 0)

    def set_game(self, g: Optional[ReplayGame]):
        self.game = g
        self.update()

    def set_play(self, play: Optional[Play], count=(0, 0)):
        self.play, self.count = play, count
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor("#1b2330"))
        g.setColorAt(1, QColor(GROUND))
        p.fillRect(self.rect(), QBrush(g))
        p.setPen(QPen(QColor(RULE), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        if not self.game:
            return
        pl = self.play
        a_sc = pl.away_score if pl else 0
        h_sc = pl.home_score if pl else 0
        batting_top = pl.is_top if pl else True

        # club + runs
        f = QFont()
        f.setBold(True)
        f.setPointSize(13)
        for i, (ab, sc, is_bat) in enumerate((
                (self.game.away, a_sc, batting_top),
                (self.game.home, h_sc, not batting_top))):
            y = 6 + i * 26
            p.setFont(f)
            p.setPen(QColor(LEV_C if is_bat else INK))
            p.drawText(QRectF(12, y, 70, 22),
                       Qt.AlignmentFlag.AlignVCenter, ab)
            p.setPen(QColor(LEV_C if is_bat else INK))
            p.drawText(QRectF(80, y, 34, 22),
                       Qt.AlignmentFlag.AlignRight |
                       Qt.AlignmentFlag.AlignVCenter, str(sc))

        # linescore
        mf = QFont("monospace", 7)
        p.setFont(mf)
        innings = self.game.innings or []
        cw = 20
        # centred in the space between the club block and the count readout,
        # so a 9-inning game and a 15-inning one both sit where the eye looks
        span = (len(innings) + 3) * cw + 8
        x0 = max(140, 126 + ((self.width() - 200) - 126 - span) / 2)
        cur = (pl.inning if pl else 1)
        p.setPen(QColor(FAINT))
        for j, inn in enumerate(innings):
            p.drawText(QRectF(x0 + j * cw, 3, cw, 12),
                       Qt.AlignmentFlag.AlignCenter, str(inn["num"]))
        for i, side in enumerate(("away", "home")):
            y = 17 + i * 15
            for j, inn in enumerate(innings):
                n = inn["num"]
                played = n < cur or (n == cur and
                                     (side == "away" or not batting_top))
                v = inn[side]
                txt = "" if (not played or v is None) else str(v)
                if n == cur and ((side == "away") == batting_top):
                    p.fillRect(QRectF(x0 + j * cw, y - 2, cw, 14),
                               QColor(PANEL_HI))
                p.setPen(QColor(INK if n == cur else DIM))
                p.drawText(QRectF(x0 + j * cw, y, cw, 12),
                           Qt.AlignmentFlag.AlignCenter, txt)
        # R H E
        xr = x0 + len(innings) * cw + 8
        p.setPen(QColor(FAINT))
        for k, lab in enumerate("RHE"):
            p.drawText(QRectF(xr + k * cw, 3, cw, 12),
                       Qt.AlignmentFlag.AlignCenter, lab)
        for i, side in enumerate(("away", "home")):
            y = 17 + i * 15
            t = self.game.totals.get(side, {})
            vals = (a_sc if side == "away" else h_sc, t.get("h", 0),
                    t.get("e", 0))
            p.setPen(QColor(INK))
            for k, v in enumerate(vals):
                p.drawText(QRectF(xr + k * cw, y, cw, 12),
                           Qt.AlignmentFlag.AlignCenter, str(v))

        # count / outs / diamond
        rx = self.width() - 176
        p.setPen(QColor(FAINT))
        p.setFont(QFont("monospace", 6))
        p.drawText(QRectF(rx, 8, 60, 10), Qt.AlignmentFlag.AlignLeft, "COUNT")
        p.drawText(QRectF(rx + 66, 8, 40, 10), Qt.AlignmentFlag.AlignLeft, "OUTS")
        bf = QFont("monospace", 11)
        bf.setBold(True)
        p.setFont(bf)
        p.setPen(QColor(INK))
        p.drawText(QRectF(rx, 22, 60, 20), Qt.AlignmentFlag.AlignLeft,
                   f"{self.count[0]}-{self.count[1]}")
        outs = pl.outs_before if pl else 0
        for i in range(3):
            c = QColor(OUT_C) if i < outs else QColor(PANEL)
            p.setBrush(c)
            p.setPen(QPen(QColor(OUT_C if i < outs else FAINT), 1))
            p.drawEllipse(QPointF(rx + 72 + i * 12, 30), 4.5, 4.5)
        occ = pl.bases_before if pl else (False, False, False)
        cx, cy, s = rx + 140, 30, 9
        for i, (dx, dy) in enumerate(((s, 0), (0, -s), (-s, 0))):
            p.setBrush(QColor(LEV_C) if occ[i] else QColor(PANEL))
            p.setPen(QPen(QColor(LEV_C if occ[i] else FAINT), 1.2))
            p.save()
            p.translate(cx + dx, cy + dy)
            p.rotate(45)
            p.drawRect(QRectF(-4, -4, 8, 8))
            p.restore()
        p.end()


class PlayLogDelegate(QStyledItemDelegate):
    """Two-line play entry: event headline over its description."""

    def sizeHint(self, opt, idx) -> QSize:
        return QSize(200, 54)

    def paint(self, p, opt, idx):
        play: Play = idx.data(Qt.ItemDataRole.UserRole)
        if play is None:
            return
        r = opt.rect
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(r, QColor(PANEL_HI) if sel else QColor(PANEL))
        if sel:
            p.fillRect(QRectF(r.left(), r.top(), 2, r.height()), QColor(LEV_C))
        p.setPen(QPen(QColor(RULE_DIM), 1))
        p.drawLine(r.left(), r.bottom(), r.right(), r.bottom())

        p.setFont(QFont("monospace", 7))
        p.setPen(QColor(FAINT))
        p.drawText(QRectF(r.left() + 8, r.top() + 6, 26, 12),
                   Qt.AlignmentFlag.AlignLeft,
                   f"{'▲' if play.is_top else '▼'}{play.inning}")

        f = QFont()
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(LEV_C if play.scoring else INK))
        p.drawText(QRectF(r.left() + 38, r.top() + 5, r.width() - 46, 14),
                   Qt.AlignmentFlag.AlignLeft, play.event or play.event_type)

        f2 = QFont()
        f2.setPointSize(8)
        p.setFont(f2)
        p.setPen(QColor(DIM))
        p.drawText(QRectF(r.left() + 38, r.top() + 19, r.width() - 46, 30),
                   int(Qt.AlignmentFlag.AlignLeft) |
                   int(Qt.TextFlag.TextWordWrap), play.desc)
        p.restore()


def _abbrev(name: str) -> str:
    """Last-ditch club abbreviation from the NAME.

    Only used when the roster's id->abbr map is unavailable, and it is a poor
    substitute: the last word's first three letters gives BRA/YAN/NAT where
    the real abbreviations are ATL/NYY/WSH. Prefer the team id.
    """
    parts = name.split()
    return (parts[-1][:3] if parts else name[:3]).upper()


def _card(title: str) -> Tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setStyleSheet(f"QFrame{{background:{PANEL};border:0;"
                    f"border-bottom:1px solid {RULE_DIM};}}")
    v = QVBoxLayout(f)
    v.setContentsMargins(12, 9, 12, 9)
    v.setSpacing(7)
    lab = QLabel(title)
    lab.setStyleSheet(f"color:{FAINT};font-family:monospace;font-size:9px;"
                      "font-weight:600;letter-spacing:2px;border:0;")
    v.addWidget(lab)
    return f, v


class _Stat(QWidget):
    """Three-up value/label block used under the rail cards."""

    def __init__(self, labels: List[str]):
        super().__init__()
        self.setStyleSheet("border:0;")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(1)
        self.vals = []
        for lb in labels:
            box = QWidget()
            box.setStyleSheet(f"background:{PANEL_HI};")
            v = QVBoxLayout(box)
            v.setContentsMargins(6, 4, 6, 4)
            v.setSpacing(1)
            val = QLabel("—")
            val.setStyleSheet(f"color:{INK};font-family:monospace;"
                              "font-size:13px;font-weight:600;border:0;")
            cap = QLabel(lb)
            cap.setStyleSheet(f"color:{FAINT};font-family:monospace;"
                              "font-size:9px;letter-spacing:1px;border:0;")
            v.addWidget(val)
            v.addWidget(cap)
            h.addWidget(box)
            self.vals.append(val)

    def set(self, *values):
        for lab, v in zip(self.vals, values):
            lab.setText("—" if v is None else str(v))

    def tint(self, colour: str):
        for lab in self.vals:
            lab.setStyleSheet(f"color:{colour};font-family:monospace;"
                              "font-size:13px;font-weight:600;border:0;")



# schedule whitelist for the game browser: one request covers a whole season
SEASON_BROWSE_FIELDS = ",".join((
    "dates", "date", "games", "gamePk", "gameDate",
    "status", "abstractGameState", "detailedState",
    "teams", "home", "away", "team", "id", "name", "score", "isWinner",
    "venue", "gameNumber", "doubleHeader",
))


class WERibbon(QWidget):
    """Win expectancy across the game, with leverage shading, as a scrubber.

    This is the thing a replay has that a box score does not: every play
    carries the swing it actually produced, priced off the season's own
    win-expectancy grid rather than an imported table.
    """

    scrub = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(86)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.game: Optional[ReplayGame] = None
        self.idx = 0
        self.has_we = False

    def set_game(self, g: Optional[ReplayGame], has_we: bool):
        self.game, self.has_we = g, has_we
        self.update()

    def set_index(self, i: int):
        self.idx = i
        self.update()

    def _plot(self):
        return QRectF(44, 16, max(1, self.width() - 58), self.height() - 34)

    def mousePressEvent(self, e):
        if not self.game:
            return
        r = self._plot()
        n = len(self.game.plays)
        f = (e.position().x() - r.left()) / max(1.0, r.width())
        self.scrub.emit(max(0, min(n - 1, int(round(f * (n - 1))))))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(GROUND))
        if not self.game:
            return
        r = self._plot()
        plays = self.game.plays
        n = len(plays)
        p.setFont(QFont("monospace", 6))

        if not self.has_we:
            p.setPen(QColor(FAINT))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "win expectancy unavailable — open Managers once to "
                       "build the season tables")
            return

        def X(i):
            return r.left() + r.width() * (i / max(1, n - 1))

        def Y(v):
            return r.bottom() - r.height() * v

        # gridlines at 0 / 50 / 100
        for v, lab in ((1.0, "100%"), (0.5, "50%"), (0.0, "0%")):
            y = Y(v)
            p.setPen(QPen(QColor(RULE_DIM), 1,
                          Qt.PenStyle.DashLine if v == 0.5 else Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(FAINT))
            p.drawText(QRectF(2, y - 7, 38, 14),
                       Qt.AlignmentFlag.AlignRight |
                       Qt.AlignmentFlag.AlignVCenter, lab)

        # leverage as shading under the curve — where the game was decided
        for i, pl in enumerate(plays):
            if not pl.li or pl.li < 1.3:
                continue
            a = min(46, int((pl.li - 1.3) * 26))
            w = max(1.5, r.width() / n)
            p.fillRect(QRectF(X(i) - w / 2, r.top(), w, r.height()),
                       QColor(244, 208, 63, a))

        pts = [(i, pl.we) for i, pl in enumerate(plays) if pl.we is not None]
        if len(pts) > 1:
            poly = QPolygonF([QPointF(X(i), Y(v)) for i, v in pts])
            area = QPolygonF(poly)
            area.append(QPointF(X(pts[-1][0]), Y(0.5)))
            area.append(QPointF(X(pts[0][0]), Y(0.5)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(52, 152, 219, 46))
            p.drawPolygon(area)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(INFO_C), 1.7))
            p.drawPolyline(poly)

        # inning ticks
        p.setPen(QColor(FAINT))
        last = None
        for i, pl in enumerate(plays):
            if pl.inning != last and pl.is_top:
                last = pl.inning
                p.setPen(QPen(QColor(RULE_DIM), 1))
                p.drawLine(QPointF(X(i), r.top()), QPointF(X(i), r.bottom()))
                p.setPen(QColor(FAINT))
                p.drawText(QRectF(X(i) - 9, r.bottom() + 2, 18, 12),
                           Qt.AlignmentFlag.AlignCenter, str(pl.inning))

        # playhead + its readout
        cur = plays[self.idx]
        x = X(self.idx)
        p.setPen(QPen(QColor(LEV_C), 1.6))
        p.drawLine(QPointF(x, r.top() - 4), QPointF(x, r.bottom() + 4))
        if cur.we is not None:
            p.setBrush(QColor(LEV_C))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, Y(cur.we)), 3.4, 3.4)
            bits = [f"{self.game.home} {cur.we * 100:.0f}%"]
            if cur.we_delta is not None:
                bits.append(f"{cur.we_delta * 100:+.1f}")
            if cur.li:
                bits.append(f"LI {cur.li:.2f}")
            p.setPen(QColor(LEV_C))
            p.setFont(QFont("monospace", 7))
            p.drawText(QRectF(r.left(), 1, r.width(), 13),
                       Qt.AlignmentFlag.AlignRight, "   ".join(bits))
        p.end()


class GameBrowser(QWidget):
    """Search and load any game of the season."""

    chosen = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 6, 10, 8)
        v.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("search team or date…")
        self.search.setStyleSheet(
            f"QLineEdit{{background:{GROUND};color:{INK};border:1px solid {RULE};"
            f"padding:4px 7px;font-family:monospace;font-size:11px;}}")
        self.search.textChanged.connect(self._filter)
        v.addWidget(self.search)
        self.list = QListWidget()
        self.list.setStyleSheet(
            f"QListWidget{{background:{GROUND};color:{INK};border:1px solid {RULE};"
            f"font-family:monospace;font-size:11px;}}"
            f"QListWidget::item{{padding:3px 6px;}}"
            f"QListWidget::item:selected{{background:{PANEL_HI};color:{LEV_C};}}"
            f"QScrollBar:vertical{{background:{GROUND};width:9px;border:0;}}"
            f"QScrollBar::handle:vertical{{background:{RULE};min-height:24px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}")
        self.list.itemClicked.connect(self._pick)
        v.addWidget(self.list, 1)
        self._rows: List[dict] = []

    def set_games(self, rows: List[dict]):
        self._rows = rows
        self._filter(self.search.text())

    def _filter(self, txt: str):
        t = (txt or "").strip().lower()
        self.list.clear()
        shown = 0
        for g in self._rows:
            if t and t not in g["hay"]:
                continue
            it = QListWidgetItem(g["label"])
            it.setData(Qt.ItemDataRole.UserRole, g["pk"])
            self.list.addItem(it)
            shown += 1
            if shown >= 400:            # a season is 2,400+; keep it snappy
                break

    def _pick(self, item: QListWidgetItem):
        self.chosen.emit(int(item.data(Qt.ItemDataRole.UserRole)))


class TopBand(QWidget):
    """The strip under the scoreboard: WE ribbon, expandable to the browser."""

    COLLAPSED = 112
    EXPANDED = 232

    def __init__(self):
        super().__init__()
        self.setFixedHeight(self.COLLAPSED)
        self.setStyleSheet(f"background:{GROUND};")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QWidget()
        head.setFixedHeight(24)
        head.setStyleSheet(f"background:{PANEL};border-bottom:1px solid {RULE_DIM};")
        h = QHBoxLayout(head)
        h.setContentsMargins(12, 0, 8, 0)
        self.title = QLabel("")
        self.title.setStyleSheet(f"color:{DIM};font-family:monospace;font-size:10px;"
                                 f"letter-spacing:1px;border:0;")
        h.addWidget(self.title, 1)
        self.toggle = QPushButton("⌄ GAMES")
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet(
            f"QPushButton{{background:transparent;color:{DIM};border:0;"
            f"font-family:monospace;font-size:10px;letter-spacing:1px;}}"
            f"QPushButton:hover{{color:{INFO_C};}}")
        self.toggle.clicked.connect(self._toggle)
        h.addWidget(self.toggle)
        v.addWidget(head)
        self.stack = QStackedWidget()
        self.ribbon = WERibbon()
        self.browser = GameBrowser()
        self.stack.addWidget(self.ribbon)
        self.stack.addWidget(self.browser)
        v.addWidget(self.stack, 1)

    def _toggle(self):
        opening = self.stack.currentIndex() == 0
        self.stack.setCurrentIndex(1 if opening else 0)
        self.setFixedHeight(self.EXPANDED if opening else self.COLLAPSED)
        self.toggle.setText("⌃ CLOSE" if opening else "⌄ GAMES")

    def show_ribbon(self):
        if self.stack.currentIndex() != 0:
            self._toggle()


class LineupPanel(QWidget):
    """Both starting lineups for the game being replayed.

    A historical replay has no live lineup rail to lean on, and the boxscore's
    top-level batting order is the END-of-game one, so this uses the same
    starters walk the defensive alignment does: slot from `battingOrder`
    ending in "00", position from `allPositions[0]`.
    """

    def __init__(self):
        super().__init__()
        self.setFixedHeight(158)
        self.game: Optional[ReplayGame] = None
        self.play: Optional[Play] = None

    def set_game(self, g):
        self.game = g
        self.update()

    def set_play(self, play):
        self.play = play
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(GROUND))
        p.setPen(QPen(QColor(RULE), 1))
        p.drawLine(0, 0, self.width(), 0)
        if not self.game:
            return
        w = self.width()
        colw = w / 2.0
        active = None
        if self.play:
            active = self.play.batter_id
        for c, side in enumerate(("away", "home")):
            x = c * colw + 14
            abbr = self.game.away if side == "away" else self.game.home
            f = QFont("monospace", 8)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(INK))
            p.drawText(QRectF(x, 7, colw - 20, 13),
                       Qt.AlignmentFlag.AlignLeft, f"{abbr} LINEUP")
            p.setPen(QPen(QColor(RULE_DIM), 1))
            p.drawLine(QPointF(x, 23), QPointF(x + colw - 28, 23))
            fm = QFont("monospace", 8)
            for i, row in enumerate(self.game.lineups.get(side, [])[:9]):
                y = 28 + i * 14
                on = row["id"] == active
                p.setFont(fm)
                p.setPen(QColor(FAINT))
                p.drawText(QRectF(x, y, 14, 13),
                           Qt.AlignmentFlag.AlignLeft, str(row["order"]))
                p.setPen(QColor(INFO_C if not on else LEV_C))
                p.drawText(QRectF(x + 16, y, 26, 13),
                           Qt.AlignmentFlag.AlignLeft, row["pos"] or "")
                p.setPen(QColor(LEV_C if on else INK))
                nm = row["name"]
                if len(nm) > 20:
                    parts = nm.split()
                    nm = (parts[0][0] + ". " + " ".join(parts[1:])) if len(parts) > 1 else nm
                p.drawText(QRectF(x + 46, y, colw - 74, 13),
                           Qt.AlignmentFlag.AlignLeft, nm)
        p.setPen(QPen(QColor(RULE_DIM), 1))
        p.drawLine(QPointF(colw, 6), QPointF(colw, self.height() - 6))
        p.end()


class _MatchupRow(QWidget):
    """One line of the matchup strip: face, name, hand, inline stats.

    Replaces the tall two-card rail. Those cards spent ~330px of column on
    two headshots and six stat tiles; at 40px a row this says the same thing
    and hands the difference to the park.
    """

    def __init__(self, tag: str):
        super().__init__()
        self.setFixedHeight(50)
        self.setStyleSheet("border:0;")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(9)
        self.tag = tag
        self.head = QLabel()
        self.head.setFixedSize(29, 44)
        self.head.setStyleSheet(f"background:{PANEL_HI};border:1px solid {RULE};")
        h.addWidget(self.head)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.name = QLabel("—")
        self.name.setStyleSheet(f"color:{INK};font-size:12px;font-weight:600;"
                                f"border:0;")
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color:{FAINT};font-family:monospace;"
                               f"font-size:9px;letter-spacing:1px;border:0;")
        col.addStretch(1)
        col.addWidget(self.name)
        col.addWidget(self.sub)
        col.addStretch(1)
        h.addLayout(col, 1)
        self.stats = QLabel("")
        self.stats.setAlignment(Qt.AlignmentFlag.AlignRight |
                                Qt.AlignmentFlag.AlignVCenter)
        self.stats.setStyleSheet(f"color:{DIM};font-family:monospace;"
                                 f"font-size:11px;border:0;")
        h.addWidget(self.stats)
        self._pid = 0

    def set(self, pid: int, name: str, sub: str, stats: str):
        self._pid = pid
        self.name.setText(name or "—")
        self.sub.setText(f"{self.tag} · {sub}" if sub else self.tag)
        self.stats.setText(stats)
        self.refresh_head()

    def refresh_head(self):
        pm = headshots().get(self._pid)
        self.head.setPixmap(_portrait(pm, 29) if pm else QPixmap())


class ReplayTab(QWidget):
    """The Replay tab: scoreboard, park view, play log and transport.

    One QTimer drives everything. A frame advances a counter and asks the
    views to repaint from state that build_replay already computed — no
    trajectory maths happens on the loop thread, which is also the UI thread
    under qasync.
    """

    TICK_MS = 33
    PITCH_TICKS = 17          # ~0.56s between pitches at 1x
    FLIGHT_TICKS = 42
    HOLD_TICKS = 24
    GAP_TICKS = 20

    def __init__(self, stats=None, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._feed = ReplayFeed()
        self.game: Optional[ReplayGame] = None
        self._idx = 0
        self._shown = 0
        self._phase = "idle"
        self._t = 0
        self._speed = 1
        self._loaded_pk = None
        self._season_rows: List[dict] = []
        self._has_we = False
        # live polling: only while the game is in progress AND the tab is on
        # screen, so a background tab never sits refetching a 0.24MB feed
        self._live = False
        self._follow = True
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(15000)
        self._live_timer.timeout.connect(self._poll_live)

        self.setStyleSheet(f"QWidget{{background:{GROUND};color:{INK};}}")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.score = ScoreStrip()
        root.addWidget(self.score)

        self.band = TopBand()
        self.band.ribbon.scrub.connect(self._on_scrub)
        self.band.browser.chosen.connect(self._on_browse_pick)
        root.addWidget(self.band)

        # A plain layout, NOT a QSplitter. The splitter renegotiated both
        # panes every time the parent tab area was dragged, so the park would
        # jump or the side column collapse mid-resize. A fixed side column
        # and an expanding park resize predictably at any window size.
        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        self.field = FieldView()
        ll.addWidget(self.field, 1)
        # The park is WIDTH-limited in any realistic pane, so the vertical
        # slack under it is free real estate — the lineups cost the field
        # nothing.
        self.lineups = LineupPanel()
        ll.addWidget(self.lineups)
        bl.addWidget(left, 1)

        side = QWidget()
        side.setStyleSheet(f"background:{PANEL};")
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)

        # --- compact matchup strip: two rows instead of two tall cards
        mw = QWidget()
        mw.setStyleSheet(f"background:{PANEL};border-bottom:1px solid {RULE_DIM};")
        mv = QVBoxLayout(mw)
        mv.setContentsMargins(10, 8, 10, 8)
        mv.setSpacing(7)
        self.p_row = _MatchupRow("P")
        self.b_row = _MatchupRow("AB")
        mv.addWidget(self.p_row)
        mv.addWidget(self.b_row)
        sv.addWidget(mw)

        zc, zv = _card("ZONE · CATCHER'S VIEW")
        self.zone = ZoneView()
        self.zone.setMinimumHeight(212)
        zv.addWidget(self.zone)
        self.seq = QLabel("")
        self.seq.setWordWrap(True)
        self.seq.setStyleSheet(f"color:{DIM};font-family:monospace;"
                               f"font-size:9px;border:0;")
        zv.addWidget(self.seq)
        sv.addWidget(zc)

        hc, hv = _card("BATTED BALL")
        self.hit_stat = _Stat(["EV MPH", "LA °", "DIST FT"])
        hv.addWidget(self.hit_stat)
        self.hit_note = QLabel("awaiting contact")
        self.hit_note.setStyleSheet(f"color:{FAINT};font-family:monospace;"
                                    f"font-size:9px;letter-spacing:1px;border:0;")
        hv.addWidget(self.hit_note)
        sv.addWidget(hc)

        lh = QLabel("PLAY BY PLAY")
        lh.setStyleSheet(f"color:{FAINT};font-family:monospace;font-size:9px;"
                         f"font-weight:600;letter-spacing:2px;padding:7px 12px;"
                         f"background:{PANEL};border-bottom:1px solid {RULE_DIM};")
        sv.addWidget(lh)

        self.log = QListWidget()
        self.log.setItemDelegate(PlayLogDelegate())
        self.log.setStyleSheet(
            f"QListWidget{{background:{PANEL};border:0;}}"
            f"QListWidget::item{{border:0;}}"
            f"QScrollBar:vertical{{background:{PANEL};width:9px;border:0;}}"
            f"QScrollBar::handle:vertical{{background:{RULE};min-height:24px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{"
            f"height:0;}}"
            f"QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{"
            f"background:{PANEL};}}")
        self.log.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.log.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.log.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.log.currentRowChanged.connect(self._on_log_row)
        sv.addWidget(self.log, 1)

        side.setFixedWidth(300)
        side.setStyleSheet(f"background:{PANEL};border-left:1px solid {RULE};")
        bl.addWidget(side)
        self._body = body
        root.addWidget(body, 1)

        bar = QWidget()
        bar.setFixedHeight(42)
        bar.setStyleSheet(f"background:{PANEL};border-top:1px solid {RULE};")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(12, 6, 12, 6)
        bh.setSpacing(6)
        btn_css = (f"QPushButton{{background:{PANEL_HI};color:{INK};"
                   f"border:1px solid {RULE};font-family:monospace;"
                   f"font-size:12px;padding:5px 10px;}}"
                   f"QPushButton:hover{{border-color:{INFO_C};}}")
        self.b_prev = QPushButton("⏮")
        self.b_play = QPushButton("▶ Play")
        self.b_next = QPushButton("⏭")
        self.b_spd = QPushButton("1×")
        for bt in (self.b_prev, self.b_play, self.b_next, self.b_spd):
            bt.setStyleSheet(btn_css)
            bt.setCursor(Qt.CursorShape.PointingHandCursor)
            bh.addWidget(bt)
        self.b_play.setMinimumWidth(76)
        self.b_prev.clicked.connect(lambda: self.goto(self._idx - 1))
        self.b_next.clicked.connect(lambda: self.goto(self._idx + 1))
        self.b_play.clicked.connect(self.toggle)
        self.b_spd.clicked.connect(self._cycle_speed)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{DIM};font-family:monospace;"
                                  f"font-size:10px;border:0;")
        bh.addSpacing(10)
        bh.addWidget(self.status, 1)
        root.addWidget(bar)

        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick_fn)
        headshots().subscribe(self._refresh_heads)

    # ------------------------------------------------------------- loading

    async def load_game(self, session, game_pk: int):
        if game_pk == self._loaded_pk:
            return
        self.status.setText(f"Loading {game_pk}…")
        feed = await self._feed.get(session, game_pk)
        if not feed:
            self.status.setText("Feed unavailable")
            return
        game = build_replay(feed)
        if not game:
            self.status.setText("No plays in feed")
            return
        self._loaded_pk = game_pk
        self.set_game(game)
        if self._stats is not None and not self.field.speeds:
            try:
                board = await self._stats.get_sprint_speed(session)
                self.field.speeds = {pid: v["speed"] for pid, v in board.items()
                                     if v.get("speed")}
                self.field.update()
            except Exception as e:
                print(f"Replay: sprint speeds unavailable ({e})")

    async def load_season(self, session, season: int):
        """One request covers the whole season's browsable game list."""
        if self._season_rows:
            return
        try:
            async with session.get(
                    "https://statsapi.mlb.com/api/v1/schedule",
                    params={"sportId": "1", "season": str(season),
                            "gameType": "R", "fields": SEASON_BROWSE_FIELDS},
                    timeout=aiohttp.ClientTimeout(total=45)) as resp:
                if resp.status != 200:
                    return
                data = json_loads(await resp.read())
        except Exception as e:
            print(f"Replay: season index failed: {e}")
            return
        # the roster walk's id -> abbreviation map is the authoritative one
        tmap = dict(getattr(self._stats, "_teams", {}) or {})
        rows = []
        for d in (data or {}).get("dates") or []:
            date = d.get("date") or ""
            for g in d.get("games") or []:
                st = (g.get("status") or {}).get("abstractGameState")
                if st != "Final":
                    continue
                try:
                    a = g["teams"]["away"]
                    h = g["teams"]["home"]
                    an, hn = a["team"]["name"], h["team"]["name"]
                    aa = tmap.get(a["team"].get("id")) or _abbrev(an)
                    ha = tmap.get(h["team"].get("id")) or _abbrev(hn)
                    label = (f"{date}  {aa:>3} {a.get('score', 0):>2}"
                             f" @ {ha:>3} {h.get('score', 0):<2}"
                             f"  {(g.get('venue') or {}).get('name', '')}")
                    rows.append({"pk": g["gamePk"], "label": label,
                                 "hay": f"{date} {an} {hn} {aa} {ha}".lower()})
                except KeyError:
                    continue
        rows.reverse()                    # most recent first
        self._season_rows = rows
        self.band.browser.set_games(rows)

    def _on_scrub(self, i: int):
        self.stop()
        self.goto(i)

    # -------------------------------------------------------------- live

    def showEvent(self, e):
        super().showEvent(e)
        if self._live and not self._live_timer.isActive():
            self._live_timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._live_timer.stop()

    def _poll_live(self):
        if not (self._live and self._stats and self._loaded_pk):
            return

        async def go():
            try:
                async with self._stats.http() as session:
                    feed = await self._feed.get(session, self._loaded_pk,
                                                refresh=True)
            except Exception as e:
                print(f"Replay: live poll failed: {e}")
                return
            if not feed:
                return
            game = build_replay(feed)
            if game:
                self._apply_live(game)
        asyncio.create_task(go())

    def _apply_live(self, game: ReplayGame):
        """Fold a refreshed feed in without stealing the user's place.

        Following means sitting on the newest play; scrub back and the tab
        stops following, so an arriving pitch does not yank the view away
        from whatever you were watching.
        """
        old = self.game
        was_last = self._idx >= len(old.plays) - 1 if old else True
        self._follow = self._follow and was_last
        keep = self._idx
        added = len(game.plays) - (len(old.plays) if old else 0)
        if old and added >= 0 and old.game_pk == game.game_pk:
            # Incremental: only the tail moves. Clearing and re-adding every
            # row cost ~45ms of UI thread per poll and threw away the
            # scroll position with it.
            self._patch_log(game, len(old.plays))
        else:
            self.set_game(game, keep_place=None if self._follow else keep)
            return
        self.game = game
        self._has_we = annotate_win_expectancy(game, self._stats)
        self.band.ribbon.set_game(game, self._has_we)
        self.lineups.set_game(game)
        self.score.set_game(game)
        self.goto(len(game.plays) - 1 if self._follow
                  else min(keep, len(game.plays) - 1))
        if not game.final:
            self.status.setText(
                f"● LIVE  {game.away} @ {game.home} · {game.venue}"
                + (f"  (+{added} play{'s' if added != 1 else ''})"
                   if added > 0 else ""))

    def _on_browse_pick(self, pk: int):
        if self._stats is None or pk == self._loaded_pk:
            return
        self.band.show_ribbon()
        self.status.setText(f"Loading {pk}…")

        async def go():
            try:
                async with self._stats.http() as session:
                    await self.load_game(session, pk)
            except Exception as e:
                self.status.setText(f"Load failed: {e}")
        asyncio.create_task(go())

    def _patch_log(self, game: ReplayGame, old_n: int):
        """Refresh the tail of the log rather than rebuilding it.

        The previously-last row is re-pointed too: an at-bat that read
        "AT BAT — Simpson batting, 0-1" last poll is a strikeout now.
        """
        self.log.blockSignals(True)
        if old_n and self.log.count() >= old_n:
            self.log.item(old_n - 1).setData(
                Qt.ItemDataRole.UserRole, game.plays[old_n - 1])
        for pl in game.plays[old_n:]:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, pl)
            self.log.addItem(it)
        self.log.blockSignals(False)
        self.log.viewport().update()

    def set_game(self, game: ReplayGame, keep_place: Optional[int] = None):
        self.stop()
        self.game = game
        self.field.set_venue(game.venue)
        self.score.set_game(game)
        self.lineups.set_game(game)
        self.log.blockSignals(True)
        self.log.clear()
        for pl in game.plays:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, pl)
            self.log.addItem(it)
        self.log.blockSignals(False)
        self._has_we = annotate_win_expectancy(game, self._stats)
        self.band.ribbon.set_game(game, self._has_we)
        self.band.title.setText(
            f"{game.date}   {game.away} @ {game.home}   ·   {game.venue}")
        self._live = not game.final
        if self._live and self.isVisible() and not self._live_timer.isActive():
            self._live_timer.start()
        elif not self._live:
            self._live_timer.stop()
        key = resolve_venue(game.venue)
        self.status.setText(
            ("● LIVE  " if self._live else "")
            + f"{game.away} @ {game.home} · {game.date} · {game.venue}"
            + ("" if key else "  (no wall data — generic outline)"))
        if keep_place is not None:
            self.goto(min(keep_place, len(game.plays) - 1))
        elif self._live:
            self.goto(len(game.plays) - 1)      # a live game opens at NOW
        else:
            self.goto(0)

    # ------------------------------------------------------------ playback

    def goto(self, i: int, animate: bool = False):
        if not self.game:
            return
        self._idx = max(0, min(len(self.game.plays) - 1, i))
        # leaving the newest play means the user wants to look at something;
        # returning to it resumes following
        self._follow = self._idx >= len(self.game.plays) - 1
        play = self.game.plays[self._idx]
        self._shown = 0 if animate else len(play.pitches)
        self._phase = "pitches" if animate else "idle"
        self._t = 0
        self.field.set_play(play)
        if not animate:
            self.field.action_t = 1.0     # settled: runners on their bags
        self.lineups.set_play(play)
        if not animate and play.hit and play.hit.angle is not None:
            self._show_hit(play)
        self._sync(play)
        if self.log.currentRow() != self._idx:
            self.log.blockSignals(True)
            self.log.setCurrentRow(self._idx)
            self.log.blockSignals(False)
        self.log.scrollToItem(self.log.item(self._idx),
                              QAbstractItemView.ScrollHint.EnsureVisible)
        self.band.ribbon.set_index(self._idx)

    def _sync(self, play: Play):
        pitches = play.pitches[:self._shown]
        count = ((pitches[-1].balls, pitches[-1].strikes) if pitches
                 else (0, 0))
        self.score.set_play(play, count)
        self.zone.set_pitches(play.pitches, self._shown)
        self.seq.setText("   ".join(
            f"{i+1} {p.ptype} {p.mph:.0f}" if p.mph else f"{i+1} {p.ptype}"
            for i, p in enumerate(pitches)))
        ss = self.game.season_stats
        bat = (ss.get(play.batter_id) or {}).get("batting") or {}
        pit = (ss.get(play.pitcher_id) or {}).get("pitching") or {}
        self.p_row.set(play.pitcher_id, play.pitcher, f"{play.pitch_hand}HP",
                       f"{play.pitcher_pitches}P   {pit.get('era', '—')} ERA")
        self.b_row.set(play.batter_id, play.batter, f"{play.bat_side}HB",
                       f"{bat.get('avg', '—')}   {bat.get('homeRuns', 0)} HR"
                       f"   {bat.get('ops', '—')}")
        if play.hit and self._shown >= len(play.pitches):
            self._show_hit(play)
        else:
            self.hit_stat.set(None, None, None)
            self.hit_stat.tint(INK)
            self.hit_note.setText("awaiting contact")

    def _show_hit(self, play: Play):
        h = play.hit
        if not h:
            return
        self.hit_stat.set(
            f"{h.ev:.1f}" if h.ev else None,
            f"{h.la:.0f}" if h.la is not None else None,
            f"{h.distance:.0f}" if h.distance else None)
        self.hit_stat.tint(LEV_C)
        self.hit_note.setText((h.trajectory or "").replace("_", " ").upper())

    def _refresh_heads(self):
        """Headshots arrive asynchronously; repaint whatever is on screen."""
        self.p_row.refresh_head()
        self.b_row.refresh_head()

    def toggle(self):
        if self._timer.isActive():
            self.stop()
        else:
            self.play()

    def play(self):
        if not self.game:
            return
        if self._phase == "idle":
            self.goto(self._idx, animate=True)
        self._timer.start()
        self.b_play.setText("⏸ Pause")

    def stop(self):
        self._timer.stop()
        self.b_play.setText("▶ Play")

    def _cycle_speed(self):
        self._speed = {1: 2, 2: 4, 4: 1}[self._speed]
        self.b_spd.setText(f"{self._speed}×")

    def _on_log_row(self, row: int):
        if row >= 0 and row != self._idx:
            self.stop()
            self.goto(row)

    def _tick_fn(self):
        if not self.game:
            return
        play = self.game.plays[self._idx]
        self._t += self._speed
        if self._phase == "pitches":
            if self._t >= self.PITCH_TICKS:
                self._t = 0
                if self._shown < len(play.pitches):
                    self._shown += 1
                    self._sync(play)
                    last = play.pitches[self._shown - 1]
                    if last.in_play and play.hit and play.hit.angle is not None:
                        self.field.start_flight(
                            play.hit.angle,
                            play.hit.distance or play.hit.calc_distance or 300)
                        self._phase = "flight"
                else:
                    if play.runner_moves and not play.hit:
                        self._phase = "runners"
                        self._t = 0
                    else:
                        self._phase = "gap"
        elif self._phase == "runners":
            self.field.action_t = min(1.0, self._t / self.FLIGHT_TICKS)
            self.field.update()
            if self._t >= self.FLIGHT_TICKS:
                self._t = 0
                self._phase = "gap"
        elif self._phase == "flight":
            self.field.ball_t = min(1.0, self._t / self.FLIGHT_TICKS)
            self.field.action_t = min(
                1.0, self._t / (self.FLIGHT_TICKS + self.HOLD_TICKS))
            self.field.update()
            if self._t >= self.FLIGHT_TICKS + self.HOLD_TICKS:
                self.field.callout = play.event.upper()
                self.field.callout_sub = self._hit_line(play)
                self.field.update()
                self._t = 0
                self._phase = "gap"
        elif self._phase == "gap":
            if self._t >= self.GAP_TICKS:
                if self._idx >= len(self.game.plays) - 1:
                    self.stop()
                    return
                self.goto(self._idx + 1, animate=True)

    @staticmethod
    def _hit_line(play: Play) -> str:
        h = play.hit
        if not h:
            return ""
        bits = []
        if h.ev:
            bits.append(f"{h.ev:.1f} MPH")
        if h.la is not None:
            bits.append(f"{h.la:.0f}°")
        if h.distance:
            bits.append(f"{h.distance:.0f} FT")
        return " · ".join(bits)


class MLBWindow(QMainWindow):
    """Standalone MLB viewer: game banner on top; lineup rail | pitcher half
    (SP form + bullpen) | batter half (Player Detail / Advanced Stats)."""

    # Lineup rail width. 178 shipped, but its viewport was then only 162
    # against items wanting 182 — which is why the longest names clipped
    # ("SP Yoshinobu Yamamo"). 200 = the 182 the delegate asks for, plus the
    # vertical scrollbar's gutter, plus the frame.
    RAIL_W = 200

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EffortMLB")
        self.stats = MLBPropStats()

        central = QWidget()
        self.setCentralWidget(central)
        # The real UI lives in its own container so startup can HIDE it rather
        # than merely cover it. A covered-but-visible widget tree is still
        # laid out and repainted by Qt on every change underneath the overlay,
        # and that cost lands on the GUI thread — which is the only thread
        # allowed to paint the loader. Hidden, Qt skips it entirely.
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._content = QWidget()
        outer.addWidget(self._content)
        root = QVBoxLayout(self._content)
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
        # NOT a splitter. `_cap_panel_height` pins the bullpen panel's height
        # to exactly its rows (min == max), so a vertical splitter could
        # never move it — the handle was 14px of dead grab area fighting a
        # pin, and it was taking that height from the SP form above. A plain
        # column gives the pen its data height and every remaining pixel to
        # the form. If the pen is ever made collapsible it should collapse
        # HORIZONTALLY, which is the axis its 25 columns actually run on.
        pitcher_col = QWidget()
        pcol = QVBoxLayout(pitcher_col)
        pcol.setContentsMargins(0, 0, 0, 0)
        pcol.setSpacing(0)
        # stretch=0, NOT 1. The form panel's two columns each end in a
        # trailing spacer, so with stretch=1 it swallowed every spare pixel
        # into those spacers — a dead band between the hitter strip and the
        # pen — and then pushed the 229px bullpen panel off the bottom of the
        # window. At 0 it takes its content height, the pen sits directly
        # under it, and any surplus pools BELOW the pen where it is harmless.
        pcol.addWidget(self.pitcher_form_panel, stretch=0)
        pcol.addWidget(self.bullpen_panel, stretch=0)
        pcol.addStretch(1)

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
        self.manager_tab = None
        self.replay_tab = None
        # a game may be selected before the Managers tab exists
        self._last_game_teams: List[str] = []
        # ...and before the Replay tab does, so its gamePk waits here
        self._pending_replay_pk: Optional[int] = None

        self.main_splitter = InsetSplitter(Qt.Orientation.Horizontal)
        # Wide enough that the inset grip is a comfortable grab target — the
        # rail|pitcher handle is also the rail's collapse toggle.
        self.main_splitter.setHandleWidth(9)
        self.main_splitter.addWidget(self.rail)
        self.main_splitter.addWidget(pitcher_col)
        self.main_splitter.addWidget(self.detail_tabs)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 1)
        # The rail's cards are a FIXED width — measured, the widest item
        # wants 182px — so dragging it to any intermediate size does nothing
        # but clip or pad. Capping it turns its handle from a pointless
        # resizer into what it was always for: a collapse toggle (index 0 is
        # the only collapsible section). The minimum stays 0 so that collapse
        # still works; the maximum is what stops the useless widening.
        #
        # The pitcher/tabs handle is NOT pointless and stays draggable: the
        # manager board is 36 columns wanting 2227px in a 706px viewport, so
        # every pixel that handle can give it is real.
        self.rail.setMaximumWidth(self.RAIL_W)
        self.main_splitter.setCollapsible(0, True)
        self.main_splitter.setCollapsible(1, False)
        self.main_splitter.setCollapsible(2, False)
        # Pen table needs ~882px to show every column without scrolling;
        # detail panel min is ~820 — split the 1900px budget accordingly
        self.main_splitter.setSizes([self.RAIL_W, 890, 838])
        # handle(1) is the rail|pitcher gutter; handle(2) (pitcher|tabs) is a
        # real resizer between two equals and stays plain.
        h = self.main_splitter.handle(1)
        if isinstance(h, InsetSplitterHandle):
            h.inset = True
            # Drop the rail's OWN right border — the handle redraws it on its
            # far edge. Left in, the two lines bracket the grip into a strip
            # of its own, which is the outside-the-panel look being replaced.
            self.rail.setStyleSheet(
                self.rail.styleSheet() + "\nQListWidget { border-right: 0; }")
        root.addWidget(self.main_splitter, stretch=1)

        # The startup overlay needs a window HANDLE to parent itself to, which
        # does not exist until the window is shown — see showEvent.
        self._loader = None
        self._loader_started = False
        central.installEventFilter(self)

        # Async init once the qasync loop is running
        QTimer.singleShot(0, lambda: asyncio.create_task(self._init_async()))
        self._cap_window_to_screen()

    # ------------------------------------------------------- startup overlay

    # Order matters only for the label shown; the pips fill as each lands.
    LOAD_STEPS = [
        ("tabs",       "building panels…"),
        ("roster",     "loading roster…"),
        ("schedule",   "fetching today's slate…"),
        ("lineups",    "posting lineups…"),
        ("percentile", "percentile boards…"),
        ("bullpen",    "bullpen availability…"),
        ("managers",   "walking the league's play-by-play…"),
    ]

    # A hung fetch must never trap the window behind the overlay.
    LOAD_TIMEOUT_MS = 90_000

    def showEvent(self, a0):
        super().showEvent(a0)
        if not self._loader_started:
            self._loader_started = True
            self._install_loader()

    def _install_loader(self):
        """In-window overlay, and the real UI hidden behind it.

        Deliberately NOT a separate always-on-top window: layering a transient
        window over the main one is unreliable (it lost stacking and the panels
        were visible populating underneath), and it is not what a splash inside
        an application window should be.

        In-window means the GUI thread paints it, and Qt permits no other
        thread to — widgets and QPixmap are main-thread-only, full stop. So
        smoothness here is bought by giving the GUI thread less to do, not by
        moving the animation off it: `_content` is HIDDEN for the whole load,
        which is what stops Qt laying out and repainting thirty panels
        underneath an overlay nobody can see through anyway."""
        central = self.centralWidget()
        self._content.hide()
        try:
            self._loader = QuickSeamLoader(central, self.LOAD_STEPS)
        except Exception as e:
            print(f"EffortMLB: Quick loader unavailable ({e}); "
                  f"using the QPainter overlay")
            self._loader = SeamLoader(central, self.LOAD_STEPS)
            self._loader.setGeometry(central.rect())
            # Built in showEvent, i.e. into an ALREADY-visible parent, so Qt
            # does not show it for us the way it would for a child made in
            # __init__.
            self._loader.show()
        self._loader.finished.connect(self._on_loader_faded)
        self._loader.raise_()

    def _reveal_content(self):
        """Show the finished UI underneath the overlay, then fade it out.

        Order matters: showing `_content` triggers the one and only full
        layout pass for the whole window, and that pass has to happen while
        the overlay is still opaque or it is seen as a flash of unstyled
        panels snapping into place."""
        if self._content.isVisible():
            return
        self._content.show()
        if self._loader is not None:
            self._loader.raise_()
        # Panels that measured themselves while hidden have to re-measure now
        # that Qt has resolved a real layout. Deferred one turn so the show's
        # layout pass has actually run before anything reads geometry back.
        QTimer.singleShot(0, self._relayout_after_reveal)

    def _relayout_after_reveal(self):
        try:
            self.bullpen_panel.refresh_layout()
        except Exception as e:
            print(f"EffortMLB: post-reveal relayout failed: {e}")

    def _on_loader_faded(self):
        self._loader = None

    def _sync_loader(self):
        if self._loader is not None:
            self._loader.setGeometry(self.centralWidget().rect())

    def eventFilter(self, a0, a1):
        """Keep the overlay covering the window as it is resized."""
        if (a1 is not None and self._loader is not None
                and a1.type() == QEvent.Type.Resize
                and a0 is self.centralWidget()):
            self._sync_loader()
        return super().eventFilter(a0, a1)

    def _load_step(self, key: str):
        if self._loader is not None:
            self._loader.step(key)

    def _loader_done(self):
        # Reveal FIRST, so the layout pass happens under an opaque overlay,
        # then start the fade. The loader nulls itself out when it has faded.
        self._reveal_content()
        if self._loader is not None:
            self._loader.finish()

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
        # The Quick overlay is its OWN window, so it has to be dragged along.
        self._sync_loader()
        super().moveEvent(a0)

    def closeEvent(self, a0):
        # The shared keep-alive session outlives every individual fetch, so
        # it has to be closed here or aiohttp warns about an unclosed
        # connector on teardown.
        try:
            asyncio.get_event_loop().create_task(self.stats.close())
        except RuntimeError:
            pass
        super().closeEvent(a0)

    # ------------------------------------------------------------ async init

    async def _init_async(self):
        watchdog = QTimer(self)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(self._loader_done)
        watchdog.start(self.LOAD_TIMEOUT_MS)
        self._tracker = StartupTracker(asyncio.get_event_loop())
        self._tracker.install()
        try:
            await self._init_async_inner()
            # Everything named above has landed; now wait for everything the
            # panels kicked off on their own before revealing the window.
            await self._tracker.drain()
        finally:
            self._tracker.uninstall()
            watchdog.stop()
            self._loader_done()

    async def _init_async_inner(self):
        try:
            from TrackingStatsWidget import AdvancedStatsWidget
            self.advanced_stats_widget = AdvancedStatsWidget()
            self.advanced_stats_widget.set_sport("baseball_mlb")
            self.detail_tabs.addTab(self.advanced_stats_widget,
                                    "Advanced Stats")
        except Exception as e:
            print(f"EffortMLB: Advanced Stats tab unavailable: {e}")
        # Hand the loop back between tabs. Each of these builds a full widget
        # tree (tables, plots, delegates) and Qt gives no yield points inside
        # that, so constructing all four back to back was one unbroken block
        # with no repaint in it — the overlay's biggest single stall.
        await asyncio.sleep(0)
        # League manager/bullpen board — lazy, loads on first view
        self.manager_tab = ManagerBoardTab(self.stats)
        self.detail_tabs.addTab(self.manager_tab, "Managers")
        await asyncio.sleep(0)
        # The only forward-looking surface in the window, so it gets its own
        # tab rather than being mixed into the SP card where a projection
        # would read as another measurement. Lazy like the board above.
        self.projections_tab = ProjectionsTab(self.stats)
        self.detail_tabs.addTab(self.projections_tab, "Projections")
        await asyncio.sleep(0)
        # Game replay off the live feed. Lazy like the others: it holds no
        # data until a game is selected, and the feed for a FINAL game is
        # fetched once ever and then read from disk.
        self.replay_tab = ReplayTab(self.stats)
        self.detail_tabs.addTab(self.replay_tab, "Replay")
        await asyncio.sleep(0)
        if self._pending_replay_pk:
            self._load_replay(self._pending_replay_pk)
        # Nothing is selected at launch, so Player Detail would leave the
        # WIDEST pane in the window empty until the first click. The league
        # board needs no selection to be worth reading, and a player click
        # switches away from it (see _show_player_detail). Making it current
        # also trips its lazy showEvent load, so it fills while you work.
        # The board's load is kicked by showEvent, so arm the completion hook
        # BEFORE making it current or the signal can fire into nothing.
        mgr_done = asyncio.get_event_loop().create_future()

        def _mgr_finished():
            if not mgr_done.done():
                mgr_done.set_result(True)
        self.manager_tab.load_finished.connect(_mgr_finished)
        self.detail_tabs.setCurrentWidget(self.manager_tab)
        # setCurrentWidget cannot trip the board's lazy showEvent while the
        # whole UI is hidden behind the overlay, so start it explicitly.
        self.manager_tab.ensure_loaded()
        if self._last_game_teams:
            self.manager_tab.set_highlight(self._last_game_teams)
        self._load_step("tabs")
        try:
            async with self.stats.http() as session:
                if not await self.stats.ensure_roster(session):
                    print("EffortMLB: roster load failed")
                    return
                self._load_step("roster")
                games = await self.stats._get_schedule(session)
        except Exception as e:
            print(f"EffortMLB: init failed: {e}")
            return
        self._load_step("schedule")
        # BEFORE select(0): choosing a game loads the Replay tab, which calls
        # the SYNCHRONOUS annotate_win_expectancy. Restore the league tables
        # and fit the surface off-thread first so that call is a cache hit —
        # warming it after the manager board (where the tables are final) is
        # too late to save the one fit that lands mid-animation.
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self.stats.load_league_tables)
            await self.stats.warm_we_surface()
        except Exception as e:
            print(f"EffortMLB: league table warm failed: {e}")
        self.banner.set_games(games, self.stats._teams)
        if games:
            self.banner.select(0)
        self._start_lineup_poll(games)
        self._load_step("lineups")

        async def _percentiles():
            await self._load_percentile_data()
            self._load_step("percentile")

        async def _pen():
            await self._populate_bullpen_teams()
            self._load_step("bullpen")

        async def _mgr():
            # showEvent may never fire (offscreen, or the tab was swapped
            # before it painted) — the watchdog in _init_async is the backstop
            # for that, so this simply waits.
            await mgr_done
            # League tables are final now — fit the WE surface off-thread so
            # the Replay tab's synchronous annotate never does it inline.
            try:
                await self.stats.warm_we_surface()
            except Exception as e:
                print(f"EffortMLB: WE warm failed: {e}")
            self._load_step("managers")

        async def _game():
            # Whatever `banner.select(0)` above fanned out (rail decoration,
            # replay + win-expectancy fit, SP card).
            for t in list(getattr(self, "_game_tasks", ())):
                try:
                    await t
                except Exception:
                    pass

        # The overlay stays up until ALL of it has landed. Exceptions are
        # swallowed per-branch so one dead board cannot hold the window.
        await asyncio.gather(_percentiles(), _pen(), _mgr(), _game(),
                             return_exceptions=True)

    # ------------------------------------------------------- lineup polling
    #
    # The slate is fetched ONCE at startup and the game dicts it returns are
    # what the banner and the rail read lineups out of. Everything else in
    # this window is settled by the time the slate posts (that is the premise
    # the date-keyed slate cache is built on) — lineups are the exception, and
    # a card that goes up at 16:10 for a 17:40 first pitch was simply never
    # seen by a session launched at noon. Nothing re-read the schedule, so the
    # rail sat on its alphabetical roster fallback for the rest of the day.

    LINEUP_POLL_MS = 5 * 60 * 1000

    @staticmethod
    def _posted(game: dict) -> bool:
        lu = game.get("lineups") or {}
        return bool(lu.get("awayPlayers") and lu.get("homePlayers"))

    @classmethod
    def _lineups_pending(cls, games: List[dict]) -> bool:
        """True while any game that could still post a card has not.

        Finals are skipped so the poll stops on a slate where an early game
        finished without its lineup ever being hydrated — otherwise it would
        run all night against a card that is never coming."""
        return any((g.get("status") or {}).get("abstractGameState") != "Final"
                   and not cls._posted(g) for g in games)

    def _start_lineup_poll(self, games: List[dict]):
        if not self._lineups_pending(games):
            return
        timer = getattr(self, "_lineup_timer", None)
        if timer is None:
            timer = self._lineup_timer = QTimer(self)
            timer.timeout.connect(
                lambda: asyncio.create_task(self._poll_lineups()))
        if not timer.isActive():
            timer.start(self.LINEUP_POLL_MS)

    async def _poll_lineups(self):
        """Re-read the slate and fold newly posted lineups into the window."""
        try:
            async with self.stats.http() as session:
                games = await self.stats._get_schedule(session, force=True)
        except Exception as e:
            print(f"EffortMLB: lineup poll failed: {e}")
            return
        if not games:
            return
        was = {g.get("gamePk"): self._posted(g) for g in self.banner._games}
        sel_pk = (self.banner.current_game() or {}).get("gamePk")
        self.banner.refresh_games(games, self.stats._teams)
        # Only the SELECTED game's rail is on screen, and rebuilding it drops
        # the row highlight — so rebuild solely on the posted transition, not
        # on every poll.
        cur = self.banner.current_game()
        if (cur is not None and cur.get("gamePk") == sel_pk
                and self._posted(cur) and not was.get(sel_pk, False)):
            self.rail.set_game(cur, self.stats)
            self._rail_gen = getattr(self, "_rail_gen", 0) + 1
            asyncio.create_task(self._decorate_rail(self._rail_gen))
        if not self._lineups_pending(games):
            self._lineup_timer.stop()

    async def _load_percentile_data(self):
        try:
            from MLBpercentilerankings import (fetch_leaderboard_data,
                                               PITCHER_URL, HITTER_URL)
            loop = asyncio.get_event_loop()
            # Two independent downloads — serialising them cost a full extra
            # round trip before the percentile bars could render
            hitters, pitchers = await asyncio.gather(
                loop.run_in_executor(None, fetch_leaderboard_data, HITTER_URL),
                loop.run_in_executor(None, fetch_leaderboard_data, PITCHER_URL))
            self.player_detail_panel.set_percentile_data(hitters, pitchers)
            self.pitcher_form_panel.set_percentile_data(pitchers)
        except Exception as e:
            print(f"EffortMLB: percentile data load failed: {e}")

    async def _populate_bullpen_teams(self):
        try:
            async with self.stats.http() as session:
                if await self.stats.ensure_roster(session):
                    self.bullpen_panel.set_teams(
                        list(self.stats._teams.values()))
        except Exception as e:
            print(f"EffortMLB: bullpen team list load failed: {e}")

    async def _show_umpire(self, name: Optional[str], gen: int):
        """Fill the umpire card for tonight's plate assignment.

        The profile is built from play-by-play the manager tendencies pull
        anyway, so this only waits on the season officials map (one request,
        cached six hours). Early in a session that map may be all there is —
        the card then shows the name and says the profile has not arrived
        rather than rendering blank."""
        card = getattr(self.pitcher_form_panel, "_ump_card", None)
        if card is None:
            return
        if not name:
            card.clear()
            return
        card.set_data(name, None, None)          # name immediately
        # The tallies accrue as the manager board walks all 30 clubs' games,
        # so on a cold start the profile lands seconds after the card does.
        # Re-check on a backoff instead of leaving "no profile yet" up.
        profiles, league, prof = {}, None, None
        for wait in (0, 4, 8, 15, 30):
            if wait:
                await asyncio.sleep(wait)
            if gen != getattr(self, "_ump_gen", 0):
                return                            # game switched mid-flight
            try:
                async with self.stats.http() as session:
                    profiles = await self.stats.umpire_profiles(session)
            except Exception as e:
                print(f"EffortMLB: umpire profile fetch failed: {e}")
                return
            league = profiles.pop("__league__", None)
            prof = profiles.get(name)
            if prof:
                break
        if gen != getattr(self, "_ump_gen", 0):
            return
        spread = None
        if profiles:
            def rng(key):
                vals = [p[key] for p in profiles.values()
                        if p.get(key) is not None]
                return (min(vals), max(vals)) if vals else None
            spread = {k: v for k, v in
                      (("csr_d", rng("csr_d")),
                       ("ovr_against", rng("ovr_against"))) if v}
        card.set_data(name, prof, league, spread)

    # ------------------------------------------------------- game selection

    def _load_replay(self, game_pk: int):
        """Hand a gamePk to the Replay tab, holding it if the tab isn't up
        yet (it is built in _init_async, which may not have run)."""
        self._pending_replay_pk = game_pk
        if self.replay_tab is None:
            return

        async def go():
            try:
                async with self.stats.http() as session:
                    await self.replay_tab.load_game(session, game_pk)
                    # the browser's season index is one request and is what
                    # makes historical replays reachable from inside the tab
                    await self.replay_tab.load_season(session,
                                                      self.stats.season)
            except Exception as e:
                print(f"EffortMLB: replay load failed: {e}")
        asyncio.create_task(go())

    def _on_game_selected(self, game: dict):
        # Tasks this selection fans out, so the startup overlay can wait on
        # them — `banner.select(0)` fires this DURING init, and its work was
        # previously untracked, which is why a 380ms replay/win-expectancy
        # stall used to land in the middle of the fade. `_show_umpire` is
        # deliberately NOT tracked: it backs off for up to 57s by design.
        self._game_tasks = []
        pk = game.get("gamePk")
        if pk:
            self._load_replay(pk)
        self.rail.set_game(game, self.stats)
        self._rail_gen = getattr(self, "_rail_gen", 0) + 1
        self._game_tasks.append(
            asyncio.create_task(self._decorate_rail(self._rail_gen)))
        # Seed the pitcher half with the away probable until a click refines
        away = game.get("teams", {}).get("away", {})
        home = game.get("teams", {}).get("home", {})
        sp = away.get("probablePitcher") or {}
        h_abbr = self.stats._teams.get(
            (home.get("team") or {}).get("id"), "?")
        a_abbr = self.stats._teams.get(
            (away.get("team") or {}).get("id"), "?")
        venue = ((game.get("venue") or {}).get("name") or "")
        self._venue = venue
        self._ump_gen = getattr(self, "_ump_gen", 0) + 1
        asyncio.create_task(
            self._show_umpire(self.stats.tonight_umpire(game), self._ump_gen))
        # each probable faces the OTHER club; the detail-panel path reaches
        # show_pitcher without the game in hand, so cache the mapping
        self._opp_of = {}
        if sp.get("id"):
            # the away probable faces the HOME lineup
            self.pitcher_form_panel.show_pitcher(
                sp["id"], sp.get("fullName", "?"),
                context=f"{a_abbr} probable — @ {h_abbr}", venue=venue,
                opp=h_abbr)
        self.bullpen_panel.show_team(a_abbr, f"{a_abbr} pen")
        # both pens of this game feed the availability edge in the pen header;
        # the probables sharpen how many innings each pen has to cover
        h_sp = home.get("probablePitcher") or {}
        if sp.get("id"):
            self._opp_of[sp["id"]] = h_abbr
        if h_sp.get("id"):
            self._opp_of[h_sp["id"]] = a_abbr
        sp_ids = {}
        if sp.get("id"):
            sp_ids[a_abbr] = sp["id"]
        if h_sp.get("id"):
            sp_ids[h_abbr] = h_sp["id"]
        self.bullpen_panel.set_matchup(a_abbr, h_abbr, sp_ids)
        self._last_game_teams = [a_abbr, h_abbr]
        if self.manager_tab is not None:
            self.manager_tab.set_highlight(self._last_game_teams)
        if getattr(self, "projections_tab", None) is not None:
            self.projections_tab.set_game(self._last_game_teams, {})

    async def _decorate_rail(self, gen: int):
        """Fill the lineup rail with mugshots and a wRC+/Def/WPA line per
        batter (FG batting board, cached), plus framing/blocking runs on
        catchers (Savant receiving boards). Aborts silently when the rail is
        rebuilt mid-flight (game switch).

        Every player's fetches run concurrently — a lineup is 9-18 rows and
        the per-row work (headshot GET, FG board lookup, catcher receiving)
        is independent, so doing it serially cost one round trip per row."""
        HEADSHOT_DIR.mkdir(exist_ok=True)
        grew = False
        try:
            rows = []
            for i in range(self.rail.count()):
                it = self.rail.item(i)
                pid = it.data(LineupRail.PID_ROLE)
                if not pid:
                    continue
                rows.append((i, pid, it.data(Qt.ItemDataRole.UserRole),
                             it.data(LineupRail.POS_ROLE)))
            if not rows:
                return

            async with self.stats.http() as session:

                async def decorate(i, pid, data, pos):
                    """Pure I/O — no Qt touched, so results can be applied in
                    one pass after the gather (and dropped if stale)."""
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
                    fgb = None
                    recv = None
                    if data and not data[1]:      # batter → FG value line
                        try:
                            fgb = await self.stats.get_fg_batting(pid)
                        except Exception:
                            fgb = None
                        if pos == "C":
                            try:
                                recv = await self.stats.get_catcher_defense(
                                    session, pid)
                            except Exception:
                                recv = None
                    return i, path, fgb, recv

                results = await asyncio.gather(
                    *(decorate(*row) for row in rows))

            if gen != self._rail_gen:
                return
            for i, path, fgb, recv in results:
                it = self.rail.item(i)
                if it is None:
                    continue
                if path.exists():
                    it.setIcon(QIcon(str(path)))
                if fgb or recv:
                    # Store the 4 value stats for the delegate's 2x2 grid,
                    # plus framing/blocking runs on catchers (3rd row)
                    fgb = fgb or {}
                    cell = {
                        "wrcplus": fgb.get("wrcplus"),
                        "defense": fgb.get("defense"),
                        "wpa": fgb.get("wpa"),
                        "bsr": fgb.get("bsr"),
                    }
                    if recv:
                        cell["framing_runs"] = recv.get("framing_runs")
                        cell["blocking_runs"] = recv.get("blocking_runs")
                        it.setToolTip(self._receiving_tip(recv))
                        grew = True
                    it.setData(LineupRail.STATS_ROLE, cell)
            if grew and gen == self._rail_gen:
                # catcher cards gained a stat row — re-run the row layout so
                # the taller sizeHint is picked up
                self.rail.doItemsLayout()
        except RuntimeError:
            return   # rail items deleted mid-decoration (game switched)

    @staticmethod
    def _receiving_tip(recv: dict) -> str:
        """Hover detail behind a catcher card's Frm/Blk cells."""
        def num(v, fmt="{:+.1f}"):
            return "—" if v is None else fmt.format(v)
        return (
            f"Framing: {num(recv.get('framing_runs'))} runs, "
            f"{num(recv.get('strike_rate'), '{:.1%}')} called strikes "
            f"({num(recv.get('framed_pitches'), '{:.0f}')} pitches)\n"
            f"Blocking: {num(recv.get('blocking_runs'))} runs, "
            f"{num(recv.get('blocks_aa'))} blocks above avg "
            f"({num(recv.get('pbwp'), '{:.0f}')} PB/WP vs "
            f"{num(recv.get('x_pbwp'), '{:.1f}')} expected)")

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
            async with self.stats.http() as session:
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
        asyncio.create_task(self._load_fg_splits(summary))
        if not summary.market_key.startswith("pitcher"):
            asyncio.create_task(self._load_matchup_batter(summary))

    async def _load_matchup_batter(self, summary):
        """Fetch the shown batter's swing-path/box metrics and hand them to
        the SP-form panel, so the arsenal flight viewer can drop this batter
        into the box (pitcher-vs-batter combo view)."""
        try:
            async with self.stats.http() as session:
                swing = await self.stats.get_batter_swing_path(
                    session, summary.player_id)
        except Exception as e:
            print(f"EffortMLB: batter swing-path failed: {e}")
            return
        if swing:
            # Swing length (ft, FanGraphs bat-tracking) sets where the swing
            # STARTS along the arc — the swing-path board has no such field.
            try:
                fgb = await self.stats.get_fg_batting(summary.player_id)
                if fgb and fgb.get("swing_length") is not None:
                    swing = dict(swing, swing_length=fgb["swing_length"])
            except Exception as e:
                print(f"EffortMLB: batter swing-length join failed: {e}")
            # Real per-moment foot positions (stance / pitch release / bat-ball
            # intercept), scraped from Savant's batting-stance visual. Absent
            # for a handful of hitters — the overlay falls back to a generic
            # stride when this is None.
            try:
                from sp_flight_viewer import get_stance_moments
                moments = get_stance_moments(summary.player_id,
                                             swing.get("side"))
                if moments:
                    swing = dict(swing, stance_moments=moments)
            except Exception as e:
                print(f"EffortMLB: stance-moment join failed: {e}")
            self.pitcher_form_panel.set_matchup_batter(
                summary.player_name, swing)

    async def _load_situational_splits(self, summary):
        group = ("pitching" if summary.market_key.startswith("pitcher")
                 else "hitting")
        try:
            async with self.stats.http() as session:
                splits = await self.stats.get_situational_splits(
                    session, summary.player_id, group)
        except Exception as e:
            print(f"EffortMLB: situational splits failed: {e}")
            return
        if (self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_situational(splits, group)

    async def _load_fg_splits(self, summary):
        """FanGraphs splits leaderboard for the shown HITTER.

        Hitters only for now: the split ids in FG_SPLIT_IDS were verified
        against batting PA, and a pitcher board would need its own
        identification pass rather than an assumption that the ids carry
        over.

        The boards are league-wide and cached for the slate, so this is free
        after the first player of the day."""
        if summary.market_key.startswith("pitcher"):
            self.player_detail_panel.show_fg_splits({}, 0, None)
            return
        try:
            splits = await self.stats.get_fg_splits(
                "B", FG_SPLIT_TYPE_ADVANCED)
            fg = await self.stats.get_fg_batting(summary.player_id)
        except Exception as e:
            print(f"EffortMLB: FG splits failed: {e}")
            return
        if (self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_fg_splits(
                splits, summary.player_id, (fg or {}).get("wrcplus"))

    async def _load_traditional_stats(self, summary):
        group = ("pitching" if summary.market_key.startswith("pitcher")
                 else "hitting")
        try:
            async with self.stats.http() as session:
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
                # 4th row: plate-discipline + batted-ball profile (FG rates are
                # 0-1 fractions → shown as percentages, HR/FB likewise)
                pct = lambda v: f"{v:.1%}" if isinstance(v, (int, float)) else None
                for label, key in (("Z-Con%", "z_contact"),
                                   ("Con%", "contact"),
                                   ("SwStr%", "swstr"),
                                   ("Chase%", "chase"),
                                   ("Pull%", "pull_pct"),
                                   ("HR/FB", "hr_fb"),
                                   ("HardHit%", "hardhit")):
                    v = pct(fgb.get(key))
                    if v is not None:
                        extra.append((label, v))
                self.player_detail_panel.show_traditional(pairs + extra)
                self.player_detail_panel.show_swing(fgb)
                # BMIELKE rides the pitch detail this panel already caches, so
                # it costs no extra request — but the session opened above is
                # closed by now, so it needs its own.
                try:
                    async with self.stats.http() as bsess:
                        rows = await self.stats._get_pitch_detail(
                            bsess, summary.player_id, "batter")
                        # v9's prior. One extra cached CSV; if it misses, the
                        # metric degrades to the v8 league prior rather than
                        # failing.
                        pw, pn = await self.stats.get_prior_wobacon(
                            bsess, summary.player_id)
                    # RE-CHECK the shown player. The guard above ran BEFORE
                    # these two awaits, and this is the slowest fetch on the
                    # panel — so on a fast click-through the earlier player's
                    # task finishes LAST and writes his index onto whoever is
                    # on screen now. It is a permanent wrong value, not a
                    # flicker, because nothing recomputes afterwards.
                    if (self.player_detail_panel.current_player_name()
                            == summary.player_name):
                        self.player_detail_panel.set_bmielke(
                            bmielke(rows, pw, pn))
                except Exception as e:
                    print(f"EffortMLB: BMIELKE failed: {e}")
                    if (self.player_detail_panel.current_player_name()
                            == summary.player_name):
                        self.player_detail_panel.set_bmielke(None)

    async def _load_pitch_splits(self, summary):
        player_type = ("pitcher" if summary.market_key.startswith("pitcher")
                       else "batter")
        try:
            async with self.stats.http() as session:
                splits = await self.stats.get_pitch_splits(
                    session, summary.player_id, player_type)
                velo_splits = await self.stats.get_velo_splits(
                    session, summary.player_id, player_type)
                # Rides the same cached pitch detail — no extra request
                counts = await self.stats.get_count_splits(
                    session, summary.player_id, player_type)
                bb_prof = await self.stats.get_batted_ball_profile(
                    session, summary.player_id, player_type)
                disc = await self.stats.get_plate_discipline(
                    session, summary.player_id, player_type)
                raw_pitches = await self.stats._get_pitch_detail(
                    session, summary.player_id, player_type)
        except Exception as e:
            print(f"EffortMLB: pitch splits failed: {e}")
            return
        if (self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_pitch_splits(
                splits, player_type, velo_splits)
            self.player_detail_panel.show_count_splits(counts)
            self.player_detail_panel.show_batted_ball(bb_prof)
            self.player_detail_panel.show_discipline(disc)
            self.player_detail_panel.set_pitch_rows(raw_pitches)
            try:
                async with self.stats.http() as session:
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
            async with self.stats.http() as session:
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
                     else f"opp SP — {summary.player_name}"),
            venue=getattr(self, "_venue", ""),
            opp=getattr(self, "_opp_of", {}).get(card_pid, ""))
        try:
            async with self.stats.http() as session:
                arsenal = None
                if not is_pitcher_prop:
                    arsenal = await self.stats.get_pitch_arsenal(
                        session, card_pid)
                    if (arsenal
                            and self.player_detail_panel.current_player_name()
                            == summary.player_name):
                        self.player_detail_panel.set_opposing_arsenal(
                            card_name, arsenal)
                    # His mix BY COUNT — same cached detail the SP card uses
                    mix = await self.stats.get_pitch_mix_by_count(
                        session, card_pid)
                    if (self.player_detail_panel.current_player_name()
                            == summary.player_name):
                        self.player_detail_panel.show_pitch_mix(card_name, mix)
                    # Zone grids — both sides off the same cached detail
                    bat_z, sp_z = await self.stats.get_zone_grids(
                        session, summary.player_id, card_pid)
                    if (self.player_detail_panel.current_player_name()
                            == summary.player_name):
                        self.player_detail_panel.show_zone(
                            bat_z, sp_z, card_name)
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
                # Same FG row the Stuff+ column comes from — PitchingBot is
                # just the block of it nobody was reading.
                self.player_detail_panel.show_sp_pitchingbot(
                    sp_stuff, card_name)
            self.player_detail_panel.show_matchup(ctx)
            if arsenal and sp_stuff:
                self.player_detail_panel.set_opposing_arsenal(
                    card_name, arsenal, sp_stuff)


def launch():
    """Run the EffortMLB window standalone (qasync event loop)."""
    import sys
    import qasync
    from PyQt6.QtWidgets import QApplication
    # Running as a script loads this module as __main__; alias it so lazy
    # importers (e.g. TrackingStatsWidget, EffortOddsPropsWindow) reuse it
    # instead of loading a second copy of the module
    sys.modules.setdefault("EffortMLB", sys.modules[__name__])
    prune_slate_cache()
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
