"""
mlb_sim.py — MLB projection and prop-pricing engine (Qt-free, self-contained).

Originates game-level projections instead of describing history. One Monte
Carlo run produces every prop jointly and with the correct correlation
structure, which is what SGP/parlay pricing needs.

    boards -> shrunk per-PA outcome vectors        (section 9)
    ball-flight geometry, for the HR distance calibration  (section 10)
    PA multinomial -> base/out Markov -> MC        (sections 1-8)
    -> priced board                                (section 11)

Command line:

    python mlb_sim.py                          smoke test on synthetic sides
    python mlb_sim.py rates                    ingest report off the boards
    python mlb_sim.py calibrate                fit the HR distance scale
    python mlb_sim.py project NYY BOS --venue "Fenway Park"
    python mlb_sim.py clv                      score against market movement
    python mlb_sim.py marks [--refresh]        re-measure the reference marks
    python mlb_sim.py dispersion               run DISPERSION on clone sides
    python mlb_sim.py slate [--refresh]        the REAL slate, scored on itself
    python mlb_sim.py calibrate-form           fit the game-level form draw
    python mlb_sim.py calibrate-fatigue        fit the opening penalty
    python mlb_sim.py asof [--every 7]         cache AS-OF boards, leak-free
    python mlb_sim.py backtest                 replay a season on frozen rates
    python mlb_sim.py closing                  model vs the DE-VIGGED close
    python mlb_sim.py clvopen                  model vs the OPENING line (CLV)
    python mlb_sim.py forecastwx               PERIOD-CORRECT weather (5b.2)
    python mlb_sim.py boards 2024 2025         fetch FULL-SEASON boards
    python mlb_sim.py pbp [seasons] [--check]  ONE-TIME play-by-play backfill
    python mlb_sim.py stints [--refresh]       relief-appearance shape
    python mlb_sim.py baserunning [--refresh]  measured advancement rates
    python mlb_sim.py milb 2024 2025 2026      minor league lines + AAA arsenal (9c)
    python mlb_sim.py parkbuild [seasons]      per-outcome park factors + exposure
    python mlb_sim.py milbasof 2025 2026       AS-OF AAA snapshots (5.11.1)
    python mlb_sim.py milbpark 2024 2025 2026  AAA PARK factors, per outcome
    python mlb_sim.py framing --validate     rebuild framing from PITCH level (9d)
    python mlb_sim.py aaa [--refresh]          AAA->MLB translation, fitted
    python mlb_sim.py re24                     run expectancy vs measured
    python mlb_sim.py ab                       A/B a change vs the CLOSE (3d)
    python mlb_sim.py diff [--check]           score on the RUN DIFFERENTIAL (4f)
    python mlb_sim.py eventodds                OPENING odds + TOTALS per event
    python mlb_sim.py stuff                    pitch-model REPEATABILITY (3d.8)
    python mlb_sim.py recency                  within-season recency (3d.9)

On disk:

    OddsAPI/Sims/          this file, mlb_ml.py, their suites, sim_state.md
    OddsAPI/Sims/savedata/ SAVE_DIR — everything only the sim reads or writes:
                           asof/ ab/ MLBclv/ mlml/ pa/ bmielke/ itp/ plus the
                           park, reliever, baserunning and slate tables
    OddsAPI/savedata/      SHARED_DIR — the caches co-owned with EffortMLB and
                           written by BOTH: fg_{bat,pit}_<season>.json,
                           mlb_roster_<season>.json, pbp/, itp_cookies.json
    OddsAPI/model_data/    DATA_DIR — shared with homerunwidget/weatherman
    OddsAPI/               the flat modules imported below, and the Savant CSVs

Reach the co-owned set through `_shared()` and never by hardcoding; §9's note
on `SHARED_DIR` says why two stores rather than one.

Everything lives here on purpose. The only outside dependencies are
`weatherman` (park geometry, wind rotation) and `homerunwidget`
(BallFlightSimulator, CD_NEUTRAL), both imported LAZILY so this module stays
importable without scipy, pywavefront or a QApplication.

`sim_state.md` is the spec: what is validated, what is open, the traps, and
appendix A — the long derivations that used to live in this file. Four traps
matter enough to repeat here:

  * **Fatigue is smooth in pitch count / BF, never a step at batter 19**
    (Brill/Deshpande/Wyner, arXiv:2210.06724). `sp_tto3` and `tto_penalty()`
    in EffortMLB.py measure MANAGER BEHAVIOUR and are not outcome multipliers.
  * **Fatigue must also be CENTRED** (`FATIGUE_REF_BF`) — a season rate already
    contains the pitcher's own average fatigue.
  * **Recency is NULL on BOTH sides, and it is CAPPED.** 3d.9 read a hitter
    effect at pooled t +3.09; 4d superseded that over 1.3M PA (best arm
    +0.00006, short windows materially WORSE), and 5.23 bounded it — tuning a
    per-OUTCOME half-life with full hindsight buys <= 1% of RMSE on one outcome
    and picks "no decay" for most, a run-value ceiling of +0.000334/PA. The
    `RECENCY_HALF_LIFE_*` constants and `USE_RECENCY` stay for the arm; nothing
    reads them while it is off.
  * **Base-running detail buys nothing for WIN PROBABILITY** (arXiv:2511.17733)
    — a narrower claim than it reads. That paper asked about a manager's
    pull/hold decision; we price stolen bases, runs and RBI, where the runner's
    own ability IS the quantity being bet on. So the transition CONSTANTS stay
    coarse while WHO is running is per-player (`runner_profile`).

Anything pooled across processes must stay at module level.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import copy
import csv
import datetime
import gzip
import hashlib
import io
import json
import math
import multiprocessing
import os
import pickle
import random
import re
import statistics
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
import bmielke_core



# ---------------------------------------------------------------------------
# **This package lives in `OddsAPI/Sims/`, its collaborators one level up.**
# BOTH directories go on the path, not just the parent: `mlb_ml` imports
# `mlb_sim` by bare name and a pool worker re-imports the main module the same
# way. sim_state.md A.0.
_SIM_ROOT = Path(__file__).resolve().parent           # OddsAPI/Sims
_APP_ROOT = _SIM_ROOT.parent                          # OddsAPI
for _p in (str(_SIM_ROOT), str(_APP_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ---------------------------------------------------------------------------

import numpy as np   # noqa: E402 - ONE import. It was function-local in 11
# places; numpy costs ~30ms and is not in CLAUDE.md's lazy list (lightgbm,
# sklearn, pandas), which stay lazy. `multiprocessing` is already eager here.
import requests   # noqa: E402 - must follow the sys.path bootstrap above
import weatherman   # noqa: E402 - park geometry + the Open-Meteo client

# ---------------------------------------------------------------------------
# **This module must be ONE object however it is entered.** Run as
# `python mlb_sim.py`, this file is `__main__` and any `import mlb_sim` —
# `mlb_ml` does one — builds a SECOND copy carrying the SHIPPED defaults, so
# every constant an A/B rebinds is invisible to it. Silent, and it has already
# cost a full A/B. `__mp_main__` is in the tuple because forkserver and spawn
# import the main module under THAT name. sim_state.md A.0.
if __name__ in ("__main__", "__mp_main__"):
    sys.modules.setdefault("mlb_sim", sys.modules[__name__])
# ---------------------------------------------------------------------------



# **`_wm()` REMOVED 2026-08-29 and its stated reason was false.** It read: "
# `weatherman` imports PyQt6 at module scope ... so hoisting either makes this
# module un-importable headless." Measured with DISPLAY and WAYLAND_DISPLAY
# both unset: weatherman imports in 0.13s, live_scores_widget 0.15s,
# homerunwidget 0.65s. **Importing PyQt6 never needed a display** — only
# instantiating a QApplication or a widget does, and none of these do that at
# module scope.
#
# The accessor was also redundant on its own terms: `import x` inside a function
# is ALREADY cached by `sys.modules` and costs ~103 ns after the first call, so
# the `global` dance saved a dict lookup. `weatherman` is now a plain top-level
# import like `requests`.
#
# `homerunwidget` DOES stay lazy, on the honest reason: 0.65s, and it is reached
# only by `BallFlight`. Cost, not importability.

# ===========================================================================
# 0. QUERY LAYER — every outbound request this module makes
# ===========================================================================
# Two caching rules: a SAME-RUN MEMO for anything that can still change (it dies
# with the process, so a forecast is never stale across runs), and a PERMANENT
# GZIP DISK CACHE only for what CANNOT change. The FULL response is stored,
# never a `fields=` projection — a consumer that later reads an omitted key gets
# an EMPTY result, not an error. sim_state.md A.0.
class Query:
    """Shared transport for every outbound request in this module."""

    _MEMO: Dict[str, object] = {}

    @staticmethod
    def memo(key: str, build):
        """`build()`, once per process, per key. For data that can still move."""
        if key not in Query._MEMO:
            Query._MEMO[key] = build()
        return Query._MEMO[key]


class PlayByPlay:
    """The play-by-play disk cache: one gzipped file per game, fetched once.

    Split out of `Query` 2026-08-29. `Query` is shared HTTP transport — a memo
    and nothing else; this is a domain store with its own on-disk layout,
    freshness contract and backfill. They were one class only because both
    touch the network.
    """

    @staticmethod
    def _pbp_path(game_pk: int) -> Path:
        # Sharded two deep: one flat directory of ~2,000 files per season is
        # workable, ten seasons of it is not.
        pk = str(int(game_pk))
        d = SHARED_DIR / "pbp" / "games" / pk[-2:]
        return d / f"{pk}.json.gz"


    @staticmethod
    def play_by_play(game_pk: int, timeout: float = 20.0, *,
                     final: bool = False) -> List[dict]:
        """`allPlays` for one game — from disk if we have it, else fetched.

        **`final` is a promise, and getting it wrong poisons the cache
        permanently.** The response carries NO game state, so a 3rd-inning game
        is indistinguishable from a finished one and would be served as the
        whole game forever. Everything reaching this from `season_game_pks` is
        Final by construction; tonight's slate is not. Raises on a network
        failure exactly as the bare `requests.get` did.
        """
        path = PlayByPlay._pbp_path(game_pk)
        if path.exists():
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    return json.load(fh).get("allPlays") or []
            except (OSError, ValueError, EOFError):
                pass                      # corrupt entry — refetch over it
        r = requests.get(f"{STATSAPI}/game/{int(game_pk)}/playByPlay",
                         timeout=timeout)
        r.raise_for_status()
        payload = r.json()
        if final:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                tmp.replace(path)         # atomic: a killed job leaves no
            except OSError:               # half-written entry to be read back
                pass                      # the cache is a nicety, not a need
        return payload.get("allPlays") or []

    @staticmethod
    def backfill_play_by_play(pks: Sequence[int], workers: int = 12,
                              timeout: float = 20.0, *,
                              check: bool = False) -> dict:
        """Fetch every game in `pks` that is not already on disk. Once.

        The point of the whole cache: after this runs, no consumer ever issues
        a play-by-play request again. It is resumable by construction — each
        game is written atomically, so an interrupted run simply leaves fewer
        games missing and the next run picks up exactly there.

        `check=True` reports and fetches NOTHING — the same count the fetch
        would act on, from the same line of code, so the dry run cannot drift
        from the real one. `missing` is on the report either way.

        `pks` MUST be completed games; see `play_by_play`.
        """
        pks = [int(x) for x in pks]
        todo = [pk for pk in pks if not PlayByPlay._pbp_path(pk).exists()]
        have = len(pks) - len(todo)
        report = {"asked": len(pks), "had": have, "missing": todo,
                  "fetched": 0, "failed": 0}
        if check:
            return report
        Archive._progress(f"[pbp] {len(pks)} games, {have} cached, "
                          f"{len(todo)} to fetch")
        if not todo:
            return report
        done = failed = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            def one(pk):
                try:
                    PlayByPlay.play_by_play(pk, timeout, final=True)
                    return True
                except Exception:                          # noqa: BLE001
                    return False                           # leave it missing
            for ok in ex.map(one, todo):
                done += 1
                failed += (not ok)
                if done % 200 == 0 or done == len(todo):
                    Archive._progress(f"[pbp] {done}/{len(todo)} fetched, "
                                      f"{failed} failed")
        report.update(fetched=done - failed, failed=failed)
        return report


# ---------------------------------------------------------------------------
# The upstreams, one class each
# ---------------------------------------------------------------------------
# Every endpoint this module talks to, plus the per-source timeouts that used to
# be five unexplained literals at the call sites. sim_state.md A.0.


class StatsApi:
    """MLB StatsAPI — schedules, stats splits, play-by-play, live feed."""

    BASE = "https://statsapi.mlb.com/api/v1"
    TIMEOUT = 20.0
    SLOW_TIMEOUT = 90.0          # season-length schedule spans

    @staticmethod
    def schedule_url(*, date: Optional[str] = None,
                     start: Optional[str] = None, end: Optional[str] = None,
                     hydrate: Optional[str] = None,
                     game_type: Optional[str] = None) -> str:
        """A /schedule URL. Omitted arguments are omitted from the query.

        `game_type` is OPTIONAL on purpose: `fetch_probables` deliberately does
        not send it, because tonight's card includes games a `gameType=R`
        filter would drop. Defaulting it here would silently narrow that call.
        """
        q = ["sportId=1"]
        if game_type:
            q.append(f"gameType={game_type}")
        if date:
            q.append(f"date={date}")
        if start:
            q.append(f"startDate={start}")
        if end:
            q.append(f"endDate={end}")
        if hydrate:
            q.append(f"hydrate={hydrate}")
        return f"{StatsApi.BASE}/schedule?" + "&".join(q)


class Savant:
    """Baseball Savant — the CSV search endpoint and the leaderboards."""

    CSV = "https://baseballsavant.mlb.com/statcast_search/csv"
    LEADERBOARD = "https://baseballsavant.mlb.com/leaderboard"
    OAA_URL = LEADERBOARD + "/outs_above_average"
    ARM_URL = LEADERBOARD + "/arm-strength"
    FRAMING_URL = LEADERBOARD + "/catcher-framing"
    TIMEOUT = 40.0
    # One hitter's batted-ball detail. `{season}` and `{pid}` are filled by the
    # caller; `scrape_framing` binds this by name, so the module-level alias
    # below is part of the contract, not a convenience.
    DETAIL_URL = (
        "https://baseballsavant.mlb.com/statcast_search/csv"
        "?hfPT=&hfAB=&hfGT=R%7C&hfPR=&hfZ=&hfStadium=&hfBBL=&hfNewZones=&hfPull="
        "&hfC=&hfSea={season}%7C&hfSit=&player_type=batter"
        "&hfOuts=&hfOpponent=&pitcher_throws=&batter_stands=&hfSA=&min_pitches=0"
        "&min_results=0&group_by=name&sort_col=pitches"
        "&player_event_sort=api_p_release_speed&sort_order=desc&min_abs=0"
        "&type=details&player_id={pid}"
    )


class FanGraphs:
    """FanGraphs leaderboards. The board paths are appended to the site root
    by `_fg_rows`, which drives a browser — FanGraphs does not serve these to
    a bare `requests` session."""

    SPLITS_URL = "https://www.fangraphs.com/api/leaders/splits/splits-leaders"
    TIMEOUT = 40.0
    SPLIT_VS_LHP = 1
    SPLIT_VS_RHP = 2
    SPLIT_STANDARD = "1"     # G PA AB H 1B 2B 3B HR R RBI BB IBB SO HBP SB..
    SPLIT_BATTED = "3"       # PA GB/FB LD% GB% FB% IFFB% HR/FB Pull% ...
    ASOF_PATH = ("/api/leaders/major-league/data?age=&pos=all&stats={stats}"
                 "&lg=all&qual=0&season={season}&season1={season}&ind=0&type=8"
                 "&pageitems=5000&pagenum=1"
                 "&month=1000&startdate={start}&enddate={end}")
    # The same board WITHOUT a date window. `month=0` is the full season, which
    # is what `fg_{bat,pit}_<season>.json` on disk are.
    SEASON_PATH = ("/api/leaders/major-league/data?age=&pos=all&stats={stats}"
                   "&lg=all&qual=0&season={season}&season1={season}&ind=0"
                   "&type=8&pageitems=5000&pagenum=1&month=0")


class OddsPortal:
    """oddsportal.com. The REQUESTS go through `OddsPortalClient` one level
    up — an imported module, not ours to change — so what lives here is only
    what this file constructs: the league paths and the token pattern.

    `AJAX_URL_RE` was defined 13 lines BELOW its first use; that worked only
    because the use is inside a function body. Here it is defined before
    anything reads it.
    """

    LEAGUE_PATH = {"baseball": "/baseball/usa/mlb/"}
    RESULTS_PATH = {"baseball": "/baseball/usa/mlb/results/"}
    AJAX_URL_RE = re.compile(r'"ajaxUrl"\s*:\s*"([^"]+)"')


class InsideThePen:
    """insidethepen.com — the real per-team bullpen state. Needs a login."""

    BASE = "https://insidethepen.com"
    TEAM_URL = BASE + "/team/{abbr}-bullpen.html"
    TIMEOUT = 25.0


class Rotowire:
    """Rotowire's expected lineups, for the hours before the real ones post."""

    LINEUPS_URL = "https://www.rotowire.com/baseball/daily-lineups.php"
    TIMEOUT = 20.0


class OpenMeteo:
    """Open-Meteo. NOTE the FORWARD forecast is `weatherman`'s, not ours —
    this module only reaches the PREVIOUS-RUNS archive, which answers "what
    did the forecast say N days before a game that has already been played"
    and is a look-ahead control for the backtest."""

    PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
    TIMEOUT = 90.0


# **Kept at module level because other files bind them BY NAME.**
# `mlb_ml` builds a play-by-play URL off `m.STATSAPI`, and `scrape_framing`
# uses both. Neither is ours to edit, so these two spellings are part of the
# contract rather than a convenience. Everything else moved onto the classes.
STATSAPI = StatsApi.BASE
SAVANT_DETAIL_URL = Savant.DETAIL_URL
# and this one is asserted BY NAME in the suite —
# `test_the_live_board_can_see_a_game_that_has_ALREADY_FINISHED` reads
# `m._OP_RESULTS_PATH` to prove the results page is still consulted.
_OP_RESULTS_PATH = OddsPortal.RESULTS_PATH


# ---------------------------------------------------------------------------
# 1. Outcome space
# ---------------------------------------------------------------------------
# Nine outcomes, matching arXiv:2511.17733. Ground and air outs are split on
# purpose: double plays, sacrifice flies, and because our OAA/defence data is
# itself split ground vs air (FieldDefenseView shades inner ring = GB).

K, BB, HBP, GB_OUT, AIR_OUT, S1B, S2B, S3B, HR = range(9)
N_OUTCOMES = 9

OUTCOME_NAMES = ("K", "BB", "HBP", "GB_OUT", "AIR_OUT", "1B", "2B", "3B", "HR")

# Outcomes that end the plate appearance with an out recorded by the defence.
_OUT_OUTCOMES = (K, GB_OUT, AIR_OUT)
# Outcomes that are at-bats (PA minus walks and HBP; sac flies handled below).
_NOT_AB = (BB, HBP)

# League baseline, per plate appearance. MEASURED off the 2026 FanGraphs
# batting board by `league_baseline()` (section 9) — not hand-set. The pitching
# board, a different row set over three seasons, independently reproduces it
# to within 0.0008 on every outcome, which is what says the derivation is
# right rather than merely self-consistent.
#
# Refresh with `python mlb_sim.py rates` and paste the printed row back here.
LEAGUE_BASELINE: Tuple[float, ...] = (
    0.2210,   # K
    0.0893,   # BB
    0.0114,   # HBP
    0.2120,   # GB_OUT
    0.2500,   # AIR_OUT
    0.1412,   # 1B
    0.0410,   # 2B
    0.0036,   # 3B
    0.0305,   # HR
)

# ---------------------------------------------------------------------------
# 2. Base-running constants — COARSE ON PURPOSE (see module docstring)
# ---------------------------------------------------------------------------

# MEASURED off 700 games of play-by-play runner movement, not assumed — and a
# runner who HOLDS generates no movement record, so the four rates below it are
# counted as OUTCOMES by `collect_baserunning` (section 15b) instead. The
# hand-set values survive as the fallback, so a missing cache degrades to the
# shipped model rather than to zero. sim_state.md A.2.
#
#   python mlb_sim.py baserunning --refresh     # rebuild the cache
#   python mlb_sim.py baserunning               # measured against shipped


# **The season the engine defaults to, in ONE place.** It used to be written
# into 64 signatures as `season: int = 2026`; they now take `Optional[int] =
# None` and resolve in the BODY, because a default argument binds at IMPORT and
# would not see the rebinding every calibration and pool worker relies on. See
# `test_no_tunable_constant_is_captured_as_a_DEFAULT_ARGUMENT`, sim_state.md A.2.
CURRENT_SEASON = 2026


# Which season's base-running the constants below are read from. Like
# `DEPLOY_SEASON`, this exists so a backtest of an earlier year is not run on a
# later year's league — though unlike deployment these rates are close to flat
# across seasons, so it is a smaller exposure than the hook curve's. Declared
# HERE rather than beside the collector in section 15b, because a second copy
# of the number is a second thing to forget to change.
BASERUN_SEASON = 2026


def _measured_baserunning(season: Optional[int] = None) -> Dict[str, float]:
    """The `rates` block of `Sims/savedata/baserunning_<season>.json`, or {}.

    Defined HERE, 1,800 lines above `SAVE_DIR`, and building its path the same
    way `SAVE_DIR` does rather than waiting for it. These constants are read on
    `advance()`'s hot path and have to hold their measured values before
    anything downstream can capture a shipped one as a default argument — the
    frozen-default trap section 8 records twice.
    """
    season = BASERUN_SEASON if season is None else int(season)
    try:
        with open(_SIM_ROOT / "savedata"
                  / f"baserunning_{season}.json") as fh:
            got = (json.load(fh) or {}).get("rates") or {}
        return {str(k): float(v) for k, v in got.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


_MEASURED_RUN = _measured_baserunning()

P_FIRST_TO_THIRD_ON_1B = 0.360  # measured 947/2632; was 0.27, 9 points low
P_SECOND_SCORES_ON_1B = 0.624   # measured 824/1320
P_FIRST_SCORES_ON_2B = 0.423    # measured 322/761
# air out scores the runner from 3rd, <2 outs. The engine's AIR_OUT lumps
# popups and line-drive outs in with fly balls, so this is measured over that
# same population and is LOWER than a fly-ball-only reading (0.71) would
# suggest. Using the fly-ball number here would score runners off infield
# popups.
P_SAC_FLY = _MEASURED_RUN.get("sac_fly", 0.50)
# GB out doubles off the runner on 1st, <2 outs
P_GIDP = _MEASURED_RUN.get("gidp", 0.30)
# unforced runner takes the next base on a GB out. This is one rate over two
# populations that are not close — with a man also on first the play goes to
# the batter and the runner walks to third, without one he can be the play —
# and the measured split is in the cache under `gb_advance_forced`.
P_GB_ADVANCE = _MEASURED_RUN.get("gb_advance", 0.45)
# runner on 3rd scores on a GB out, <2 outs
P_GB_SCORES = _MEASURED_RUN.get("gb_scores", 0.45)

# Reached on error. There is no error OUTCOME — the rate source counts a ROE
# inside `PA - SO - BB - HBP - H`, so without this the engine turns roughly half
# a baserunner per team-game into an out and pays for it twice. Lands league
# scoring on 4.41 R/G against a real 4.40. sim_state.md A.2.
P_REACH_ON_ERROR = 0.038

# "Free" advancement — everything that moves a runner without a batted ball.
# Modelling none of it left run expectancy short by up to 0.40 runs in exactly
# the states with the most runners. Both constants are calibrated against our
# own measured RE24; re-fit them before trusting a changed advancement model.
# Steals are an ATTEMPT with a success rate, never free bases: 0.096 gives ~0.94
# attempts, ~0.73 steals and ~0.21 caught per team-game. sim_state.md A.2.
P_STEAL_ATTEMPT = 0.096         # runner on 1st, 2nd unoccupied
# **5.6c called this one "known WRONG" and it is not** — the league rate
# measures 0.782 against the 0.78 here. What is wrong is the SIMULATED 0.86, and
# this is only the fallback for a runner with no board profile; the fix is the
# battery, not this number. sim_state.md A.2.
P_STEAL_SUCCESS = _MEASURED_RUN.get("steal_success", 0.78)
# All runners move up one; man on third scores. 17.38 runner-on PAs per
# team-game puts this at ~0.38 events, against a real WP+PB+balk rate of ~0.40.
P_WILD_ADVANCE = 0.022

# ---------------------------------------------------------------------------
# 3. Rate estimation — shrinkage and recency
# ---------------------------------------------------------------------------
# Per-outcome stabilisation points, in plate appearances: the sample at which a
# player's own rate and the prior carry equal weight. K and BB stabilise fast,
# the batted-ball outcomes slowly, which is the whole reason a flat "min PA"
# gate is wrong. SPLIT BY SIDE 2026-08-15 — one shared table was the HITTER
# column applied to both, over-trusting a pitcher's own HR rate 3-4x and his
# contact outcomes 2-6x, which is SIERA's thesis arriving as a measurement.
# Derivation, both estimators and the full table: sim_state.md A.3.
STABILIZE_MAX = 3000.0    # a measured 31,732 is "no skill"; the cap says so
                          # without pretending to that precision

# **The MEASURED tables.** Geometric mean of a within-season and a cross-season
# estimator that bracket the truth from opposite sides, capped at STABILIZE_MAX.
# They did NOT ship at first — put in during August they made the model predict
# WORSE, because the decomposition measures TALENT while prediction wants talent
# plus the context that recurs. A SEQUENCING problem, not a wrong measurement,
# and the reusable lesson. Do not reach for stabilisation as a level knob.
# sim_state.md A.3.
STABILIZE_PA_MEASURED_BAT: Tuple[float, ...] = (
    55.0, 125.0, 250.0, 111.0, 132.0, 279.0, 2335.0, 564.0, 244.0)
STABILIZE_PA_MEASURED_PIT: Tuple[float, ...] = (
    93.0, 277.0, 519.0, 135.0, 153.0, 749.0, 1900.0, 3000.0, 634.0)

# What actually ships: one table, both sides, unchanged.
STABILIZE_PA: Tuple[float, ...] = (
    60.0,    # K
    120.0,   # BB
    240.0,   # HBP
    80.0,    # GB_OUT
    80.0,    # AIR_OUT
    290.0,   # 1B
    350.0,   # 2B
    380.0,   # 3B
    170.0,   # HR
)

# **SHIPPED 2026-08-16, and the earlier refusal is why it works now.** Once
# framing, park run factors and team defence had all landed, the split HELPED:
# on 3,856 games, pooled model-vs-market t -1.24 -> -0.77, ROI -2.8% -> -1.5%.
# sim_state.md A.3.
STABILIZE_PA_BAT: Tuple[float, ...] = STABILIZE_PA_MEASURED_BAT
STABILIZE_PA_PIT: Tuple[float, ...] = STABILIZE_PA_MEASURED_PIT


def stabilize_for(side: str) -> Tuple[float, ...]:
    """The stabilisation table for one side. `side` is "bat" or "pit"."""
    return STABILIZE_PA_PIT if side == "pit" else STABILIZE_PA_BAT


def recency_weights(n: int, half_life: float = 500.0) -> List[float]:
    """Exponential-decay weights over `n` plate appearances, oldest first.

    `half_life=500` reproduces arXiv:2511.17733's schedule closely. Windows come
    from differencing the as-of boards (`board_windows`, `recency_counts`).

    **Measured NULL on both sides and OFF by default** — 3d.9's hitter-only
    reading was superseded by 4d and bounded by 5.23; `RECENCY_HALF_LIFE_PIT`
    is 0.0 and `USE_RECENCY` is False. sim_state.md 4d, 5.23 and A.20.
    """
    if n <= 0:
        return []
    decay = math.log(2.0) / half_life
    # index 0 is the OLDEST plate appearance, so age counts down from n-1.
    return [math.exp(-decay * (n - 1 - i)) for i in range(n)]


def weighted_counts(outcomes: Sequence[int],
                    half_life: float = 500.0) -> Tuple[List[float], float]:
    """Recency-weighted outcome counts from a chronological PA sequence.

    `outcomes` is oldest -> newest, each an outcome index. Returns the
    per-outcome weighted counts and the total weight (an effective PA count).
    """
    w = recency_weights(len(outcomes), half_life)
    counts = [0.0] * N_OUTCOMES
    for oc, wt in zip(outcomes, w):
        counts[oc] += wt
    return counts, sum(w)


def shrink_rates(counts: Sequence[float],
                 league: Optional[Sequence[float]] = None,
                 stabilize: Optional[Sequence[float]] = None) -> List[float]:
    """Empirical-Bayes shrink an outcome-count vector toward the league.

    Each outcome is shrunk with its OWN stabilisation weight, so a hitter with
    200 PA is nearly fully trusted on strikeouts and barely trusted at all on
    triples. Shrinking the whole vector by one factor — the usual shortcut —
    gets both ends wrong at once.
    """
    league = LEAGUE_BASELINE if league is None else league
    stabilize = STABILIZE_PA if stabilize is None else stabilize
    n = sum(counts)
    out = []
    for i in range(N_OUTCOMES):
        obs = counts[i] / n if n > 0 else league[i]
        w = n / (n + stabilize[i]) if n > 0 else 0.0
        out.append(w * obs + (1.0 - w) * league[i])
    return _normalize(out)


def _normalize(v: Sequence[float]) -> List[float]:
    tot = sum(v)
    if tot <= 0:
        return list(LEAGUE_BASELINE)
    return [x / tot for x in v]


# ---------------------------------------------------------------------------
# 4. Matchup — log5 in log space
# ---------------------------------------------------------------------------

# How hard to apply the Morey-Cohen tail damping. 1.0 = the full `4*l*(1-l)`
# factor; 0.0 = plain log5, the standard method and what arXiv:2511.17733 uses.
#
# **1.0 was indefensible and is the defect this constant exists to fix** — at
# the home-run rate it kept 12% of a slugger's edge, compressing the model's
# game-to-game spread to 0.35 of a real slate while adding nothing to
# correlation. **Shipped 0.0 is a JUDGEMENT call, not a fitted one — say so
# before quoting it.** Two market samples disagree and neither settles it.
# sim_state.md A.4.
LOG5_TAIL_ALPHA = 0.0

# Gain on the log5 DEVIATION. 1.0 is plain log5 and is what ships.
#
# **The one lever in the engine targeted at mismatches by construction**: `dev`
# is ~0 when both sides are league-average and grows with the mismatch, so
# scaling it moves the extreme cell and leaves balanced games alone. Every other
# amplitude change in 4e moves player RATES and widens all 4,025 games.
# sim_state.md A.4.
LOG5_GAIN = 1.0


def log5(batter: Sequence[float], pitcher: Sequence[float],
         league: Optional[Sequence[float]] = None,
         tail_correction: bool = True) -> List[float]:
    """Combine a batter and pitcher outcome vector against the league.

    Odds-ratio log5 in log space, renormalised (arXiv:2511.17733). Morey & Cohen
    (JSA 2015) showed the form skews at asymmetric probabilities — the HR (~3%)
    and K (~22%) regime we price — so `tail_correction` damps it there. The
    correction shrinks the log-space DEVIATION, not the result, so it cannot
    reorder two hitters.
    """
    league = LEAGUE_BASELINE if league is None else league
    out = []
    for i in range(N_OUTCOMES):
        b, p, l = batter[i], pitcher[i], league[i]
        if b <= 0 or p <= 0 or l <= 0:
            out.append(max(b * p, 1e-12))
            continue
        # log5 in log space: log(b) + log(p) - log(l)
        dev = math.log(b) + math.log(p) - 2.0 * math.log(l)
        if tail_correction and LOG5_TAIL_ALPHA > 0:
            # 4*l*(1-l) is 1 at l=0.5 and falls toward 0 at either tail. At
            # full strength this kept 12% of a home-run edge and 1% of a
            # triples edge — the single biggest defect the engine has had.
            # Morey & Cohen's point is that the odds-ratio form OVERSHOOTS in
            # the tails, not that tail information should be discarded. A.4.
            dev *= (4.0 * l * (1.0 - l)) ** LOG5_TAIL_ALPHA
        if LOG5_GAIN != 1.0:
            dev *= LOG5_GAIN
        out.append(math.exp(math.log(l) + dev))
    return _normalize(out)


def apply_multipliers(rates: Sequence[float],
                      mult: Optional[Dict[int, float]]) -> List[float]:
    """Apply per-outcome context multipliers and renormalise.

    This is the seam for park x weather on HR, umpire CSR delta on K and BB,
    defence on the out-vs-hit split, and catcher framing. Renormalising after
    the fact means a multiplier moves the TARGETED outcome's share and takes
    the mass proportionally from everything else, which is the intended
    reading of "this park adds 20% to his home runs".
    """
    if not mult:
        return list(rates)
    out = list(rates)
    for i, m in mult.items():
        out[i] *= m
    return _normalize(out)


# ---------------------------------------------------------------------------
# 5. Pitcher fatigue and the hook — SMOOTH, no TTO step
# ---------------------------------------------------------------------------

# Mean batters-faced position over a start, i.e. the midpoint of a typical
# ~23-batter outing. Fatigue is measured RELATIVE to this, never from zero.
FATIGUE_REF_BF = 11.5

# The within-start fatigue GRADIENT, in multiplier units per batter faced.
#
# **MEASURED TO BE ZERO (2026-08-15), and it used to be 0.004** — 4.2 standard
# errors off 79,483 within-pitcher starter PAs, and not a harmless 4 sigma: it
# handed the starter a 4% bonus for the first two batters and put inning 1 at
# 0.460 against a real 0.531. What survives from the literature is the SHAPE,
# not the size (Brill/Deshpande/Wyner, arXiv:2210.06724). Kept rather than
# deleted because zero is a MEASURED VALUE here, not a term that failed.
# `mlb_sim.py calibrate-fatigue` re-derives it. sim_state.md A.5.
FATIGUE_DECLINE_PER_BF = 0.0

# Forces the per-PA fatigue call even when the shipped gradient is zero, so
# `calibrate_fatigue` can score a variant that acts somewhere other than on the
# slope. Set by that probe and nothing else — the hot loop skips the call
# entirely at a zero gradient, which is why a probe cannot simply swap the
# function out.
_FATIGUE_FORCE = False


def fatigue_multipliers(bf: int, decline_per_bf: Optional[float] = None,
                        ref_bf: Optional[float] = None) -> Dict[int, float]:
    """Continuous within-game decline, as a multiplier bundle.

    Smooth in batters faced — no step at batter 19, and there must not be one.
    **Centred on `ref_bf`, which is required, not cosmetic**: a season rate
    already contains the pitcher's own average fatigue, so an uncentred
    multiplier charges it twice. `FATIGUE_DECLINE_PER_BF` is measured at zero, so
    this returns a flat bundle unless a caller passes its own slope. A.5.
    """
    ref_bf = FATIGUE_REF_BF if ref_bf is None else float(ref_bf)
    d = 1.0 + (FATIGUE_DECLINE_PER_BF if decline_per_bf is None
               else decline_per_bf) * (bf - ref_bf)
    d = max(d, 0.5)
    return {HR: d, S1B: d, S2B: d, BB: d,
            K: 1.0 / d, GB_OUT: 1.0 / d, AIR_OUT: 1.0 / d}


# --- PITCHES, and why the hook needs them --------------------------------
# **A manager hooks on the PITCH COUNT and this engine hooked on BATTERS
# FACED**, which cannot tell 75 pitches through six from 105 through four. P/BF
# WITHIN a start has sd 0.417 against 0.148 ACROSS starters — a 7.2-batter swing
# a BF-indexed hazard is blind to.
#
# **These are the BASE means of the pre-floor draw, not the fitted values.** The
# fit (721 arm-seasons, no intercept, R2 99.47%) gives 4.891 / 6.749 / 3.243;
# the floors below truncate the left tail, so the means are solved back by fixed
# point to land POST-floor on the fitted number. sim_state.md A.5.
PITCHES_PER_K = 4.685
PITCHES_PER_BB = 6.678
PITCHES_PER_BIP = 3.108      # ball in play, plus HBP


# **PER-START FRAILTY on the hook, which is what the deep-start tail needs.** A
# marginal hazard applied independently at each batter gives every start the
# average pull probability, so the survival product decays too fast to reach 27
# outs. Real deep starts come from a LATENT state — unobserved heterogeneity,
# treated the standard way, one lognormal draw per start. Score is deliberately
# NOT a second dimension (conditioned on depth the hook count is FLAT).
#
# No single value fits every target: 0.40 lands complete games and the innings
# level, at the cost of the marginal spread. Ships OFF until a price says
# otherwise, like everything else here. sim_state.md A.5.
HOOK_FRAILTY_SD = 0.0        # 0 disables
USE_PITCH_HOOK = False       # A/B decides, like every other term here
# Starts at or below this many batters are OPENERS, which carry their own
# hazard, and must not also sit in the ordinary-starter curve.
OPENER_BF_MAX = 10
# Bucket width for the pitch hazard. One entry per pitch is 120 near-empty
# bins on 3,700 starts; five keeps each bin populated without blurring the
# 80-99 band where the decision actually lives.
PITCH_HOOK_BUCKET = 5


class Fatigue:
    """Pitcher fatigue and the hook — smooth in pitches/BF, never a TTO step."""

    @staticmethod
    def hook_hazard_pitches(pitch_distribution: Sequence[float],
                            bucket: Optional[int] = None) -> List[float]:
        """Discrete-time hazard of being pulled, indexed by PITCHES thrown.

        Same construction as `hook_hazard` one variable over, so the two cannot
        drift apart on a definition: h[k] = P(pulled in bucket k | still in).
        """
        bucket = PITCH_HOOK_BUCKET if bucket is None else int(bucket)
        if not pitch_distribution:
            return []
        idx = [int(x) // bucket for x in pitch_distribution]
        hi = max(idx)
        pulled = [0] * (hi + 2)
        for k in idx:
            pulled[k] += 1
        hazard, at_risk = [], len(idx)
        for k in range(hi + 1):
            if at_risk <= 0:
                hazard.append(1.0)
                continue
            hazard.append(pulled[k] / at_risk)
            at_risk -= pulled[k]
        hazard.append(1.0)
        return hazard

    @staticmethod
    def real_starter_pitch_hazard() -> Optional[List[float]]:
        """The league's pitch-indexed starter hook curve, off real stints.

        No `save_dir` default on purpose: `STINT_CACHE` is defined further down the
        module, and a module constant captured as a default argument is bound at
        IMPORT — the frozen-default trap this file records twice (section 8).
        Resolved in the body, where a rebind reaches it.
        """
        try:
            with open(STINT_CACHE) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return None
        got = [s["pitches"] for s in st if s.get("starter") and s.get("pitches")]
        return Fatigue.hook_hazard_pitches(got) if len(got) >= 500 else None

    @staticmethod
    def pa_pitches(outcome: int,
                   rng: Optional[random.Random] = None) -> float:
        """Pitches for one plate appearance. Stochastic when given an `rng`.

        Floored at 1 — every plate appearance costs at least one pitch — and at 3
        for a strikeout, which cannot happen in fewer.
        """
        if outcome == K:
            mu, lo = PITCHES_PER_K, 3.0
        elif outcome == BB:
            mu, lo = PITCHES_PER_BB, 4.0
        else:
            mu, lo = PITCHES_PER_BIP, 1.0
        if rng is None or PITCH_PA_SD <= 0:
            return mu
        return max(lo, mu + rng.gauss(0.0, PITCH_PA_SD))


_SP_HAZ: List[List[float]] = []
# The stand-in that was hardcoded at ten call sites. Kept ONLY as the fallback
# for a checkout with no stint cache; its sd is 2.49 against a real 5.12.
_FALLBACK_SP_BF = [18, 20, 21, 22, 22, 23, 23, 24, 25, 26, 27, 19, 21, 24, 20]


def starter_hazard() -> List[float]:
    """The starter hook curve every caller should use, memoised.

    Real when `reliever_stints.json` is present, the old hardcoded stand-in
    otherwise. Measured on the real slate, swapping the stand-in for the real
    curve moves simulated starter BF sd 4.63 -> 5.19 against a real 5.12 and
    IP 4.97 -> 5.03 against a real 5.10 (5.6b).
    """
    if not _SP_HAZ:
        _SP_HAZ.append(real_starter_bf_hazard() or hook_hazard(_FALLBACK_SP_BF))
    return _SP_HAZ[0]


def real_starter_bf_hazard() -> Optional[List[float]]:
    """The league's BF-indexed starter hook curve, off REAL stints.

    **The engine built this from a 15-element hardcoded list** whose sd is 2.49
    against a real 5.12 over 3,728 starts — 2.1x too tight, which is most of why
    the sim produced 27.8% four-inning starts against a real 16.0%. The real
    distribution had been on disk since 5.6; nothing read the starter half of it.
    """
    try:
        with open(STINT_CACHE) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    # **Openers are EXCLUDED.** `_game_side` already gives an opener his own
    # short `opener_hazard`, so leaving opener starts in this curve puts the
    # same short tail in twice and the sim pulls ordinary starters early.
    got = [s["bf"] for s in st
           if s.get("starter") and s.get("bf") and s["bf"] > OPENER_BF_MAX]
    return hook_hazard(got) if len(got) >= 500 else None


# **Per-PA pitch counts must be STOCHASTIC, and this is the whole reason the
# deep-start tail exists.** A deterministic cost per outcome makes a start's
# pitch count a fixed function of its outcome mix; the real within-start spread
# is 0.411 of the 0.437 total, which at ~23 batters needs a per-PA sd of ~1.96.
# Without it the hook has no latent state to condition on. sim_state.md A.5.
PITCH_PA_SD = 1.96


def hook_hazard(bf_distribution: Sequence[int]) -> List[float]:
    """Discrete-time hazard of being pulled, indexed by batters faced.

    Built straight from a club's observed per-start BF distribution — that is
    `summarize_stints()["sp_bf_list"]` in EffortMLB.py, which already carries
    the full list rather than the mean precisely so the hook CURVE survives.

    Returns h[k] = P(pulled after facing batter k | still in at k).
    """
    if not bf_distribution:
        return []
    hi = max(bf_distribution)
    pulled = [0] * (hi + 2)
    for bf in bf_distribution:
        pulled[bf] += 1
    hazard, at_risk = [], len(bf_distribution)
    for k in range(hi + 1):
        if at_risk <= 0:
            hazard.append(1.0)
            continue
        hazard.append(pulled[k] / at_risk)
        at_risk -= pulled[k]
    return hazard


# ---------------------------------------------------------------------------
# 6. Game state and the base/out Markov
# ---------------------------------------------------------------------------

@dataclass
class PlayerLine:
    """One player's accumulated line from a single simulated game."""
    pa: int = 0
    ab: int = 0
    h: int = 0
    b1: int = 0
    b2: int = 0
    b3: int = 0
    hr: int = 0
    bb: int = 0
    hbp: int = 0
    k: int = 0
    rbi: int = 0
    r: int = 0
    sf: int = 0
    sb: int = 0
    # Caught stealing was never recorded — only the successful half of the
    # running game reached the box, so a simulated base stealer looked
    # costless. `running_game` has always produced them.
    cs: int = 0

    @property
    def tb(self) -> int:
        return self.b1 + 2 * self.b2 + 3 * self.b3 + 4 * self.hr

    @property
    def hrr(self) -> int:
        """Hits + runs + RBI, the H+R+RBI market."""
        return self.h + self.r + self.rbi


@dataclass
class PitcherLine:
    bf: int = 0
    outs: int = 0
    k: int = 0
    bb: int = 0
    h: int = 0
    hr: int = 0
    r: int = 0
    """Runs charged to whoever was ON THE MOUND when they crossed. This is NOT
    earned runs: real scoring charges an inherited runner to the pitcher who
    put him on, and separates unearned runs on errors (which this engine does
    not model at all, having no error outcome). Both effects push the number
    the same way — `r` slightly overstates a reliever's ER and understates the
    starter's. `pitcher_earned_runs` is therefore the one mapped market that
    is not yet honestly priced; fix the attribution before trusting it."""

    @property
    def ip(self) -> float:
        return self.outs / 3.0


@dataclass
class HalfInningState:
    """Bases carry the batting-order index of the RUNNER, not just occupancy —
    runs have to be credited to the man who scored, and RBI to the man who
    drove him in, so occupancy bits are not enough."""
    bases: List[Optional[int]] = field(default_factory=lambda: [None, None, None])
    outs: int = 0

    def reset(self) -> None:
        self.bases = [None, None, None]
        self.outs = 0


# The base state as a BITMASK, and the ONE definition of that encoding.
# **A cross-FILE contract**: `mlb_ml.pa_rows_from_plays` writes the same bits
# into `savedata/pa/v2`, the ML residual is TRAINED on that column, and
# `simulate_game` SERVES it from here. `mlb_ml` imports this name and a test
# pins the two together — a model trained on one bit order and served another
# still returns nine plausible probabilities. sim_state.md A.6.
BASE_STATE_BITS: Tuple[int, int, int] = (1, 2, 4)     # 1B, 2B, 3B


def base_mask(bases: Sequence[Optional[int]]) -> int:
    """Occupancy bitmask from `HalfInningState.bases`.

    `is not None`, never truthiness: the bases carry the RUNNER'S batting-order
    index, and the leadoff hitter's index is 0.
    """
    return sum(v for v, b in zip(BASE_STATE_BITS, bases) if b is not None)


def advance(state: HalfInningState, outcome: int, batter: int,
            rng: random.Random,
            lineup: Optional[List["Batter"]] = None,
            arm: float = 1.0) -> Tuple[List[int], int, bool]:
    """Apply one PA outcome to the base/out state.

    Returns (scorers, rbi, is_sac_fly) where `scorers` is the list of
    batting-order indices that crossed the plate.

    Every advancement decision is taken by the RUNNER who has to make it, at
    his own speed — first-to-third on a single is a different proposition for
    the man who runs a 29 ft/s sprint than for the one who runs 26. Pass
    `lineup` to get that; omit it and every runner moves at the league rate.
    """
    b = state.bases
    scorers: List[int] = []
    rbi = 0
    sac_fly = False
    n_free = 0

    if outcome == HR:
        for r in b:
            if r is not None:
                scorers.append(r)
        scorers.append(batter)
        rbi = len(scorers) - n_free
        state.bases = [None, None, None]

    elif outcome == S3B:
        for r in b:
            if r is not None:
                scorers.append(r)
        rbi = len(scorers) - n_free
        state.bases = [None, None, batter]

    elif outcome == S2B:
        if b[2] is not None:
            scorers.append(b[2])
        if b[1] is not None:
            scorers.append(b[1])
        if b[0] is not None:
            _r0 = Markov._runner(lineup, b[0])
            if rng.random() < scale_odds((_r0.adv or {}).get("first_scores_2b")
                               if _r0 and _r0.adv else
                               scale_odds(P_FIRST_SCORES_ON_2B,
                                          _speed(lineup, b[0])), arm):
                scorers.append(b[0])
                state.bases = [None, batter, None]
            else:
                state.bases = [None, batter, b[0]]
        else:
            state.bases = [None, batter, None]
        rbi = len(scorers) - n_free

    elif outcome == S1B:
        if b[2] is not None:
            scorers.append(b[2])
        third = None
        second = None
        if b[1] is not None:
            _r1 = Markov._runner(lineup, b[1])
            if rng.random() < scale_odds((_r1.adv or {}).get("second_scores")
                               if _r1 and _r1.adv else
                               scale_odds(P_SECOND_SCORES_ON_1B,
                                          _speed(lineup, b[1])), arm):
                scorers.append(b[1])
            else:
                third = b[1]
        if b[0] is not None:
            _r2 = Markov._runner(lineup, b[0])
            _p2 = scale_odds((_r2.adv or {}).get("first_to_third")
                             if _r2 and _r2.adv else
                             scale_odds(P_FIRST_TO_THIRD_ON_1B,
                                        _speed(lineup, b[0])), arm)
            if third is None and rng.random() < _p2:
                third = b[0]
            else:
                second = b[0]
        state.bases = [batter, second, third]
        rbi = len(scorers) - n_free

    elif outcome in (BB, HBP):
        # Forced advance only — nobody moves unless the base behind them fills.
        if b[0] is None:
            state.bases = [batter, b[1], b[2]]
        elif b[1] is None:
            state.bases = [batter, b[0], b[2]]
        elif b[2] is None:
            state.bases = [batter, b[0], b[1]]
        else:
            scorers.append(b[2])
            rbi = 1
            state.bases = [batter, b[0], b[1]]

    elif outcome == K:
        state.outs += 1

    elif outcome == GB_OUT:
        if rng.random() < P_REACH_ON_ERROR:
            # Nobody is retired. Everyone moves up a base; a man on third
            # scores, and it is unearned so it carries no RBI.
            if b[2] is not None:
                scorers.append(b[2])
            state.bases = [batter, b[0], b[1]]
        elif (b[0] is not None and state.outs < 2
                and rng.random() < scale_odds(P_GIDP,
                                              1.0 / _speed(lineup, b[0]))):
            # Force at second plus the batter at first. Runners already in
            # scoring position hold.
            state.outs += 2
            state.bases = [None, b[1], b[2]]
        else:
            state.outs += 1
            if state.outs < 3:
                # The PRODUCTIVE OUT. Leaving it out — every runner simply
                # holding — stranded enough men to cost ~0.4 runs a game
                # against a league-average lineup, which is a tenth of the
                # scoring environment and would have mispriced every RBI,
                # runs-scored and team-total market in the same direction.
                first, second, third = b
                new_third = third
                if third is not None and rng.random() < scale_odds(
                        P_GB_SCORES, _speed(lineup, third)):
                    scorers.append(third)
                    rbi = 1
                    new_third = None
                if second is not None and new_third is None \
                        and rng.random() < scale_odds(
                            P_GB_ADVANCE, _speed(lineup, second)):
                    new_third, second = second, None
                if first is not None:
                    # Force at second: the lead runner is erased and the
                    # batter is safe at first.
                    state.bases = [batter, second, new_third]
                else:
                    state.bases = [None, second, new_third]
            else:
                state.bases = [None, None, None]

    elif outcome == AIR_OUT:
        state.outs += 1
        if (b[2] is not None and state.outs < 3
                and rng.random() < scale_odds(P_SAC_FLY,
                                              _speed(lineup, b[2]))):
            scorers.append(b[2])
            rbi = 1
            sac_fly = True
            state.bases = [b[0], b[1], None]

    return scorers, rbi, sac_fly


def scale_odds(p: float, factor: float) -> float:
    """Scale a probability by `factor` in ODDS space, so it cannot leave [0,1].

    Multiplying a probability directly is what breaks when a fast runner meets
    an already-likely advance: 0.8 * 1.5 = 1.2. In odds space the same
    multiplier is well behaved at both ends and still means "1.5x as likely".
    """
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    o = (p / (1.0 - p)) * factor
    return o / (1.0 + o)


class Markov:
    """Helpers for the base/out Markov: who is running, and booking a PA."""

    @staticmethod
    def _runner(lineup: Optional[List["Batter"]], slot: Optional[int]
                ) -> Optional["Batter"]:
        if lineup is None or slot is None:
            return None
        return lineup[slot % len(lineup)]

    @staticmethod
    def _record(line: PlayerLine, outcome: int, rbi: int, sac_fly: bool) -> None:
        line.pa += 1
        line.rbi += rbi
        if outcome not in _NOT_AB and not sac_fly:
            line.ab += 1
        if sac_fly:
            line.sf += 1
        if outcome == K:
            line.k += 1
        elif outcome == BB:
            line.bb += 1
        elif outcome == HBP:
            line.hbp += 1
        elif outcome == S1B:
            line.h += 1
            line.b1 += 1
        elif outcome == S2B:
            line.h += 1
            line.b2 += 1
        elif outcome == S3B:
            line.h += 1
            line.b3 += 1
        elif outcome == HR:
            line.h += 1
            line.hr += 1

    @staticmethod
    def draw_outcome(rates: Sequence[float], rng: random.Random) -> int:
        u = rng.random()
        acc = 0.0
        for i, p in enumerate(rates):
            acc += p
            if u < acc:
                return i
        return AIR_OUT


def _speed(lineup: Optional[List["Batter"]], slot: Optional[int]) -> float:
    r = Markov._runner(lineup, slot)
    return r.speed if r is not None else 1.0


def running_game(state: HalfInningState, rng: random.Random,
                 lineup: Optional[List["Batter"]] = None) -> List[int]:
    """Steals, wild pitches and passed balls, resolved BETWEEN plate appearances.

    Outside `advance()` because a caught stealing can be the third out, and then
    the batter never completes his PA — he leads off the next inning. Returns
    `(scorers, events)`; those runs carry NO RBI. **The events are not
    decoration**: inferring a steal from the state change booked every wild pitch
    with a runner on first as one. sim_state.md A.7.
    """
    if not any(r is not None for r in state.bases):
        return [], []
    scorers: List[int] = []
    events: List[dict] = []
    b = state.bases
    if rng.random() < P_WILD_ADVANCE:
        if b[2] is not None:
            scorers.append(b[2])
        state.bases = [None, b[0], b[1]]
        events.append({"kind": "WP",
                       "runners": [x for x in b if x is not None],
                       "scored": ([b[2]] if b[2] is not None else [])})
    elif b[0] is not None and b[1] is None:
        # The man ON FIRST decides whether to go, and how often he makes it.
        runner = Markov._runner(lineup, b[0])
        attempt = runner.steal_attempt if runner else P_STEAL_ATTEMPT
        success = runner.steal_success if runner else P_STEAL_SUCCESS
        if rng.random() < attempt:
            if rng.random() < success:
                state.bases = [None, b[0], b[2]]
                events.append({"kind": "SB", "runner": b[0]})
            else:
                state.bases = [None, b[1], b[2]]
                state.outs += 1
                events.append({"kind": "CS", "runner": b[0]})
    return scorers, events


# ---------------------------------------------------------------------------
# 7. The lineup, the staff, and one simulated game
# ---------------------------------------------------------------------------

@dataclass
class Batter:
    name: str
    rates: List[float]                     # shrunk, recency-weighted
    player_id: Optional[int] = None
    # --- the running game, PER PLAYER ---
    # League marks so a Batter built without them still simulates; fill from
    # `runner_profile` (section 9). A league-constant running game is wrong in
    # both directions at once, worst where it is most bettable. sim_state.md A.7.
    # `hand` is the RAW board value, so "B" for a switch hitter, not "S" — see
    # `_bat_hand`, and do not normalise it here (mlb_ml reads this field).
    bats: str = ""                         # "L", "R" or "B"
    steal_attempt: float = P_STEAL_ATTEMPT
    steal_success: float = P_STEAL_SUCCESS
    speed: float = 1.0                     # odds multiplier on taking a base
    # His OWN advancement rates, blended from PBP history + XBR + speed by
    # `runner_advance_rates`. None falls back to the league constants.
    adv: Optional[Dict[str, float]] = None
    # Per-outcome context for THIS hitter in TONIGHT's conditions. Applied
    # AFTER log5, because folding it into his rates first would let the log5
    # tail correction damp it as though it were a skill claim. The park x
    # weather HR term used to live here and was REMOVED 2026-08-15, measured
    # worse than nothing; do not reintroduce one without a measurement that
    # beats leaving it out. sim_state.md A.7.
    context: Optional[Dict[int, float]] = None


@dataclass
class Pitcher:
    name: str
    rates: List[float]
    player_id: Optional[int] = None
    hazard: List[float] = field(default_factory=list)   # by batters faced
    # ...and by PITCHES, which is what a manager actually hooks on (5.6b).
    # Empty means fall back to the BF curve, so an arm without one behaves
    # exactly as before.
    pitch_hazard: List[float] = field(default_factory=list)
    is_starter: bool = False
    # --- bullpen role ---
    # gmLI is FanGraphs' game leverage index: the average leverage of the
    # situations a manager actually brings him into. It is the direct
    # measurement of the thing we need — WHO gets the ball when it matters —
    # so the pen is ordered by it rather than by innings or saves.
    gm_li: float = 1.0
    # Rest state carried in from outside (recent workload). 1.0 = fully
    # available, 0.0 = unavailable tonight.
    availability: float = 1.0
    # Long men absorb innings in blowouts instead of being burned one at a
    # time; inferred from innings per outing, not labelled.
    multi_inning: bool = False
    # How often he pitches AT ALL (G / team games). The league max is 0.534
    # and the median 0.108 — an arm simulated above ~0.5 is wrong by
    # construction, which is what happens with no availability model at all.
    app_rate: float = 0.35
    # How long he stays once he is in (TBF per outing).
    bf_per_outing: float = 4.0
    # insidethepen deployment traits — the manager's actual decision inputs.
    # `avg_inning` is the single most direct one: every identified closer in
    # the league reads 9.0, setup men 8.0, middle relief 6-7.
    avg_inning: Optional[float] = None
    avg_run_diff: Optional[float] = None
    back_to_back: Optional[float] = None
    # Share of his appearances that are saves. A closer only pitches when his
    # team is AHEAD — that constraint, not leverage, is what caps his usage
    # near 43%: he simply does not appear in the games his team is losing.
    save_share: float = 0.0
    throws: str = ""                       # "L" or "R"


@dataclass
class TeamSide:
    lineup: List[Batter]                   # 9, in batting order
    starter: Pitcher
    bullpen: List[Pitcher]                 # in the order the manager reaches
    # Team defence behind the pitcher. `oaa` is season outs above average
    # (league sd ~21.5); `of_arm` is mean outfield arm in mph (league 87.7,
    # sd 1.93). Both from `load_team_defense()`.
    oaa: float = 0.0
    of_arm: Optional[float] = None
    # Club run differential per game, SEASON-TO-DATE and already shrunk. The
    # engine is built strictly bottom-up — nine hitters, a starter, a pen — so
    # it has no way to express a club being better than the sum of its parts.
    # See `TEAM_QUALITY_GAIN`. 0.0 leaves the model exactly as it was.
    team_quality: float = 0.0
    # Catcher framing in runs PER GAME, applied to the OPPOSING lineup —
    # unlike the umpire, framing belongs to ONE side and does not cancel within
    # a game. **Per CATCHER when one is known, per CLUB otherwise**: a club
    # aggregate carries the framing of men who have left. sim_state.md A.7.
    framing: float = 0.0
    catcher_id: Optional[int] = None


@dataclass
class GameResult:
    batters: Dict[str, PlayerLine]
    pitchers: Dict[str, PitcherLine]
    runs_home: int = 0
    runs_away: int = 0
    # Runs in each HALF-INNING, in order. The engine draws every PA
    # independently and the form draw is per team-GAME, so it has no mechanism
    # for an inning getting away from a pitcher. Whether that leaves the upper
    # tail too thin is measurable, and was not being measured. A.6.
    half_runs_home: List[int] = field(default_factory=list)
    half_runs_away: List[int] = field(default_factory=list)


# --- Leverage, MEASURED from our own play-by-play ------------------------
# Nothing here is a chosen number: leverage is read from
# `savedata/pbp/season_2026_v2.json` and the thresholds are QUANTILES of that
# table's own distribution. State key matches EffortMLB's `we_key` and is
# deliberately coarse — one season cannot support the full grid. A.7.

_LI_TABLE: Optional[Dict[tuple, float]] = None
_LI_QUANTILES: Optional[Tuple[float, float]] = None
MIN_STATE_N = 30          # transitions before a state's leverage is believed


class Leverage:
    """Leverage index and the platoon weight that rides on it."""

    @staticmethod
    def load_leverage_table(path: Optional[Path] = None
                            ) -> Tuple[Dict[tuple, float], Tuple[float, float]]:
        """Leverage per game state, derived from the season's real transitions.

        LI(s) = E|WE(s') - WE(s)| over the transitions actually observed out of
        s, normalised to a league mean of 1. Returns the table and its own
        (median, p75), which is what the bullpen logic thresholds on — so the
        cut-offs move with the data instead of being chosen.
        """
        global _LI_TABLE, _LI_QUANTILES
        if _LI_TABLE is not None:
            return _LI_TABLE, _LI_QUANTILES
        path = path or (SHARED_DIR / "pbp" / "season_2026_v2.json")
        table: Dict[tuple, float] = {}
        try:
            with open(path) as fh:
                d = json.load(fh)
            we = {tuple(k): v[0] / v[1] for k, v in d["we_acc"] if v[1] >= 20}
            trans: Dict[tuple, list] = {}
            for a, b, n in d["we_trans"]:
                trans.setdefault(tuple(a), []).append((tuple(b), n))
            raw = {}
            for st, outs in trans.items():
                if st not in we:
                    continue
                tot = sum(n for _, n in outs)
                if tot < MIN_STATE_N:
                    continue
                raw[st] = sum(n * abs(we.get(t, we[st]) - we[st])
                              for t, n in outs) / tot
            if raw:
                mean = sum(raw.values()) / len(raw)
                table = {k: v / mean for k, v in raw.items()}
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            table = {}
        vals = sorted(table.values())
        _LI_TABLE = table
        _LI_QUANTILES = ((vals[len(vals) // 2], vals[3 * len(vals) // 4])
                         if vals else (0.85, 1.45))
        return _LI_TABLE, _LI_QUANTILES

    @staticmethod
    def game_leverage(inning: int, is_top: bool, lead: int, on_base: int,
                      outs: int) -> float:
        """Leverage of the current state, looked up in the measured table.

        `lead` is from the PITCHING side's perspective. Falls back along the axes
        the table is thinnest on — blowouts and deep extras — before giving up and
        returning the league mean.
        """
        table, (med, _) = Leverage.load_leverage_table()
        if not table:
            return 1.0
        inn = min(int(inning), 10)
        ld = max(-4, min(4, int(lead)))
        for key in ((inn, bool(is_top), ld, int(on_base), int(outs)),
                    (inn, bool(is_top), ld, int(on_base), 1),
                    (inn, bool(is_top), ld, 0, int(outs)),
                    (min(inn, 9), bool(is_top), ld, 0, 1)):
            if key in table:
                return table[key]
        # Blowout and deep-extra states are the thinnest cells and often missing.
        # Falling back to the league MEDIAN there is wrong in the direction that
        # matters most: it would tell the manager a 9-run game is an average
        # situation and burn the closer in it. Walk out to the nearest lead the
        # table does hold, keeping the sign, so a blowout stays a blowout.
        same = [(abs(k[2] - ld), k) for k in table
                if k[0] == inn and k[1] == bool(is_top)
                and (k[2] >= 0) == (ld >= 0)]
        if same:
            return table[min(same)[1]]
        return med

    @staticmethod
    def platoon_weight(avg_inning: Optional[float]) -> float:
        """Interpolated platoon-seeking strength for an arm, 0 when unknown."""
        if not avg_inning:
            return 0.0
        xs = PLATOON_LIFT
        if avg_inning <= xs[0][0]:
            return xs[0][1]
        if avg_inning >= xs[-1][0]:
            return xs[-1][1]
        for (x0, y0), (x1, y1) in zip(xs, xs[1:]):
            if x0 <= avg_inning <= x1:
                f = (avg_inning - x0) / (x1 - x0)
                return y0 + f * (y1 - y0)
        return 0.0


# How hard a manager chases the platoon, as a function of the arm's measured
# entry inning. MEASURED off 2,606 real pitching changes:
#
#   avg entry inning 6 -> +20.2    7 -> +14.5    8 -> -3.0    9 -> -19.5
#
# Middle relievers ARE matchup pieces. **Closers are not** — they enter on the
# inning regardless of who is due up, hence the NEGATIVE lift at 9. A.7.
PLATOON_LIFT = ((6.0, 0.202), (7.0, 0.145), (8.0, -0.030), (9.0, -0.195))


def _choose_reliever(side: TeamSide, used: set, lev: float,
                     rng: random.Random,
                     ready: Optional[set] = None,
                     inning: Optional[int] = None,
                     run_diff: Optional[int] = None,
                     bat_hand: str = "",
                     is_home: bool = False) -> Optional["Pitcher"]:
    """Which arm comes in, from MEASURED deployment traits, not a rank order.

    `avg_inning` is the inning insidethepen records him actually entering (every
    identified closer reads 9.0, setup 8.0, middle 6-7) — a direct observation of
    the manager's decision, not a proxy. `avg_run_diff` is the margin he is
    trusted in, which keeps a closer out of a blowout with no leverage threshold;
    `gm_li` is the tiebreak. An arm with no traits falls back to gmLI alone.
    """
    avail = [p for p in side.bullpen
             if p.name not in used and (not ready or p.name in ready)]
    if not avail:                      # everyone rested or burned: go anyway
        avail = [p for p in side.bullpen if p.name not in used]
    if not avail:
        return None

    def score(p: "Pitcher") -> float:
        """P(this arm enters | this state), factored the way the decision is
        actually made. Availability is a HARD GATE above; what is left is

            P(enters here) = P(he pitches at all)          <- base rate
                           x P(this inning | he pitches)   <- role
                           x P(this margin  | this inning) <- situation
                           x handedness

        **The base rate is the term that used to be missing**, which is why the
        pen read too flat: two conditional distributions say WHERE an arm is used
        and nothing about how OFTEN, so Oakland's Medina simulated 56.3% against a
        real 32.5%. sim_state.md A.20.
        """
        sc = max(p.app_rate, 0.01)
        # EMPIRICAL: how often this pitcher actually entered in this inning
        # and this score margin, from his own play-by-play history (shrunk
        # toward his role when thin). No formula reproduces "8th 14%, 9th 84%,
        # never a 6th" as cleanly as reading it off.
        sc *= deployment_score(p.player_id, inning or 1, run_diff or 0,
                               bool(is_home))
        # Handedness, scaled by how much THIS arm is used for matchups.
        # `platoon_weight` is negative for a closer, so a closer is not
        # passed over for a specialist in the ninth.
        if bat_hand and p.throws:
            pw = Leverage.platoon_weight(p.avg_inning)
            if pw > 0:
                same = (p.throws == bat_hand)
                sc *= (1.0 + pw) if same else max(1.0 - pw, 0.05)
        return max(sc, 1e-9)

    # **Sample proportionally, do not take the argmax.** Winner-take-all put
    # Tanner Scott in 70% of his available games against a real 43%, above the
    # league's busiest reliever. sim_state.md A.7.
    weights = [score(p) for p in avail]
    total = sum(weights)
    if total <= 0:
        return avail[rng.randrange(len(avail))]
    draw = rng.random() * total
    acc = 0.0
    for p, w in zip(avail, weights):
        acc += w
        if draw < acc:
            return p
    return avail[-1]


# **`PEN_AVAILABLE_BOOST` RETIRED 2026-08-15 — it was double-counting
# `app_rate`.** Once the base rate was correctly added to `_choose_reliever`'s
# score, `app_rate` entered the decision twice and suppressed a marginal arm
# roughly quadratically, taking innings off ranks 8-13 — the worst innings in a
# bullpen, so the league run environment came out too low. REMOVED rather than
# set to 1.0: a neutralised knob is dead code with a switch on it. A.7.

# Rest. A real pen has ~7.0 of 8 arms on hand on a given day with sd ~0.7
# (section 5.3), so this is 7/8 and is NOT a free parameter — raising it does
# not make arms pitch more often, because the selection score is normalised
# over whoever is available. It only decides how often the pen is short.
PEN_AVAILABLE_P = 0.875

# **`ENTRY_INNING_SCALE` and `ENTRY_DIFF_SCALE` were REMOVED 2026-08-23** on
# that same precedent — they read as fitted and load-bearing and reached
# nothing, orphaned when `deployment_score`'s empirical histograms replaced the
# hand-tuned entry scorer. sim_state.md A.7, §5.12.


@dataclass
class MoundState:
    """Who is pitching for one side, and who has already been burned.

    `available` is drawn ONCE per game from each arm's real appearance rate.
    Without it every reliever is fresh in every game and usage runs to 80%,
    against a real league maximum of 53%.
    """
    current: Optional["Pitcher"] = None
    used: set = field(default_factory=set)
    available: set = field(default_factory=set)
    inning: int = 1
    run_diff: int = 0
    bat_hand: str = ""
    is_home: bool = False
    # per-START hook frailty, drawn once and held (see HOOK_FRAILTY_SD)
    frailty: Optional[float] = None


def _mound(side: TeamSide, state: "MoundState", bf_by_pitcher: Dict[str, int],
           lev: float, inning_start: bool, rng: random.Random,
           runs_allowed: Optional[Callable[[str], int]] = None,
           pitch_by_pitcher: Optional[Dict[str, float]] = None) -> "Pitcher":
    """Who is on the mound for this plate appearance.

    Two decisions, separated because managers make them differently: the STARTER
    is pulled on a hazard over batters faced at any point in an inning, while a
    RELIEVER is almost always changed at an inning BOUNDARY having gone about an
    inning. The old rule swapped arms after exactly four batters wherever that
    fell, manufacturing mid-inning changes that do not happen. Who replaces him
    is `_choose_reliever`, keyed on the measured leverage of the state.
    """
    if state.current is None:
        state.current = side.starter
        return state.current

    cur = state.current
    if cur is side.starter:
        faced = bf_by_pitcher.get(cur.name, 0)
        # **Hook on PITCHES when we have a pitch curve.** The BF hazard cannot
        # distinguish 75 pitches through six from 105 through four, and that
        # blindness is most of the missing dispersion in simulated starter
        # length (section 5.6b). Falls back to the BF curve, which keeps every
        # existing configuration bit-identical.
        pz = getattr(cur, "pitch_hazard", None)
        if USE_PITCH_HOOK and pz and pitch_by_pitcher is not None:
            thrown = pitch_by_pitcher.get(cur.name, 0.0)
            k = int(thrown) // PITCH_HOOK_BUCKET
            h = pz[k] if k < len(pz) else 1.0
        else:
            h = cur.hazard[faced] if faced < len(cur.hazard) else 1.0
        # one frailty draw per START, held for the whole outing
        if HOOK_FRAILTY_SD > 0.0:
            if state.frailty is None:
                state.frailty = math.exp(rng.gauss(
                    -0.5 * HOOK_FRAILTY_SD ** 2, HOOK_FRAILTY_SD))
            h = min(1.0, h * state.frailty)
        if rng.random() < h:
            nxt = _choose_reliever(side, state.used, lev, rng,
                                   state.available, state.inning,
                                   state.run_diff, state.bat_hand,
                                   state.is_home)
            if nxt is not None:
                state.used.add(nxt.name)
                state.current = nxt
        return state.current

    # A reliever getting hit is pulled MID-INNING. Without it the sim can only
    # change arms at an inning boundary and an arm that cannot get outs stays in
    # forever; 14.8% of real entries arrive with inherited runners and our sim
    # was making none of them. Both scales are FITTED against the measured stint
    # shape (§5.6), not chosen — set by eye they pulled 50.6% of relievers
    # mid-inning against a real 31.1%. sim_state.md A.7.
    faced = bf_by_pitcher.get(cur.name, 0)
    if not inning_start and faced >= 2:
        line = runs_allowed(cur.name) if runs_allowed else 0
        over = max(faced - cur.bf_per_outing, 0.0)
        # Two triggers, both rising: damage done and length of the outing.
        hazard = RELIEF_PULL_DAMAGE * line + RELIEF_PULL_LENGTH * over
        if rng.random() < min(hazard, 0.8):
            nxt = _choose_reliever(side, state.used, lev, rng, state.available,
                                   state.inning, state.run_diff,
                                   state.bat_hand, state.is_home)
            if nxt is not None:
                state.used.add(nxt.name)
                state.current = nxt
            return state.current

    # A reliever hands over between innings, once he has worked one. **The
    # slack decides how many appearances span two innings**: at 1.0 a clean
    # three-batter inning did not qualify as a hand-over and the arm went back
    # out, putting 41.4% of appearances into 2+ innings against a real 30.0%.
    # Fitted against the measured shape (§5.6). sim_state.md A.7.
    if (inning_start and bf_by_pitcher.get(cur.name, 0)
            >= cur.bf_per_outing - RELIEF_HANDOVER_SLACK):
        if not (cur.multi_inning and lev < Leverage.load_leverage_table()[1][0]):
            nxt = _choose_reliever(side, state.used, lev, rng,
                                   state.available, state.inning,
                                   state.run_diff, state.bat_hand,
                                   state.is_home)
            if nxt is not None:
                state.used.add(nxt.name)
                state.current = nxt
    return state.current


# Relief-appearance shape, FITTED against 11,969 measured stints (§5.6).
# `validate_stint_shape()` re-scores them; `mlb_sim.py stints` prints it.
# Seasons to step BACK for team-level context Savant will not serve as-of.
# 0 = this season (leaks into a backtest), 1 = the prior season (leak-free).
TEAM_CONTEXT_LAG = 0

RELIEF_PULL_DAMAGE = 0.05       # mid-inning hook per run already allowed
RELIEF_PULL_LENGTH = 0.02       # ...and per batter faced beyond his usual
RELIEF_HANDOVER_SLACK = 1.6     # batters short of `bf_per_outing` that still
                                # counts as a completed outing at an inning
                                # break — bigger means shorter appearances

MAX_INNINGS = 15   # safety bound on extras; ~1 game in 5,000 reaches it


# ---------------------------------------------------------------------------
# The per-PA state vector — ONE function, so there is one definition of it
# ---------------------------------------------------------------------------
# Extracted for `mlb_ml`, not for tidiness: it trains against "the vector the
# incumbent would have produced", so a second copy would drift and put a
# constant into every residual the model then learns as skill. The ORDER is not
# arbitrary and has been wrong before. sim_state.md A.7.

# Which rate layer prices a plate appearance. "baseline" is shrinkage + log5 +
# context, the incumbent, and is what ships. The other two exist so the ML
# experiment can be an A/B ARM rather than a fork of the engine. Uppercase
# STRINGS so `_slate_overrides` can carry them into a pool worker; a callable
# could not travel and the worker would silently run the baseline. A.7.
RATE_MODEL = "baseline"           # "baseline" | "ml" | "blend"
ML_MODEL_TAG = ""                 # which trained model, by name on disk
ML_BLEND_ALPHA = 1.0              # weight on the ML vector when blending
# Centre the residual on the season being PRICED rather than on the season it
# was validated on. Diagnostic — see `mlb_ml.applied_centre` before shipping it.
ML_SELF_CENTRE = False
# Non-empty selects the HIERARCHY (`mlb_ml` section 5) instead of the flat
# nine-class model: a comma-separated node list, or "all". `ML_BLEND_ALPHA`
# then scales each node's residual in LOGIT space rather than mixing
# distributions — see the fixes memo section 3.
ML_HIER_NODES = ""

# Which LightGBM configuration the node models are fitted and served under:
# "shipped" is `mlb_ml.LGB_NODE_PARAMS`, "tuned" the search in `mlb_ml` 5b. It
# lives HERE so an A/B arm can select it and `_slate_overrides` can carry it
# into a worker (trap 6). Defaults to "shipped" because `hier25` is a RECORDED
# result and must keep meaning what it meant when measured. A.7.
ML_NODE_PARAMS = "shipped"        # "shipped" | "tuned"

# **Which per-PA STATE columns the ML residual is allowed to read.** "" is the
# incumbent — the adjuster is memoised on (batter, pitcher, side, is_starter)
# and every state column multiplies that key space.
#
# Measured 2026-08-23, both folds: ALL state is +76% of the residual's whole
# contribution and BASE-OUT alone +33% at a key-space cost of 24. `tto`
# measured +22% and is deliberately NOT offered — it is a data-driven
# re-introduction of `FATIGUE_DECLINE_PER_BF`, a DELIBERATE null, and needs its
# own control. sim_state.md A.7.
ML_STATE_COLS = ""                # "" | "baseout"


ML_MODEL_FOLD = ""                # which walk-forward fold's model


def game_adjuster(season: int, as_of: str, row: dict,
                  home: "TeamSide", away: "TeamSide",
                  save_dir: Optional[Path] = None):
    """The trained rate correction for ONE game, or None when off.

    Built ONCE per game and handed to `simulate_many`, never per plate
    appearance: a boosted model at 76 PA x 2,000 sims is four orders of magnitude
    more work than the simulation. The returned callable memoises on (batter,
    pitcher, side), so a game costs a few hundred rows rather than 152,000.

    **A PARAMETER rather than a module lookup on purpose**: `RATE_MODEL` is a
    string and travels to a forkserver worker, a callable would not — and the
    worker would silently run the baseline under the variant's name.
    """
    # **A non-baseline arm that cannot build an adjuster RAISES.** As one
    # `or not (...)` returning None, a hierarchy arm fell straight through and
    # ran the incumbent under its own name — `hier25` came out byte-identical
    # to `base` on all 1,750 games. Make the impossible state loud. A.7.
    if RATE_MODEL == "baseline":
        return None
    if not ML_MODEL_FOLD:
        raise ValueError(
            f"mlb_sim: RATE_MODEL={RATE_MODEL!r} but no ML_MODEL_FOLD. A "
            f"season may only be priced by a model whose training and "
            f"validation both end before it; refusing to guess.")
    if not (ML_MODEL_TAG or ML_HIER_NODES):
        raise ValueError(
            f"mlb_sim: RATE_MODEL={RATE_MODEL!r} but neither ML_MODEL_TAG "
            f"(flat model) nor ML_HIER_NODES (hierarchy) is set. This arm "
            f"would silently run the baseline under its own name.")
    import mlb_ml                      # deferred: mlb_ml imports THIS module
    return mlb_ml.game_adjuster(
        ML_MODEL_TAG, ML_MODEL_FOLD, season, as_of, row, home, away,
        mode=RATE_MODEL, alpha=ML_BLEND_ALPHA,
        save_dir=(save_dir if save_dir is not None else SAVE_DIR))


def pa_rates(bat: "Batter", pit: "Pitcher", *, faced: int = 0,
             oaa: float = 0.0, framing: float = 0.0, is_home: bool = False,
             tilt: float = 0.0, mult: Optional[Dict[int, float]] = None,
             ml=None, bases: int = 0, outs: int = 0) -> List[float]:
    """One plate appearance's nine-outcome distribution.

    `ml` is the optional trained correction from `_ml_adjuster`. It is applied
    LAST, on the fully composed vector, and returns MULTIPLIERS rather than a
    replacement — so tonight's form draw, which the model never saw, survives
    the correction instead of being overwritten by it.
    """
    rates = log5(platoon_rates(bat.rates, bat.bats, pit.throws), pit.rates)
    if pit.is_starter and (FATIGUE_DECLINE_PER_BF or _FATIGUE_FORCE):
        rates = apply_multipliers(rates, fatigue_multipliers(faced))
    rates = apply_defense(rates, oaa)
    if framing:
        rates = apply_multipliers(rates, framing_multipliers(framing))
    rates = apply_hfa(rates, is_home)
    rates = offence_tilt(rates, tilt)
    rates = apply_multipliers(rates, bat.context)
    rates = apply_multipliers(rates, mult)
    if ml is not None:
        rates = apply_multipliers(rates, ml(bat, pit, is_home, bases, outs))
    return rates


def simulate_game(home: TeamSide, away: TeamSide,
                  rng: Optional[random.Random] = None,
                  innings: int = 9,
                  context: Optional[Dict[str, Dict[int, float]]] = None,
                  log: Optional[List[dict]] = None,
                  weather: Optional[dict] = None,
                  venue: Optional[str] = None,
                  events: Optional[List[dict]] = None,
                  ml=None
                  ) -> GameResult:
    """Play one game plate appearance by plate appearance.

    `context` optionally carries per-side outcome multipliers keyed "home"/"away"
    (park x weather, umpire, defence) applied to the BATTING side.

    The game STRUCTURE is modelled, not a flat nine innings, because PA count is
    where the pricing value is: the bottom of the ninth is not played when the
    home side leads, a walk-off ends the half mid-rally, and a tie goes to extras
    under the automatic-runner rule. A flat nine hands every home batter roughly
    half an extra PA he does not get.
    """
    rng = rng or random.Random()
    res = GameResult(batters={}, pitchers={})
    context = context or {}

    order = {"away": 0, "home": 0}
    mound = {"away": MoundState(), "home": MoundState()}
    # Rest state for tonight: who is PHYSICALLY available, and nothing else. It
    # must NOT depend on `app_rate` — the selection score already carries that
    # as the base rate, and gating on it here charged it twice and starved the
    # back of the pen (see `PEN_AVAILABLE_P`). The draw is ALWAYS taken, so an
    # all-1.0 pen consumes the random stream exactly as before and is
    # bit-identical. sim_state.md A.7.
    for hf, sd in (("away", away), ("home", home)):
        mound[hf].available = {p.name for p in sd.bullpen
                               if rng.random() < PEN_AVAILABLE_P * p.availability}
    bf_by_pitcher: Dict[str, int] = {}
    # Pitches thrown, accumulated from the OUTCOMES actually simulated, so a
    # starter who is walking men and missing bats burns his count faster —
    # which is the real mechanism and correlates a bad night with an early
    # hook for free.
    pitch_by_pitcher: Dict[str, float] = {}
    runs = {"away": 0, "home": 0}
    # Tonight's offensive form, drawn ONCE per team-game. Per SIDE, never once
    # for the game — the two sides' totals are uncorrelated in real baseball.
    # Weather rides the SAME axis, deterministically and shared, which makes the
    # double-count explicit: re-calibrate `GAME_FORM_SD` whenever the weather
    # coefficients move. sim_state.md A.7.
    wx = weather_tilt(weather, venue)
    pk = {"home": park_run_tilt(venue, True),
          "away": park_run_tilt(venue, False)}
    form = {"away": (draw_form(rng) + wx + pk["away"]
                     + TeamQuality.team_quality_tilt(away.team_quality)),
            "home": (draw_form(rng) + wx + pk["home"]
                     + TeamQuality.team_quality_tilt(home.team_quality))}
    last_pit = {"away": "", "home": ""}

    def play_half(half: str, inning: int, walk_off: bool) -> None:
        bat_side = away if half == "away" else home
        pit_side = home if half == "away" else away
        mult = context.get(half)
        state = HalfInningState()

        # Automatic runner on second from the 10th: the man who made the last
        # out, i.e. the slot batting immediately before this inning's leadoff.
        if inning > innings:
            state.bases[1] = (order[half] - 1) % 9

        first_pa = True
        # Runs the RUNNING GAME has scored since the last logged PA. Real runs
        # in `runs[half]` either way; this exists so `re24_report` can attribute
        # them to the state they were scored FROM. sim_state.md A.6.
        pending_runs = 0
        half_rows = 0
        while state.outs < 3:
            # Leverage from the PITCHING side's perspective, read off the
            # measured table rather than a formula.
            pit_half = "home" if half == "away" else "away"
            lev = Leverage.game_leverage(inning, half == "away",
                                runs[pit_half] - runs[half],
                                sum(1 for b in state.bases if b is not None),
                                state.outs)
            mound[pit_half].inning = inning
            mound[pit_half].run_diff = runs[pit_half] - runs[half]
            mound[pit_half].bat_hand = bat_side.lineup[order[half] % 9].bats
            mound[pit_half].is_home = (pit_half == "home")
            pit = _mound(pit_side, mound[pit_half], bf_by_pitcher, lev,
                         first_pa, rng,
                         lambda nm: (res.pitchers.get(nm) or PitcherLine()).r,
                         pitch_by_pitcher)
            first_pa = False
            slot = order[half] % 9
            bat = bat_side.lineup[slot]

            # The running game resolves first, and can end the inning on a
            # caught stealing — in which case this batter's PA never happens.
            rg_outs_before = state.outs
            rg_bases_before = base_mask(state.bases)
            rg_scorers, rg_events = running_game(state, rng, bat_side.lineup)
            for s in rg_scorers:
                res.batters.setdefault(
                    bat_side.lineup[s].name, PlayerLine()).r += 1
                runs[half] += 1
                pending_runs += 1
                res.pitchers.setdefault(pit.name, PitcherLine()).r += 1
            rg_line = res.pitchers.setdefault(pit.name, PitcherLine())
            rg_line.outs += state.outs - rg_outs_before
            # **Credited from the EVENT, never inferred from the state.** The
            # old test — on first before, on second after, no out — is also
            # true of a wild pitch, so every wild pitch with a man on first
            # was booked as a stolen base.
            for ev in rg_events:
                if ev["kind"] == "SB":
                    res.batters.setdefault(
                        bat_side.lineup[ev["runner"]].name, PlayerLine()).sb += 1
                elif ev["kind"] == "CS":
                    res.batters.setdefault(
                        bat_side.lineup[ev["runner"]].name, PlayerLine()).cs += 1
            # The running game happens BETWEEN plate appearances, so a log of
            # only those shows a runner teleporting. Kept on its own list rather
            # than interleaved into `log`, whose row shape several consumers
            # depend on. sim_state.md A.6.
            if events is not None and rg_events:
                for ev in rg_events:
                    row = {"inning": inning, "half": half,
                           "before_pa": len(log) if log is not None else None,
                           "outs_before": rg_outs_before,
                           "outs_after": state.outs,
                           "bases_before": rg_bases_before,
                           "bases_after": base_mask(state.bases),
                           "pitcher": pit.name, **ev}
                    for k in ("runner",):
                        if k in ev:
                            row[k + "_name"] = bat_side.lineup[ev[k]].name
                    if ev.get("scored"):
                        row["scored_names"] = [bat_side.lineup[x].name
                                               for x in ev["scored"]]
                    events.append(row)
            if state.outs >= 3:
                break
            if walk_off and runs["home"] > runs["away"]:
                return

            faced = bf_by_pitcher.get(pit.name, 0)
            # The base-out state reaches `pa_rates` only for the ML residual.
            # Computed inside the guard because the shipped configuration is
            # `ml is None`, and `base_mask` on ~600M PAs is ~1.4% of a
            # backtest's wall clock for a value that would be discarded.
            rates = pa_rates(bat, pit, faced=faced, oaa=pit_side.oaa,
                             framing=pit_side.framing,
                             is_home=(half == "home"), tilt=form[half],
                             mult=mult, ml=ml,
                             bases=(base_mask(state.bases)
                                    if ml is not None else 0),
                             outs=state.outs)

            outs_before = state.outs
            before_bases = list(state.bases)
            outcome = Markov.draw_outcome(rates, rng)
            scorers, rbi, sac_fly = advance(state, outcome, slot, rng,
                                            bat_side.lineup,
                                            BallFlight.arm_factor(pit_side.of_arm))
            if log is not None:
                log.append({
                    "inning": inning, "half": half, "outs_before": outs_before,
                    "outs_after": state.outs,
                    "pitcher": pit.name, "new_pitcher": pit.name != last_pit[pit_half],
                    "batter": bat.name, "bats": bat.bats,
                    "throws": pit.throws, "outcome": OUTCOME_NAMES[outcome],
                    "rbi": rbi, "runs": len(scorers),
                    "on_before": sum(1 for b in before_bases if b is not None),
                    # The base-out state as a BITMASK (1=1B, 2=2B, 4=3B), which
                    # `on_before` cannot reconstruct — a runner on second is a
                    # different run expectancy from a runner on first. This is
                    # what `re24_report` needs to score the base-running
                    # constants against the measured RE24 on disk.
                    "bases_before": base_mask(before_bases),
                    "runs_before": pending_runs,
                    "score": (runs["away"], runs["home"]),
                })
                pending_runs = 0
                half_rows += 1
                last_pit[pit_half] = pit.name

            bline = res.batters.setdefault(bat.name, PlayerLine())
            Markov._record(bline, outcome, rbi, sac_fly)
            for s in scorers:
                res.batters.setdefault(
                    bat_side.lineup[s].name, PlayerLine()).r += 1
            runs[half] += len(scorers)

            pline = res.pitchers.setdefault(pit.name, PitcherLine())
            pline.bf += 1
            pline.r += len(scorers)
            # Credit the OUTS THE STATE ACTUALLY RECORDED, not one per out
            # outcome — a double play retires two men on a single GB_OUT, and
            # crediting one silently broke the outs-recorded market in 63% of
            # games (a side's staff finished on 26 outs instead of 27).
            pline.outs += state.outs - outs_before
            if outcome == K:
                pline.k += 1
            elif outcome == BB:
                pline.bb += 1
            elif outcome in (S1B, S2B, S3B, HR):
                pline.h += 1
                if outcome == HR:
                    pline.hr += 1

            bf_by_pitcher[pit.name] = faced + 1
            # **Only draw when the pitch hook is actually on.** `pa_pitches`
            # with an rng consumes a gauss draw, which advances the stream and
            # would change EVERY simulated game — including every cached A/B
            # arm — while the flag reads False. A dormant feature must not
            # touch the random stream.
            if USE_PITCH_HOOK:
                pitch_by_pitcher[pit.name] = (
                    pitch_by_pitcher.get(pit.name, 0.0)
                    + Fatigue.pa_pitches(outcome, rng))
            order[half] += 1

            if walk_off and runs["home"] > runs["away"]:
                return

        # The half is over. Two things can be left over, and both matter only
        # to `re24_report`: runs the running game scored after the last logged
        # plate appearance, and — when a caught stealing WAS the third out —
        # the fact that the half ended in three outs at all, which the last
        # row's `outs_after` cannot show because it predates the steal.
        if log is not None and half_rows:
            if pending_runs:
                log[-1]["runs_after"] = log[-1].get("runs_after", 0) \
                    + pending_runs
            if state.outs >= 3 and log[-1]["outs_after"] < 3:
                log[-1]["half_ended_rg"] = True

    inning = 1

    def _half(hf: str, inn: int, walk_off: bool) -> None:
        before = runs[hf]
        play_half(hf, inn, walk_off=walk_off)
        (res.half_runs_home if hf == "home"
         else res.half_runs_away).append(runs[hf] - before)

    while True:
        _half("away", inning, walk_off=False)
        # The home half is skipped entirely when the home side already leads
        # after the top of the last scheduled inning or any extra inning.
        if inning >= innings and runs["home"] > runs["away"]:
            break
        _half("home", inning, walk_off=(inning >= innings))
        if inning >= innings and runs["home"] != runs["away"]:
            break
        if inning >= MAX_INNINGS:
            break
        inning += 1

    res.runs_home = runs["home"]
    res.runs_away = runs["away"]
    return res


# ---------------------------------------------------------------------------
# 8. Monte Carlo and prop extraction
# ---------------------------------------------------------------------------

# Every market key in EffortMLB.MARKET_STATS that this engine can price,
# mapped to the accessor on a simulated line.
BATTER_MARKETS: Dict[str, str] = {
    "batter_home_runs": "hr",
    "batter_hits": "h",
    "batter_total_bases": "tb",
    "batter_rbis": "rbi",
    "batter_runs_scored": "r",
    "batter_hits_runs_rbis": "hrr",
    "batter_singles": "b1",
    "batter_doubles": "b2",
    "batter_triples": "b3",
    "batter_walks": "bb",
    "batter_strikeouts": "k",
    "batter_stolen_bases": "sb",
}

PITCHER_MARKETS: Dict[str, str] = {
    "pitcher_strikeouts": "k",
    "pitcher_hits_allowed": "h",
    "pitcher_walks": "bb",
    "pitcher_outs": "outs",
    "pitcher_earned_runs": "r",
}


def simulate_many(home: TeamSide, away: TeamSide, n: int = 20000,
                  seed: Optional[int] = None,
                  context: Optional[Dict[str, Dict[int, float]]] = None,
                  weather: Optional[dict] = None,
                  venue: Optional[str] = None,
                  ml=None) -> List[GameResult]:
    rng = random.Random(seed)
    return [simulate_game(home, away, rng, context=context,
                          weather=weather, venue=venue, ml=ml)
            for _ in range(n)]


# **`_slate_worker` / `simulate_slate` REMOVED 2026-08-24 — dead, and a trap
# if revived.** They hand-packed two constants into a job tuple while the worker
# read seven more that never travelled, so under forkserver any calibration that
# rebound one would have been silently compared against the shipped model. Live
# paths ship state as a NAME->VALUE dict via `_slate_overrides()`; both pool
# workers capture, never enumerate. sim_state.md A.8.


def prop_distribution(results: Sequence[GameResult], player: str,
                      market: str) -> List[float]:
    """The simulated values for one player and one market, one per game."""
    if market in BATTER_MARKETS:
        attr = BATTER_MARKETS[market]
        return [float(getattr(r.batters.get(player) or PlayerLine(), attr))
                for r in results]
    if market in PITCHER_MARKETS:
        attr = PITCHER_MARKETS[market]
        return [float(getattr(r.pitchers.get(player) or PitcherLine(), attr))
                for r in results]
    raise KeyError(f"mlb_sim: no simulated stat for market {market!r}")


def price_over(values: Sequence[float], line: float) -> float:
    """P(value > line). Half-point lines make this unambiguous; on an integer
    line the push mass is excluded from BOTH sides, which is what a book
    means by a push rather than a loss."""
    if not values:
        return 0.0
    over = sum(1 for v in values if v > line)
    push = sum(1 for v in values if v == line)
    live = len(values) - push
    return over / live if live else 0.0


def to_american(p: float) -> Optional[int]:
    """Fair American odds for a probability, no vig."""
    if p <= 0.0 or p >= 1.0:
        return None
    return round(-100.0 * p / (1.0 - p)) if p >= 0.5 else round(100.0 * (1.0 - p) / p)


def summarize_prop(results: Sequence[GameResult], player: str, market: str,
                   line: float) -> dict:
    vals = prop_distribution(results, player, market)
    p = price_over(vals, line)
    mean = sum(vals) / len(vals) if vals else 0.0
    return {
        "player": player,
        "market": market,
        "line": line,
        "mean": mean,
        "p_over": p,
        "fair_over": to_american(p),
        "fair_under": to_american(1.0 - p),
    }


# ===========================================================================
# 9. RATE INGEST — FANGRAPHS BOARDS TO OUTCOME VECTORS
# The league baseline is computed from the SAME board the player rates come
# from. Shrinking toward a prior built from a different source imports every
# definitional difference between them as a silent one-directional bias.
# ===========================================================================


SAVE_DIR = _SIM_ROOT / "savedata"

# **Two roots, because two applications own this data between them.**
# `SAVE_DIR` is the sim's own store; `SHARED_DIR` is the app-wide
# `OddsAPI/savedata`, holding the handful of caches `EffortMLB` both READS AND
# WRITES alongside us. Those must stay ONE file, not two — a second copy would
# go stale in whichever process refreshed it last and neither side would know.
# Reach for them through `_shared()`, never by hardcoding, so a caller passing
# an explicit `save_dir` still gets one self-consistent directory.
SHARED_DIR = _APP_ROOT / "savedata"


# Weight of a season relative to the most recent one, halving each year back.
# It drives BOTH the blended counts and the blended effective PA, so it also
# moves how hard `shrink_rates` regresses a player.
#
# **MEASURED 2026-08-24: the value survives, its old justification did not.**
# Out of sample the optimum is a BROAD BASIN from 0.75 to 2.0 with 1.0 inside it
# in every fold; only the ENDS are discriminated. **Not the compression lever.**
# sim_state.md A.9.
SEASON_HALF_LIFE = 1.0
class RateIngest:
    """FanGraphs board rows -> shrunk per-PA outcome vectors, and the playing-time prior."""

    @staticmethod
    def _shared(save_dir=None) -> Path:
        """Where the caches co-owned with `EffortMLB` live.

        The DEFAULT store splits — sim data under `Sims/savedata`, co-owned
        caches under `OddsAPI/savedata`. Any explicit override is honoured as
        given, so a caller pointing at a scratch directory keeps everything in it.
        """
        p = Path(save_dir) if save_dir is not None else SAVE_DIR
        return SHARED_DIR if p == SAVE_DIR else p

    @staticmethod
    def rate_seasons(side: str) -> Tuple[int, ...]:
        return RATE_SEASONS_BAT if side == "bat" else RATE_SEASONS_PIT

    @staticmethod
    def use_season_blend(side: str) -> bool:
        return USE_SEASON_BLEND_PIT if side == "pit" else USE_SEASON_BLEND_BAT

    @staticmethod
    def projected_league_baseline(board: Sequence[dict], side: str,
                                  prior_board: Optional[Sequence[dict]]
                                  ) -> List[float]:
        """The FULL-season league environment, projected from a partial board.

        **Season-to-date is the wrong target, and on an as-of board it is wrong
        by a lot** — the board's HOME-RUN rate reads -15.4% at 7 April. That is
        the cold-weather effect, which `weather_tilt` already prices, so an
        April-depressed baseline charges the cold TWICE — and `rebase_to_season`
        maps every player's older evidence onto it.

            baseline = f * observed + (1 - f) * prior season

        `f` is playing time per club against the prior season's: no calendar, no
        free parameter, and at f = 1 it is the old behaviour exactly. A.9.
        """
        observed = league_baseline(board, side)
        if not prior_board:
            return observed
        now, before = (board_pa_per_club(board, side),
                       board_pa_per_club(prior_board, side))
        if before <= 0:
            return observed
        f = min(1.0, now / before)
        if f >= 1.0:
            return observed
        prior = league_baseline(prior_board, side)
        return _normalize([f * o + (1.0 - f) * p
                           for o, p in zip(observed, prior)])

    @staticmethod
    def season_weights(seasons: Sequence[int],
                       half_life: Optional[float] = None) -> Dict[int, float]:
        """Recency weight per season, 1.0 on the most recent."""
        half_life = SEASON_HALF_LIFE if half_life is None else float(half_life)
        if not seasons:
            return {}
        newest = max(seasons)
        decay = math.log(2.0) / half_life
        return {s: math.exp(-decay * (newest - s)) for s in seasons}

    @staticmethod
    def _curve_from_board(board: Sequence[dict], side: str
                          ) -> List[Tuple[float, List[float]]]:
        """[(playing-time SHARE, outcome vector)] per equal-count bin."""
        per_club = board_pa_per_club(board, side)
        if per_club <= 0:
            return []
        rows = []
        for row in board or []:
            counts, pa = outcome_counts(row, side)
            if pa > 0:
                rows.append((pa / per_club, counts))
        return RateIngest._prior_bins(rows)

    @staticmethod
    def _prior_bins(rows: List[Tuple[float, List[float]]]
                    ) -> List[Tuple[float, List[float]]]:
        """Equal-count playing-time bins over [(share, counts)]."""
        if not rows:
            return []
        rows = sorted(rows, key=lambda r: r[0])
        step = max(1, len(rows) // PRIOR_BINS)
        out: List[Tuple[float, List[float]]] = []
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            if len(chunk) < step // 2 and out:      # fold a short tail back in
                break
            acc = [0.0] * N_OUTCOMES
            for _, counts in chunk:
                for j in range(N_OUTCOMES):
                    acc[j] += counts[j]
            out.append((statistics.mean(pa for pa, _ in chunk), _normalize(acc)))
        return out

    @staticmethod
    def solve_bat_prior_tilt(pop: Sequence[Tuple[float, Sequence[float]]],
                             curve: Sequence[Tuple[float, List[float]]],
                             league: Sequence[float],
                             stab: Sequence[float]) -> float:
        """The tilt that stops a playing-time prior moving the league's run level.

        **Side-agnostic despite the name** — `league`, `stab`, `curve` and `pop`
        all arrive as arguments, and `offence_tilt` / `PRIOR_CENTRE_LW` act on a
        bare nine-vector. The name is kept because it is what the surrounding
        comments and `sim_state.md` A.9 call it. `PIT_PRIOR_CENTRED` uses it
        unchanged; on a pitcher's vector a positive tilt means MORE allowed.

        **Centre on the population you actually apply it to** — trap 7, and it
        took FOUR wrong answers, each of which measured as a clean success on the
        quantity it was solved for. `pop` is [(playing-time share, blended
        counts)] and must carry the BLENDED counts the engine really shrinks
        against; solved off the newest board instead it cost -0.79 runs a game on
        the April cutoffs, invisibly. sim_state.md A.9.
        """
        if not pop or not curve:
            return 0.0
        rv = lambda v: sum(w * x for w, x in zip(PRIOR_CENTRE_LW, v))
        rows = []
        for share, counts in pop:
            n = sum(counts)
            if n <= 0:
                continue
            rows.append((counts, n, _curve_at(curve, share),
                         rv(shrink_rates(counts, league, stab))))
        if not rows:
            return 0.0

        def imbalance(t: float) -> float:
            num = den = 0.0
            for counts, n, tgt, base_rv in rows:
                got = shrink_rates(counts, offence_tilt(tgt, t) if t else tgt, stab)
                # weight by playing time: a row counts for as many lineup slots as
                # it really fills
                num += n * (rv(got) - base_rv)
                den += n
            return num / den if den else 0.0

        lo, hi = 0.0, 0.35
        if imbalance(lo) > 0:                     # prior already lifts: tilt DOWN
            lo, hi = -0.35, 0.0
        if imbalance(lo) * imbalance(hi) > 0:     # not bracketed — refuse, do not clamp
            return 0.0
        for _ in range(BAT_PRIOR_CENTRE_ITERS):
            mid = 0.5 * (lo + hi)
            if imbalance(mid) < 0:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    @staticmethod
    def blend_seasons(by_season: Dict[int, Tuple[List[float], float]],
                      half_life: Optional[float] = None,
                      newest: Optional[int] = None
                      ) -> Tuple[List[float], float]:
        """Recency-weighted combination of one player's per-season counts.

        Returns (blended counts, effective PA). The effective PA is weighted too,
        so a player whose only recent sample is small stays properly shrunk.

        **`newest` must be the newest season on the BOARD, not the newest this
        player has**, or `season_weights` anchors on his own last season and
        re-weights a stale line as though it were current — worse on an as-of
        board, where "not on the board yet" is April's normal state.
        """
        half_life = SEASON_HALF_LIFE if half_life is None else float(half_life)
        seasons = list(by_season)
        if newest is not None and newest not in by_season:
            seasons.append(newest)
        w = RateIngest.season_weights(seasons, half_life)
        counts = [0.0] * N_OUTCOMES
        pa = 0.0
        for season, (c, p) in by_season.items():
            wt = w[season]
            for i in range(N_OUTCOMES):
                counts[i] += c[i] * wt
            pa += p * wt
        return counts, pa



# Which seasons feed each side's blend. Empty means "every board on disk",
# which is the shipped behaviour. These exist so a board ADDITION can be
# A/B'd against its own absence without moving files around — the slate
# harness captures every uppercase constant, so they travel to the pool.
RATE_SEASONS_BAT: Tuple[int, ...] = ()
RATE_SEASONS_PIT: Tuple[int, ...] = ()


# Whether each side blends OLDER seasons at all. `RATE_SEASONS_*` cannot
# express this — it is an absolute list, and an A/B arm is one constant for
# every season it runs. **The blend has never been scored**, on either side.
# sim_state.md A.9.
USE_SEASON_BLEND_BAT = True
USE_SEASON_BLEND_PIT = True


# League BABIP by batted-ball type, used ONLY to split outs in play into
# ground and air. Ground balls and line drives convert to outs at very
# different rates, so splitting by raw batted-ball share would put far too
# many outs on the line-drive side.
BABIP_GB = 0.239
BABIP_FB = 0.128    # includes infield flies, which are near-automatic outs
BABIP_LD = 0.630

# Extra-base mix of NON-HOME-RUN hits, league-wide. The pitching board carries
# only H and HR, so 2B/3B must be imputed there; the BATTING board carries the
# real split and is ground truth for exactly this constant. Re-derive with
# `mlb_sim.py rates` — if the two 1B/2B rows drift apart, this pair is stale.
LG_XB_SHARE_2B = 0.221
LG_XB_SHARE_3B = 0.0194
LG_AIR_SHARE = 0.55     # league air share of balls in play, the pivot below


def _num(row: dict, key: str, default: float = 0.0) -> float:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else default


def _innings(row: dict, key: str = "IP", default: float = 0.0) -> float:
    """Innings off the board, which are written in OUTS notation.

    **`65.2` is 65 and TWO THIRDS.** Verified on the 2026 pitching board: the
    fractional part takes only `.0`/`.1`/`.2` and nothing else. Read as a plain
    float it is short by up to 0.467 innings, always the same way, and both
    consumers are in the start-length path so the errors COMPOUND.
    """
    v = row.get(key)
    if not isinstance(v, (int, float)):
        return default
    whole = int(v)
    outs = round((float(v) - whole) * 10)
    if outs not in (0, 1, 2):
        # not outs notation after all — trust the number as written
        return float(v)
    return whole + outs / 3.0


def _split_outs_in_play(outs: float, gb: float, fb: float, ld: float,
                        hr: float) -> Tuple[float, float]:
    """Divide outs on balls in play into (ground, air).

    Weighted by how often each batted-ball type actually becomes an out, then
    renormalised so the two sides still sum to the outs we know were made.
    That keeps the identity exact while respecting that a grounder retires the
    batter far more often than a line drive does.
    """
    w_gb = gb * (1.0 - BABIP_GB)
    w_air = max(fb - hr, 0.0) * (1.0 - BABIP_FB) + ld * (1.0 - BABIP_LD)
    if w_gb + w_air <= 0 or outs <= 0:
        # No batted-ball detail: fall back to the league ground/air split.
        return outs * 0.46, outs * 0.54
    share_gb = w_gb / (w_gb + w_air)
    return outs * share_gb, outs * (1.0 - share_gb)


def outcome_counts(row: dict, side: str) -> Tuple[List[float], float]:
    """One board row -> (9-vector of outcome counts, plate appearances).

    `side` is "bat" or "pit". Returns raw COUNTS, not rates, because counts
    are what shrinkage and season-blending both need.
    """
    pa = _num(row, "PA") if side == "bat" else _num(row, "TBF")
    if pa <= 0:
        return [0.0] * N_OUTCOMES, 0.0

    so = _num(row, "SO")
    bb = _num(row, "BB")
    hbp = _num(row, "HBP")
    h = _num(row, "H")
    hr = _num(row, "HR")
    d2 = _num(row, "2B")
    d3 = _num(row, "3B")
    # The pitcher board carries no 1B/2B/3B breakdown, only H and HR, so the
    # extra-base split has to come from the batted-ball columns there.
    if side == "bat":
        s1 = _num(row, "1B")
    else:
        s1 = None

    gb, fb, ld = _num(row, "GB"), _num(row, "FB"), _num(row, "LD")

    if s1 is None:
        # Doubles and triples are not on the pitching board. Apportion the
        # non-home-run hits by the league extra-base mix, scaled by how
        # air-heavy this pitcher is — a fly-ball pitcher gives up more
        # doubles per hit than a ground-ball pitcher does.
        non_hr = max(h - hr, 0.0)
        air_share = ((max(fb - hr, 0.0) + ld) / (gb + fb + ld)
                     if (gb + fb + ld) > 0 else LG_AIR_SHARE)
        d2 = non_hr * min(LG_XB_SHARE_2B * (air_share / LG_AIR_SHARE), 0.45)
        d3 = non_hr * LG_XB_SHARE_3B
        s1 = max(non_hr - d2 - d3, 0.0)

    hits_in_play_outs = pa - so - bb - hbp - h
    outs_in_play = max(hits_in_play_outs, 0.0)
    gb_out, air_out = _split_outs_in_play(outs_in_play, gb, fb, ld, hr)

    counts = [0.0] * N_OUTCOMES
    counts[K] = so
    counts[BB] = bb
    counts[HBP] = hbp
    counts[GB_OUT] = gb_out
    counts[AIR_OUT] = air_out
    counts[S1B] = s1
    counts[S2B] = d2
    counts[S3B] = d3
    counts[HR] = hr
    return counts, pa


def league_baseline(rows: Sequence[dict], side: str) -> List[float]:
    """League per-PA outcome vector, summed straight off the board.

    This is the shrinkage target and it must come from the same board as the
    players — see the module docstring.
    """
    total = [0.0] * N_OUTCOMES
    for row in rows:
        counts, _ = outcome_counts(row, side)
        for i in range(N_OUTCOMES):
            total[i] += counts[i]
    return _normalize(total)


# ---------------------------------------------------------------------------
# The shrinkage prior depends on PLAYING TIME — sim_state.md 5.9 / A.9
# ---------------------------------------------------------------------------
# `shrink_rates` assumes a player is a random draw from the league. **He is not
# — playing time in MLB is selected on performance**, oppositely on the two
# sides: shrinking a 40-batter reliever toward league calls him a 0.331 arm and
# arms with that little work threw 0.368, in RELIEF innings, which is where the
# sim's per-inning deficit was.
#
# **PITCHERS ONLY**, and that is structural: a hitter arrives through the POSTED
# LINEUP, a second selection on the same axis, so the prior on top counts it
# twice. Measured: pitcher prior +0.047 runs a game, batter prior -0.178.
PRIOR_SIDES = ("pit",)

# --- the HITTER playing-time prior -----------------------------------------
# **OFF by default and only meaningful CENTRED.** The naive flip was measured
# and rejected: the curve's bins are PA-weighted, but the prior is applied as a
# SHRINKAGE TARGET, and fringe hitters carry a target ~25% below league AND the
# heaviest weight toward it (trap 7, sixth instance). `bat_prior_offset`
# re-centres on the population it is really applied to. sim_state.md A.9.
USE_BAT_PRIOR = False
BAT_PRIOR_CENTRED = True
BAT_PRIOR_CENTRE_ITERS = 40

# --- the PITCHER playing-time prior, centred -------------------------------
# **The centring above was gated to `side == "bat"` in both places it appears,
# so the prior that actually SHIPS (`PRIOR_SIDES = ("pit",)`) had none.**
# League IS the PA-weighted mean of all pitchers; the PA-weighted mean of the
# prior TARGET is a second estimate of that same quantity, and on the 2026
# as-of boards the two disagree by -0.4115 runs per team-game at the 04-07
# cutoff against -0.0197 at 08-11. Shrinkage weight decays as samples grow, so
# the disagreement decays with it and surfaces as a CALENDAR ramp: the model's
# projected total walks +1.185 runs across 2026 (8.104 -> 9.289) while actual
# scoring is flat, and `PRIOR_SIDES = ()` flattens the pitcher side's whole
# contribution to it (-0.2445 -> -0.0614 in April, -0.0156 -> -0.0645 in
# August). The same shape appears in 2025 (+0.96) where the league happened to
# ramp too, which is why it went unseen.
#
# OFF until it is scored. Unlike 4i's raking and 4j's opener fix this is not a
# free defect repair: it removes ~0.46 runs a game of seasonal ramp, which
# helps the April under-read that replicates in BOTH seasons (-0.507 2025,
# -0.862 2026) and hurts 2025's August. Arm `pitcentre`. sim_state.md 5.21.
PIT_PRIOR_CENTRED = False

# WHICH population the centring bisection is solved over. "board" every arm on
# the board; "engine" only those `engine_pitcher_ids` says will pitch.
#
# **"engine" was the better-reasoned answer and it SCORED WORSE — 5.21.** The
# post-tilt residual really is +0.060 on sub-1%-share arms against -0.007 on
# the bulk, so the board solve really does balance across arms `PEN_DEPTH`
# removes. But the tilt is ONE SCALAR APPLIED TO EVERY ARM, so by trap 7's own
# wording the board IS the population it is applied to; narrowing the solve set
# without narrowing the apply set re-creates the mismatch pointing the other
# way. Excluding a POSITIVE residual makes the solver want a bigger positive
# tilt, every pitcher's target allows more, and 2026 ran +0.175 runs hot.
# Head-to-head against "board": t -4.40 (2026) and t -1.93 (2025).
#
# The residual's share-dependence is real and unfixed — a single scalar cannot
# zero a residual that VARIES along the share axis. That is a shape limit of
# the correction, not a population error, and "engine" only moves which slice
# is left over. Arm `pitcentre-enginepop` keeps it measurable.
PIT_PRIOR_CENTRE_POP = "board"       # "board" | "engine"

# Linear weights, for centring the hitter prior on RUN VALUE rather than on
# on-base. `offence_tilt` preserves the hit mix; the curve does not — a fringe
# hitter is weaker in slugging too. Centring on-base alone leaves -0.066 runs a
# game on the table. sim_state.md A.9.
PRIOR_CENTRE_LW: Tuple[float, ...] = (0.0, 0.69, 0.72, 0.0, 0.0,
                                      0.883, 1.244, 1.569, 2.004)


# Equal-COUNT bins, and the choice matters. Equal-weight bins (equal share of
# total PA) put the whole bottom of the board — where the effect is — into one
# bucket alongside 200-batter arms, diluting a +0.078 signal to +0.037 and
# leaving two thirds of the error in place. Equal counts spend the resolution
# where the players are, which is exactly where the curve is steep.
PRIOR_BINS = 8

_PRIOR_CURVE: Dict[tuple, List[Tuple[float, List[float]]]] = {}
# The league the cached SHAPE was measured in, so it can be rebased onto the
# board actually being used. Cleared wherever _PRIOR_CURVE is.
_PRIOR_LEAGUE: Dict[tuple, Optional[List[float]]] = {}

N_CLUBS = 30


def board_pa_per_club(rows: Sequence[dict], side: str) -> float:
    """One club's total plate appearances on this board. The scale unit.

    **The prior curve must be indexed by a SHARE of playing time, not a count.**
    The curve encodes a rate, and a raw count silently carries how much SEASON
    the board covers: on a season-final board 57 TBF is a fringe arm, ten days in
    it is a workhorse with two starts. Dividing by this makes the index
    season-length invariant, and on a full-season board it is one constant
    divisor, so nothing about the shipped bins changes.
    """
    tot = 0.0
    for row in rows or []:
        tot += outcome_counts(row, side)[1]
    return (tot / N_CLUBS) if tot > 0 else 0.0


def prior_curve(side: str, season: Optional[int] = None, save_dir: Path = SAVE_DIR,
                rows_override: Optional[List[dict]] = None
                ) -> List[Tuple[float, List[float]]]:
    """The playing-time prior's SHAPE, taken from a COMPLETED prior season.

    **A partial board cannot produce this curve, and it fails in the direction
    that flatters the model** — ten days in, cumulative playing time separates
    relievers from starters rather than good from bad, so the 2026-04-07 board
    inverted the curve and regressed every backtested starter toward a prior that
    made him good. ~0.9 runs a game on the early cutoffs, and INVISIBLE to a
    harness that never builds a partial board.

    The shape is persistent across three seasons, which is what makes this
    legitimate rather than convenient; only the run ENVIRONMENT moves, and
    `rebase_to_season` maps each bin onto the target league. A.9.
    """
    season = CURRENT_SEASON if season is None else int(season)
    board = (rows_override if rows_override is not None
             else load_board(side, season, save_dir) or [])
    earlier = [s for s in available_seasons(side, save_dir) if s < season]
    key = (side, int(season), str(save_dir))

    shape = _PRIOR_CURVE.get(key)
    if shape is None:
        src = (load_board(side, max(earlier), save_dir) if earlier else board)
        shape = RateIngest._curve_from_board(src or [], side)
        src_league = league_baseline(src, side) if src else None
        _PRIOR_CURVE[key] = shape
        _PRIOR_LEAGUE[key] = src_league
    src_league = _PRIOR_LEAGUE.get(key)

    if not shape or src_league is None or not board:
        return shape
    target = league_baseline(board, side)
    return [(pt, _normalize(rebase_to_season(v, src_league, target)))
            for pt, v in shape]


def playing_time_prior(share: float, side: str, league: Sequence[float],
                       season: Optional[int] = None, save_dir: Path = SAVE_DIR,
                       rows_override: Optional[List[dict]] = None,
                       centre_tilt: Optional[float] = None
                       ) -> List[float]:
    """The outcome vector a player with this much playing time comes from.

    `share` is his playing time as a fraction of ONE club's — see
    `board_pa_per_club`. Deliberately NOT the summed multi-season PA that drives
    shrinkage: how much evidence we have and what role he fills are different
    questions, and a part-timer with three seasons on the board is still a
    part-timer. Falls back to `league` where the prior does not apply.
    """
    season = CURRENT_SEASON if season is None else int(season)
    if side not in _prior_sides():
        return list(league)
    curve = prior_curve(side, season, save_dir, rows_override)
    if not curve:
        return list(league)
    got = _curve_at(curve, share)
    if centre_tilt and _prior_centred(side):
        got = offence_tilt(got, centre_tilt)
    return got


def _prior_centred(side: str) -> bool:
    """Whether this side's playing-time prior is re-centred on its population.

    Per SIDE because the two shipped independently: the hitter prior is centred
    and off, the pitcher prior is on and — until `PIT_PRIOR_CENTRED` — was not
    centred at all, because both gates read `side == "bat"` literally.
    """
    return BAT_PRIOR_CENTRED if side == "bat" else PIT_PRIOR_CENTRED





def _prior_sides() -> Tuple[str, ...]:
    """Which sides the playing-time prior applies to.

    `USE_BAT_PRIOR` is a separate flag rather than an edit to `PRIOR_SIDES`
    so the shipped pitcher behaviour cannot move when the hitter side is
    switched on, and so `_slate_overrides` carries one boolean into a
    forkserver worker instead of a tuple (trap 6).
    """
    return PRIOR_SIDES + (("bat",) if USE_BAT_PRIOR
                          and "bat" not in PRIOR_SIDES else ())


def _curve_at(curve: Sequence[Tuple[float, List[float]]],
              share: float) -> List[float]:
    """The prior curve read at one playing-time share, UNCENTRED."""
    if share <= curve[0][0]:
        return list(curve[0][1])
    if share >= curve[-1][0]:
        return list(curve[-1][1])
    for (p0, v0), (p1, v1) in zip(curve, curve[1:]):
        if p0 <= share <= p1:
            # interpolate in LOG playing time: the curve is steep at the
            # bottom, where the bins are decades apart, and flat at the top.
            t = ((math.log(share) - math.log(p0))
                 / (math.log(p1) - math.log(p0)) if p1 > p0 > 0 else 0.0)
            return _normalize([a + t * (b - a) for a, b in zip(v0, v1)])
    return list(curve[-1][1])


def rebase_to_season(counts: Sequence[float], season_league: Sequence[float],
                  target_league: Sequence[float]) -> List[float]:
    """Re-express one season's outcome counts in ANOTHER season's environment.

    **Blending raw counts across seasons imports their run environments, and
    they are not the same** — a 2024 league-average pitcher reads 1.8% BETTER
    than league when shrunk toward 2026. He is not better; the league was. The
    asymmetry made it worse than a wash and cost ~0.11 runs a game.
    Renormalising back to the original PA preserves SAMPLE SIZE, which drives
    shrinkage and must not be invented by an era adjustment.
    """
    n = sum(counts)
    if n <= 0:
        return list(counts)
    out = [counts[i] * (target_league[i] / season_league[i])
           if season_league[i] > 0 else counts[i]
           for i in range(N_OUTCOMES)]
    scale = n / sum(out) if sum(out) > 0 else 1.0
    return [c * scale for c in out]


# ---------------------------------------------------------------------------
# PITCH-CHARACTERISTIC repeatability — sim_state.md 0.1 Objective 1 / A.9
# ---------------------------------------------------------------------------
# A pitcher's own line is a far worse estimate of him than a hitter's is (HR
# stabilises at 634 TBF against 244), and a starter carries ~23 of a game's ~38
# batters. **The seam that failed twice is NOT this one**: 3d.6/3d.7 made a
# CONTACT estimate the target for counts built from the same batted balls, while
# pitch characteristics are DISJOINT from the outcomes being shrunk — which is
# why xERA/SIERA/xFIP are deliberately not features. Repeatability is a WEIGHT:
#
#     estimate = w * observed + (1 - w) * (playing-time prior + stuff delta)
#     w        = n / (n + M_eff)        M_eff = M / (1 - rho2)
#
# At rho2 = 0 this reduces exactly to the shipped behaviour.
STUFF_FEATURES: Tuple[str, ...] = (
    "sp_stuff",        # FanGraphs Stuff+
    "sp_location",     # FanGraphs Location+
    "pb_stuff",        # PitchingBot stuff
    "pb_command",      # PitchingBot command
    "FBv",             # fastball velocity
)

# --- the ARSENAL block: spin, break and velocity separation ----------------
# The board carries 145 per-pitch-type columns nothing read, all of them PITCH
# CHARACTERISTICS, so the disjointness argument covers them unchanged.
# **They cannot go in as 17 raw columns** — a pitcher with no curveball has a
# null and `_stuff_feats` is all-or-nothing by design — so they collapse into a
# DENSE usage-weighted block, one number per FAMILY, with a family he does not
# throw falling back to his OWN arsenal average. sim_state.md A.9.
PITCH_FAMILIES: Dict[str, Tuple[str, ...]] = {
    "fb": ("FA", "FT", "SI", "FC"),
    "bb": ("SL", "CU", "KC", "ST", "SC", "CV", "SLO", "CUO"),
    "off": ("CH", "FS", "FO", "EP"),
}
STUFF_ARSENAL_FEATURES: Tuple[str, ...] = (
    "spin_fb", "spin_bb", "spin_off",   # release spin by family, rpm
    "mov_h", "mov_v",                   # usage-weighted break, inches
    "velo_sep",                         # fastball minus offspeed velocity
)
# **There is deliberately no DRIFT feature here.** A trailing-window minus
# season delta was built and measured and is null — the correlation flips SIGN
# between seasons, and drift is only 4.2% of the cross-sectional variance.
# **SHIPPED True**: 4.6x the five-column version on the closing line (paired
# t +1.14 against +0.25). Turning it off requires putting the five-column
# STUFF_RELIABILITY back. sim_state.md A.9.
STUFF_USE_ARSENAL = True


def _arsenal_block(row: dict) -> Optional[List[float]]:
    """The dense arsenal features for one board row, or None if unusable.

    Usage-weighted across the pitch types he actually throws, so a two-pitch
    reliever and a six-pitch starter produce comparable numbers.
    """
    use: Dict[str, float] = {}
    for _fam, types in PITCH_FAMILIES.items():
        for pt in types:
            u = row.get(f"pfx{pt}%")
            sp = row.get(f"pfxsp{pt}")
            if isinstance(u, (int, float)) and u > 0 and isinstance(
                    sp, (int, float)):
                use[pt] = float(u)
    if not use:
        return None
    total = sum(use.values())
    if total <= 0:
        return None

    def wmean(key: str, types: Sequence[str]) -> Optional[float]:
        num = den = 0.0
        for pt in types:
            v = row.get(key.format(pt=pt))
            u = use.get(pt)
            if u and isinstance(v, (int, float)):
                num += float(v) * u
                den += u
        return (num / den) if den > 0 else None

    every = tuple(use)
    spin_all = wmean("pfxsp{pt}", every)
    if spin_all is None:
        return None
    spins = []
    for fam in ("fb", "bb", "off"):
        got = wmean("pfxsp{pt}", PITCH_FAMILIES[fam])
        # a family he does not throw falls back to HIS OWN arsenal average
        spins.append(spin_all if got is None else got)
    mov_h = wmean("pfx{pt}-X", every)
    mov_v = wmean("pfx{pt}-Z", every)
    v_fb = wmean("pfxv{pt}", PITCH_FAMILIES["fb"])
    v_off = wmean("pfxv{pt}", PITCH_FAMILIES["off"])
    if mov_h is None or mov_v is None or v_fb is None:
        return None
    # the separator: a change-up is only a change-up relative to the fastball
    sep = 0.0 if v_off is None else (v_fb - v_off)
    return spins + [abs(mov_h), mov_v, sep]

# Fraction of a pitcher's PREDICTABLE variance, per outcome, that the stuff
# estimate explains — measured, never assumed; at zero the estimator reduces
# exactly to the shipped one. **On BB, 2B, 3B and HR his stuff predicts his own
# future better than his own results do.** The MINIMUM of two seasons ships, not
# the mean, because every demonstrated failure here has been over-trusting a new
# term. **ARSENAL-fit values** — they must move with `STUFF_USE_ARSENAL` or
# `stuff_predict` raises on the feature width. Table: sim_state.md A.9.
STUFF_RELIABILITY: Tuple[float, ...] = (
    0.547,   # K
    0.443,   # BB
    0.149,   # HBP
    0.274,   # GB_OUT   <- 0.000 without the arsenal block
    0.587,   # AIR_OUT  <- 0.271
    0.363,   # 1B       <- 0.151
    0.504,   # 2B
    0.423,   # 3B
    0.377,   # HR
)

# **SHIPPED True 2026-08-16** on the arsenal feature set, then **turned OFF
# because CHED SUPERSEDES it, not because it failed.** Both read pitch
# characteristics into the same prior and `build_rates` refuses to run them
# together (double count). Flip the two to compare — `AB_ARMS["stuffprior"]`.
# sim_state.md A.9.
USE_STUFF_PRIOR = False

# Batters faced a pitcher needs on the board before his stuff columns are used
# at all. **20, not 40 — MEASURED 2026-08-24.** At 40 an arm four starts into a
# season had NO stuff term; the literature puts Stuff+ usable by ~80 pitches
# (~20 TBF). Worth ~nothing on its own, and lowered anyway because it is the
# difference between having a stuff term and having none. sim_state.md A.9.
STUFF_MIN_TBF = 20.0
# The count at which the stuff delta is trusted half against nothing. A
# pitch-characteristic average stabilises far faster than any outcome — every
# PITCH contributes, not every plate appearance.
#
# **It was 100, and that argument did not survive its own value**:
# `STABILIZE_PA_PIT[K]` is 93, so the stuff half-trust point sat ABOVE the
# stabiliser it claims to be small against. Swept on the thin population, 50 is
# the conservative end of a flat region between the two seasons' argmins — and
# picking the argmin of two seasons fits the noise between them. A.9.
STUFF_SHRINK_TBF = 50.0


# --- ROLLING arsenal: the same columns, over a trailing window -------------
# A season-to-date average hides that his fastball is down 1.2 mph since June —
# **and it is recoverable from the boards already on disk**, because every
# per-type column is a MEAN and the board carries the count it was taken over:
#
#     mean_window = (mean_2 * n_2 - mean_1 * n_1) / (n_2 - n_1)
#
# the same differencing `board_windows` uses on counts. This is NOT the question
# 3d.9 answered for pitcher OUTCOMES: characteristics are measured on every
# pitch, two orders of magnitude more evidence per unit of calendar. A.9.
STUFF_ROLLING_PITCHES = 0.0     # 0 = season to date; else the trailing window
# Below this many pitches of a TYPE inside the window, that type falls back to
# its season-to-date average rather than being computed from a handful.
STUFF_ROLLING_MIN_TYPE = 25.0


class Stuff:
    """Pitch-characteristic repeatability, and within-season recency."""

    @staticmethod
    def _type_pitches(row: dict, pt: str) -> Optional[float]:
        """How many pitches of one type this cumulative board row was taken over."""
        total = row.get("Pitches")
        share = row.get(f"pfx{pt}%")
        if not isinstance(total, (int, float)) or not isinstance(
                share, (int, float)):
            return None
        return float(total) * float(share)

    @staticmethod
    def rolling_board(side: str, season: int, terminal: Sequence[dict],
                      window: float, as_of: Optional[str] = None,
                      save_dir: Path = SAVE_DIR) -> List[dict]:
        """`terminal` with each arm's arsenal taken over his last `window` pitches.

        The reference snapshot is chosen PER PITCHER — the cached cutoff whose
        cumulative pitch count is closest to `now - window` — because a starter
        and a reliever cover the same window in very different amounts of calendar
        and a single date would give one of them a tenth of the sample.
        """
        if window <= 0:
            return list(terminal)
        cuts = [c for c in available_asof_cutoffs(season, save_dir)
                if as_of is None or c < as_of]
        if not cuts:
            return list(terminal)
        snaps = []
        for c in cuts:
            rows = load_board_asof(side, season, c, save_dir) or []
            snaps.append({pid: r for r in rows
                          if (pid := _row_id(r)) is not None})
        out = []
        for row in terminal:
            pid = _row_id(row)
            now_n = row.get("Pitches")
            if pid is None or not isinstance(now_n, (int, float)):
                out.append(row)
                continue
            target = float(now_n) - window
            best, best_gap = None, None
            for snap in snaps:
                prev = snap.get(pid)
                n = prev.get("Pitches") if prev else None
                if not isinstance(n, (int, float)) or n >= now_n:
                    continue
                gap = abs(float(n) - target)
                if best_gap is None or gap < best_gap:
                    best, best_gap = prev, gap
            out.append(rolling_arsenal_row(row, best))
        return out

    @staticmethod
    def _lstsq(A: Sequence[Sequence[float]],
               targets: Sequence[Sequence[float]],
               ridge: float = 1e-6) -> List[List[float]]:
        """Least squares via normal equations, for SEVERAL targets at once.

        The nine outcomes share one design matrix, so `A^T A` is factorised once
        and every right-hand side rides along — the alternative rebuilds an
        identical p x p system nine times, in a function every pool worker runs.

        The ridge term is for conditioning only: the features are collinear
        (Stuff+ and PitchingBot stuff measure the same thing two ways) and an
        exactly singular system is otherwise reachable on a thin board.
        """
        p = len(A[0])
        k = len(targets)
        ata = [[sum(r[i] * r[j] for r in A) for j in range(p)] for i in range(p)]
        atb = [[sum(r[i] * y for r, y in zip(A, t)) for t in targets]
               for i in range(p)]
        for i in range(p):
            ata[i][i] += ridge
        for i in range(p):
            piv = max(range(i, p), key=lambda r: abs(ata[r][i]))
            ata[i], ata[piv] = ata[piv], ata[i]
            atb[i], atb[piv] = atb[piv], atb[i]
            d = ata[i][i]
            if abs(d) < 1e-12:
                continue
            for r in range(p):
                if r == i:
                    continue
                f = ata[r][i] / d
                for c in range(i, p):
                    ata[r][c] -= f * ata[i][c]
                for c in range(k):
                    atb[r][c] -= f * atb[i][c]
        return [[atb[i][c] / ata[i][i] if abs(ata[i][i]) > 1e-12 else 0.0
                 for i in range(p)] for c in range(k)]

    @staticmethod
    def stuff_source_board(season: int, board: Sequence[dict],
                           as_of: Optional[str] = None,
                           save_dir: Path = SAVE_DIR) -> List[dict]:
        """The board the stuff features are read from — rolling, if configured.

        One place, so the rate layer and every measurement harness cannot end up
        reading different windows. The MODEL is still fit on season-to-date
        features, because the fit seasons have no as-of boards to roll; the
        features are on the same scale either way and the per-population centring
        absorbs any offset between them.
        """
        if STUFF_ROLLING_PITCHES <= 0:
            return list(board)
        return Stuff.rolling_board("pit", season, board, STUFF_ROLLING_PITCHES,
                             as_of, save_dir)

    @staticmethod
    def stuff_future_rows(season: int, save_dir: Path = SAVE_DIR,
                          min_pre: float = 100.0, min_post: float = 100.0,
                          trim: int = 4) -> List[dict]:
        """Every (as-of line, what he did AFTER it) pair the season can supply.

        Differencing the season-final board against an as-of one gives the rest of
        that pitcher's season, which is the only honest target for "does this
        predict him". `trim` drops the first and last few cutoffs: the earliest
        have no sample to estimate from and the latest have no future to score
        against.
        """
        full = {pid: r for r in load_board("pit", season, save_dir)
                if (pid := _row_id(r)) is not None}
        cuts = available_asof_cutoffs(season, save_dir)
        use = cuts[trim:len(cuts) - trim] if len(cuts) > 2 * trim else cuts
        out: List[dict] = []
        for cut in use:
            board = load_board_asof("pit", season, cut, save_dir)
            for row in board:
                pid = _row_id(row)
                if pid is None or pid not in full:
                    continue
                pre, n_pre = outcome_counts(row, "pit")
                if n_pre < min_pre:
                    continue
                post, n_all = outcome_counts(full[pid], "pit")
                n_post = n_all - n_pre
                if n_post < min_post:
                    continue
                out.append({
                    "pid": pid, "cutoff": cut, "row": row,
                    "pre": [c / n_pre for c in pre], "n_pre": n_pre,
                    "post": [max(a - b, 0.0) / n_post
                             for a, b in zip(post, pre)], "n_post": n_post,
                })
        return out

    @staticmethod
    def measure_stuff_reliability(season: Optional[int] = None, save_dir: Path = SAVE_DIR,
                                  min_pre: float = 100.0, min_post: float = 100.0
                                  ) -> dict:
        """How much of a pitcher's PREDICTABLE variance his stuff explains.

        Two covariances, both against what he did AFTER the cutoff, so neither
        shares a sampling error with its predictor:

            var_pred = cov(rate before, rate after)   <- what is predictable at
                       all: talent plus whatever recurs (park, defence, role)
            rho2     = cov(delta, rate after)^2 / (var(delta) * var_pred)

        Capped at 0 from below — a negative covariance means the model has
        nothing for that outcome, and zero is the honest encoding of that.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rows = Stuff.stuff_future_rows(season, save_dir, min_pre, min_post)
        model = stuff_model_for(season, save_dir)
        if not rows or model is None:
            return {"n": 0, "season": season}
        # deltas are centred PER CUTOFF, exactly as the rate layer does it
        by_cut: Dict[str, List[dict]] = {}
        for r in rows:
            by_cut.setdefault(r["cutoff"], []).append(r)
        recs: List[dict] = []
        for cut, group in by_cut.items():
            d = stuff_deltas(
                Stuff.stuff_source_board(
                    season, load_board_asof("pit", season, cut, save_dir) or [],
                    cut, save_dir), model)
            for r in group:
                got = d.get(r["pid"])
                if got is not None:
                    recs.append({**r, "delta": got})
        if len(recs) < 30:
            return {"n": len(recs), "season": season}

        out = {"n": len(recs), "season": season,
               "cutoffs": sorted(by_cut), "fit_seasons": model["seasons"]}
        rho2, corr_d, corr_o, var_pred = [], [], [], []
        for i in range(N_OUTCOMES):
            pre = [r["pre"][i] for r in recs]
            post = [r["post"][i] for r in recs]
            dl = [r["delta"][i] for r in recs]
            vp = Stuff._cov(pre, post)
            vd = statistics.pvariance(dl)
            cdp = Stuff._cov(dl, post)
            var_pred.append(vp)
            corr_d.append(_corr(dl, post))
            corr_o.append(_corr(pre, post))
            rho2.append(min(max((cdp * cdp) / (vd * vp), 0.0), 0.95)
                        if vd > 0 and vp > 0 else 0.0)
        out["rho2"] = rho2
        out["corr_delta"] = corr_d
        out["corr_own"] = corr_o
        out["var_pred"] = var_pred
        return out

    @staticmethod
    def measure_recency(side: str = "pit", season: Optional[int] = None,
                        half_lives: Sequence[float] = (250.0, 500.0, 1000.0),
                        save_dir: Path = SAVE_DIR,
                        min_pre: float = 100.0, min_post: float = 100.0,
                        trim: int = 4) -> dict:
        """Does weighting a player's season by recency predict his FUTURE better?

        **The residual, measured before anything is built** — §0.1's own
        instruction, because part of what recency would capture is lineup
        turnover and bullpen state, which a PA simulator already carries
        structurally. Reported both ways: `raw` shows the mechanism but is unfair
        to recency by construction (a weighted estimate has genuinely seen less),
        while `shrunk` regresses each variant at ITS OWN effective sample size,
        which is what the rate layer would really run. **Read `shrunk`** — if
        recency is real it has to survive paying for its own smaller sample.
        """
        season = CURRENT_SEASON if season is None else int(season)
        full = {pid: r for r in (load_board(side, season, save_dir) or [])
                if (pid := _row_id(r)) is not None}
        cuts = available_asof_cutoffs(season, save_dir)
        use = cuts[trim:len(cuts) - trim] if len(cuts) > 2 * trim else cuts
        names = ["season"] + [f"hl{hl:.0f}" for hl in half_lives]
        series: Dict[str, List[float]] = {k: [] for k in names}
        shrunk: Dict[str, List[float]] = {k: [] for k in names}
        actual: List[float] = []
        weights: List[float] = []
        eff_share: List[float] = []
        owners: List[int] = []
        stab = stabilize_for(side)
        for cut in use:
            rows = load_board_asof(side, season, cut, save_dir) or []
            win = board_windows(side, season, rows, cut, save_dir)
            league = league_baseline(rows, side)
            for pid, seq in win.items():
                row = full.get(pid)
                if row is None:
                    continue
                pre = [0.0] * N_OUTCOMES
                n_pre = 0.0
                for c, pa in seq:
                    for i in range(N_OUTCOMES):
                        pre[i] += c[i]
                    n_pre += pa
                if n_pre < min_pre:
                    continue
                post, n_all = outcome_counts(row, side)
                n_post = n_all - n_pre
                if n_post < min_post:
                    continue
                actual.append(rate_run_value([max(a - b, 0.0) / n_post
                                              for a, b in zip(post, pre)]))
                weights.append(n_post)
                owners.append(pid)
                series["season"].append(rate_run_value([c / n_pre for c in pre]))
                shrunk["season"].append(
                    rate_run_value(shrink_rates(pre, league, stab)))
                for hl in half_lives:
                    c, eff = recency_counts(seq, hl)
                    series[f"hl{hl:.0f}"].append(
                        rate_run_value([x / eff for x in c]) if eff > 0
                        else series["season"][-1])
                    # `shrink_rates` reads the sample size off the COUNTS, so
                    # passing the weighted ones prices the smaller effective
                    # sample automatically — no second argument to keep in step.
                    shrunk[f"hl{hl:.0f}"].append(
                        rate_run_value(shrink_rates(c, league, stab))
                        if eff > 0 else shrunk["season"][-1])
                    if hl == half_lives[0]:
                        eff_share.append(eff / n_pre if n_pre else 1.0)
        n = len(actual)
        out = {"n": n, "side": side, "season": season, "cutoffs": list(use),
               "names": names,
               "eff_share": statistics.mean(eff_share) if eff_share else 1.0}
        if n < 30:
            return out
        tot = sum(weights)
        for label, block in (("raw", series), ("shrunk", shrunk)):
            out[label] = {
                name: {
                    "rmse": (sum(w * (p - a) ** 2
                                 for p, a, w in zip(vals, actual, weights))
                             / tot) ** 0.5,
                    "corr": _corr(vals, actual),
                } for name, vals in block.items()}
        # **The error bar has to be CLUSTERED BY PLAYER.** One arm contributes a
        # row at every cutoff and those rows share his talent, his park and most of
        # his future window, so treating 3,315 of them as independent would put a
        # standard error on this roughly sqrt(cutoffs) too small and turn a
        # coin-flip into a finding.
        out["paired"] = {}
        for name in names[1:]:
            by_pid: Dict[int, List[float]] = {}
            for pid, a, w, base, alt in zip(owners, actual, weights,
                                            shrunk["season"], shrunk[name]):
                by_pid.setdefault(pid, []).append(
                    w * ((base - a) ** 2 - (alt - a) ** 2))
            per = [statistics.mean(v) for v in by_pid.values()]
            mu = statistics.mean(per)
            se = statistics.pstdev(per) / len(per) ** 0.5 if len(per) > 1 else 0.0
            out["paired"][name] = {
                "players": len(per), "mean": mu, "se": se,
                "t": (mu / se) if se else 0.0}
        return out

    @staticmethod
    def _cov(a: Sequence[float], b: Sequence[float]) -> float:
        if len(a) < 2:
            return 0.0
        ma, mb = statistics.mean(a), statistics.mean(b)
        return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a)

    @staticmethod
    def score_stuff_prior(season: Optional[int] = None, save_dir: Path = SAVE_DIR,
                          rho2: Optional[Sequence[float]] = None,
                          min_pre: float = 100.0, min_post: float = 100.0
                          ) -> dict:
        """Predict each pitcher's FUTURE rates. Incumbent named, then beaten or not.

        **The incumbent is `build_rates_asof` as it ships** — the shrunk blend at
        the measured `STABILIZE_PA_PIT`, with the playing-time prior and the
        season rebasing. Not league, not the raw observed line. Section 3d.6
        measured against both of those first and the win shrank from 28% to 16%
        when the real incumbent was named, so it is named here up front.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rows = Stuff.stuff_future_rows(season, save_dir, min_pre, min_post)
        if not rows:
            return {"n": 0, "season": season}
        by_cut: Dict[str, List[dict]] = {}
        for r in rows:
            by_cut.setdefault(r["cutoff"], []).append(r)

        # **CHED has to be held OFF for the duration.** This function turns
        # `USE_STUFF_PRIOR` on to build its "stuff" arm, and `build_rates`
        # refuses to run both pitch-characteristic priors at once. Without
        # this the harness raises the moment CHED ships on — and the harness
        # is how the stuff prior gets scored, so it must keep working.
        global USE_STUFF_PRIOR, STUFF_RELIABILITY, USE_CHED_PRIOR
        was_use, was_rho, was_ched = (USE_STUFF_PRIOR, STUFF_RELIABILITY,
                                      USE_CHED_PRIOR)
        USE_CHED_PRIOR = False
        preds: Dict[str, List[List[float]]] = {k: [] for k in
                                               ("league", "own", "incumbent",
                                                "stuff")}
        actual: List[List[float]] = []
        weights: List[float] = []
        try:
            for cut, group in sorted(by_cut.items()):
                USE_STUFF_PRIOR, STUFF_RELIABILITY = False, was_rho
                base, lg = build_rates_asof("pit", season, cut, save_dir=save_dir)
                USE_STUFF_PRIOR = True
                STUFF_RELIABILITY = tuple(rho2) if rho2 is not None else was_rho
                new, _ = build_rates_asof("pit", season, cut, save_dir=save_dir)
                for r in group:
                    pid = r["pid"]
                    if pid not in base or pid not in new:
                        continue
                    preds["league"].append(list(lg))
                    preds["own"].append(r["pre"])
                    preds["incumbent"].append(base[pid]["rates"])
                    preds["stuff"].append(new[pid]["rates"])
                    actual.append(r["post"])
                    weights.append(r["n_post"])
        finally:
            USE_STUFF_PRIOR, STUFF_RELIABILITY = was_use, was_rho
            USE_CHED_PRIOR = was_ched

        n = len(actual)
        out = {"n": n, "season": season, "cutoffs": sorted(by_cut)}
        if n < 30:
            return out
        tot = sum(weights)
        for name, series in preds.items():
            rmse = []
            for i in range(N_OUTCOMES):
                e = sum(w * (p[i] - a[i]) ** 2
                        for p, a, w in zip(series, actual, weights))
                rmse.append((e / tot) ** 0.5)
            rv_p = [rate_run_value(p) for p in series]
            rv_a = [rate_run_value(a) for a in actual]
            out[name] = {
                "rmse": rmse,
                "rv_rmse": (sum(w * (p - a) ** 2
                                for p, a, w in zip(rv_p, rv_a, weights))
                            / tot) ** 0.5,
                "rv_corr": _corr(rv_p, rv_a),
            }
        return out


def rolling_arsenal_row(now: dict, earlier: Optional[dict]) -> dict:
    """`now`, with every per-type average re-expressed over the window since
    `earlier`. Returns `now` unchanged when the window cannot be formed.

    Only the per-type ARSENAL columns are rewritten. Stuff+ and the rest are
    left alone deliberately — they are the base features and their rolling
    version is a separate question, measured separately.
    """
    if not earlier:
        return now
    out = dict(now)
    types = [pt for fam in PITCH_FAMILIES.values() for pt in fam]
    n_win_total = 0.0
    for pt in types:
        n2, n1 = Stuff._type_pitches(now, pt), Stuff._type_pitches(earlier, pt)
        if n2 is None or n1 is None:
            continue
        dn = n2 - n1
        if dn < STUFF_ROLLING_MIN_TYPE:
            continue
        n_win_total += dn
        for key in (f"pfxsp{pt}", f"pfxv{pt}", f"pfx{pt}-X", f"pfx{pt}-Z"):
            a, b = now.get(key), earlier.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                out[key] = (float(a) * n2 - float(b) * n1) / dn
    if n_win_total <= 0:
        return now
    # usage shares are re-expressed over the window too, so the weighting
    # reflects what he has been throwing lately rather than in April
    for pt in types:
        n2, n1 = Stuff._type_pitches(now, pt), Stuff._type_pitches(earlier, pt)
        if n2 is None or n1 is None:
            continue
        out[f"pfx{pt}%"] = max(n2 - n1, 0.0) / n_win_total
    out["Pitches"] = n_win_total
    return out


def _stuff_feats(row: dict) -> Optional[List[float]]:
    """The feature vector for one board row, or None if any column is missing.

    All-or-nothing deliberately: imputing a missing Stuff+ with the mean would
    hand a pitcher we know nothing about the population's prior with a
    confident-looking weight attached.
    """
    out = []
    for k in STUFF_FEATURES:
        v = row.get(k)
        if not isinstance(v, (int, float)):
            return None
        out.append(float(v))
    if STUFF_USE_ARSENAL:
        block = _arsenal_block(row)
        if block is None:
            return None
        out += block
    return out


def fit_stuff_model(seasons: Sequence[int], save_dir: Path = SAVE_DIR,
                    min_tbf: float = 100.0) -> dict:
    """Per-outcome linear model: stuff columns -> rate ABOVE the playing-time prior.

    The target is the RESIDUAL against `playing_time_prior`, not against league,
    so the model cannot take credit for what the rate layer already knows —
    relievers have better stuff AND less playing time, and regressing on the
    deviation from league would pay for that fact twice. Rows are weighted by
    sqrt(TBF), or a few 30-batter lines set the slope.
    """
    rows: List[Tuple[List[float], List[float], float]] = []
    for season in seasons:
        board = load_board("pit", season, save_dir)
        if not board:
            continue
        league = league_baseline(board, "pit")
        per_club = board_pa_per_club(board, "pit")
        for row in board:
            counts, tbf = outcome_counts(row, "pit")
            if tbf < min_tbf:
                continue
            f = _stuff_feats(row)
            if f is None:
                continue
            share = tbf / per_club if per_club else 0.0
            prior = playing_time_prior(share, "pit", league, season,
                                       save_dir, board)
            rows.append((f, [counts[i] / tbf - prior[i]
                             for i in range(N_OUTCOMES)], tbf))
    if len(rows) < 50:
        raise RuntimeError(
            f"mlb_sim: only {len(rows)} pitcher-seasons with stuff columns in "
            f"{list(seasons)} — need the full-season boards on disk")

    # sized off the DATA, not off `STUFF_FEATURES`, so the optional arsenal
    # block cannot silently drop out of the design matrix while still being
    # computed — the shape has exactly one source of truth
    p = len(rows[0][0])
    mean = [statistics.mean(r[0][j] for r in rows) for j in range(p)]
    sd = [statistics.pstdev(r[0][j] for r in rows) or 1.0 for j in range(p)]
    design = [[1.0] + [(r[0][j] - mean[j]) / sd[j] for j in range(p)]
              for r in rows]
    w = [math.sqrt(r[2]) for r in rows]
    aw = [[x * ww for x in a] for a, ww in zip(design, w)]
    fits = Stuff._lstsq(aw, [[r[1][i] * ww for r, ww in zip(rows, w)]
                       for i in range(N_OUTCOMES)])
    coef = {str(i): fits[i] for i in range(N_OUTCOMES)}
    return {"features": (list(STUFF_FEATURES) +
                         (list(STUFF_ARSENAL_FEATURES)
                          if STUFF_USE_ARSENAL else [])),
            "mean": mean, "sd": sd,
            "coef": coef, "seasons": [int(s) for s in seasons],
            "n": len(rows)}


_STUFF_MODEL: Dict[tuple, Optional[dict]] = {}


def stuff_model_for(season: int, save_dir: Path = SAVE_DIR) -> Optional[dict]:
    """The stuff model a run scoring `season` is allowed to use.

    Seasons STRICTLY EARLIER only, same rule as `contact_map_for`: the mapping
    from pitch characteristics to outcomes is league knowledge that could have
    been had before the season started, and fitting it on the season being
    scored is the same leak as a season-final board.
    """
    # **`STUFF_USE_ARSENAL` is in the key because it changes the model's
    # feature WIDTH.** `ab_configure` clears this cache per arm for that reason,
    # but that guard covers only the A/B path. A cache key coarser than the
    # configuration is a guard that cannot fire: trap 23.
    key = (int(season), bool(STUFF_USE_ARSENAL), str(save_dir))
    if key in _STUFF_MODEL:
        return _STUFF_MODEL[key]
    use = [s for s in available_seasons("pit", save_dir) if s < season]
    out: Optional[dict] = None
    if use:
        try:
            out = fit_stuff_model(use, save_dir)
        except (RuntimeError, FileNotFoundError):
            out = None
    _STUFF_MODEL[key] = out
    return out


def stuff_predict(model: dict, row: dict) -> Optional[List[float]]:
    """Predicted rate delta above the playing-time prior, for one board row."""
    f = _stuff_feats(row)
    if f is None:
        return None
    if len(f) != len(model["mean"]):
        # the feature set changed since the model was fit — a silent length
        # mismatch would just mis-index every coefficient
        raise ValueError(
            f"mlb_sim: stuff model has {len(model['mean'])} features, the "
            f"board row yields {len(f)}. Clear _STUFF_MODEL after changing "
            f"STUFF_USE_ARSENAL.")
    z = [1.0] + [(f[j] - model["mean"][j]) / model["sd"][j]
                 for j in range(len(f))]
    return [sum(c * x for c, x in zip(model["coef"][str(i)], z))
            for i in range(N_OUTCOMES)]


def stuff_deltas(board: Sequence[dict], model: Optional[dict],
                 min_tbf: Optional[float] = None) -> Dict[int, List[float]]:
    """{pid: centred, sample-shrunk rate delta} for every arm the model can see.

    **Centred on the population it is applied to**, weighted by the evidence
    behind each row: the model is fit on a completed season and applied to a
    partial one, so an uncentred delta moves the whole league's run level. Fifth
    instance of that trap in this file, so it is done by construction.

    **The shrink is applied BEFORE the centring, not after** — they do not
    commute, because the shrink weight and the delta both rise with playing time,
    and the other order leaves the applied population 0.0007 of a walk per PA off
    league. sim_state.md A.9.
    """
    if not model:
        return {}
    # **Resolved HERE, not as a default argument.** A module constant used as a
    # default is bound at import, so rebinding the global — which is how every
    # calibration and every `_slate_overrides` capture works — silently does not
    # reach it. `STUFF_MIN_TBF` was frozen at 40 and an A/B on it would have
    # reported a clean null.
    min_tbf = STUFF_MIN_TBF if min_tbf is None else min_tbf
    raw: Dict[int, Tuple[List[float], float]] = {}
    for row in board:
        pid = _row_id(row)
        if pid is None:
            continue
        _, tbf = outcome_counts(row, "pit")
        if tbf < min_tbf:
            continue
        d = stuff_predict(model, row)
        if d is None:
            continue
        w = tbf / (tbf + STUFF_SHRINK_TBF)
        raw[pid] = ([x * w for x in d], tbf)
    if not raw:
        return {}
    tot = sum(t for _, t in raw.values()) or 1.0
    centre = [sum(d[i] * t for d, t in raw.values()) / tot
              for i in range(N_OUTCOMES)]
    return {pid: [d[i] - centre[i] for i in range(N_OUTCOMES)]
            for pid, (d, _) in raw.items()}


# ---------------------------------------------------------------------------
# CHED — the pitch model from `ched_core`, as a shrinkage-target shift.
# ---------------------------------------------------------------------------
# **MUTUALLY EXCLUSIVE WITH `USE_STUFF_PRIOR`, and the guard below enforces
# it.** Both read PITCH CHARACTERISTICS into the same prior, so together they
# count a pitcher's stuff twice — and it would look like a working improvement,
# because both terms are individually real.
USE_CHED_PRIOR = True
# Ships ON at the measured persistence rather than at 1.0. A pitcher's CHED is
# DESCRIPTIVE of the pitches he has thrown; the prior wants the forecast, and
# CHED's own year-over-year r is 0.787, so that fraction is what carries. 1.0
# would hand next season his current-season number in full.
CHED_PRIOR_SCALE = 0.787
# **REMOVED, deliberately: there is no minimum.** CHED used to require 80
# pitches HERE and again in the export, so a pitcher at 79 got nothing and at 80
# got full strength. The per-row `rel` replaces both (`ched_core` 2c). The name
# survives only so `_slate_overrides` keeps shipping something the A/B arms may
# reference; nothing reads it in the apply path.
CHED_MIN_PITCHES = 0
# Pitches per plate appearance, league. Converts CHED's per-PITCH run value
# into the per-PA units the tilt works in.
CHED_PITCHES_PER_PA = 3.9
_CHED: Dict[int, dict] = {}


def load_ched(season: int, save_dir: Path = SAVE_DIR) -> Dict[int, dict]:
    """{pid: {ched, rv_delta, n}} for one season, or {} when not exported.

    Written by `ched_train.export`. Returns EMPTY rather than raising when the
    file is absent, because a store without it must still simulate — but see
    `ched_delta` for why a silent empty is dangerous on its own.
    """
    if season in _CHED:
        return _CHED[season]
    try:
        with open(Path(save_dir) / f"ched_{season}.json") as fh:
            raw = json.load(fh)
        _CHED[season] = {int(k): v for k, v in (raw.get("pitchers") or {}).items()}
    except (OSError, ValueError):
        _CHED[season] = {}
    return _CHED[season]


def ched_delta(prior: Sequence[float], rv_delta: float) -> List[float]:
    """A CHED run-value differential as an additive nine-outcome delta.

    CHED predicts ONE number — run value per pitch — and the prior is a rate
    vector, so the scalar is spread by `offence_tilt`, the same primitive HFA and
    the form draw use, rather than by a second runs-to-rates mapping.

    **Sign.** `rv_delta` is negative for a pitcher who SUPPRESSES runs and the
    vector being tilted is what he ALLOWS, so a negative delta must tilt the
    allowed rates down — which `offence_tilt` does with a negative `s`, no flip.
    """
    per_pa = float(rv_delta) * CHED_PITCHES_PER_PA * CHED_PRIOR_SCALE
    tilt = per_pa / (RUNS_PER_TILT / 38.0)
    return [a - b for a, b in zip(offence_tilt(prior, tilt), prior)]


def stuff_prior(prior: Sequence[float], delta: Sequence[float]
                ) -> List[float]:
    """The playing-time prior, moved by what this arm's pitches say about him.

    Additive in RATE space and then renormalised, with a floor so no outcome
    can be argued to zero: the model is linear and a large negative delta on a
    small rate would otherwise cross it.
    """
    out = [max(prior[i] + delta[i], prior[i] * 0.2)
           for i in range(N_OUTCOMES)]
    return _normalize(out)


def stuff_stabilize(base: Sequence[float],
                    rho2: Optional[Sequence[float]] = None,
                    cap: Optional[float] = None) -> Tuple[float, ...]:
    """`base` stabilisation points, re-derived for a prior that knows something.

    M = sigma^2 / var_true. If the prior explains rho2 of var_true, what the
    observations still have to resolve is var_true * (1 - rho2), so
    M_eff = M / (1 - rho2). The estimate is trusted LESS, not more, because
    what it is being weighed against is now better than league.
    """
    # Same reason as `stuff_deltas`: resolved in the body, never as a default.
    # This one silently mattered — `ab_ars.py` set STUFF_RELIABILITY at runtime
    # and the frozen default ignored it, so the arsenal A/B ran arsenal FEATURES
    # against five-column RELIABILITIES.
    rho2 = STUFF_RELIABILITY if rho2 is None else rho2
    cap = STABILIZE_MAX if cap is None else cap
    out = []
    for m, r in zip(base, rho2):
        r = min(max(float(r), 0.0), 0.95)
        out.append(min(m / (1.0 - r), cap))
    return tuple(out)


# Linear run weights per plate appearance, for collapsing a rate vector into
# one number. Diagnostics only — the simulator prices runs by playing them out
# and never uses these. Ordered as the outcome vector is.
RUN_VALUE_PER_PA: Tuple[float, ...] = (
    -0.27,   # K
    +0.33,   # BB
    +0.35,   # HBP
    -0.27,   # GB_OUT
    -0.27,   # AIR_OUT
    +0.47,   # 1B
    +0.78,   # 2B
    +1.09,   # 3B
    +1.40,   # HR
)


def rate_run_value(rates: Sequence[float]) -> float:
    return sum(r * w for r, w in zip(rates, RUN_VALUE_PER_PA))


# ---------------------------------------------------------------------------
# WITHIN-SEASON RECENCY — sim_state.md 0.1 Objective 2 / A.9
# ---------------------------------------------------------------------------
# `recency_weights` shipped with the module citing arXiv:2511.17733 — **but
# nothing called `weighted_counts`.** The rate layer ran season totals, which
# carry no ordering, so the only recency was `SEASON_HALF_LIFE` ACROSS seasons.
# The ordering is recoverable by DIFFERENCING the cumulative as-of boards, at no
# new fetch. Ages are in PLATE APPEARANCES, not days — the calendar is the wrong
# clock for a reliever. Measured before any of it was wired, and it does NOT
# apply to both sides.
RECENCY_HALF_LIFE_BAT = 500.0
RECENCY_HALF_LIFE_PIT = 0.0        # 0 = off; measured null, seasons disagree
USE_RECENCY = False                # ships off until the closing-line A/B says


def recency_half_life(side: str) -> float:
    return RECENCY_HALF_LIFE_PIT if side == "pit" else RECENCY_HALF_LIFE_BAT


def board_windows(side: str, season: int,
                  terminal: Sequence[dict],
                  as_of: Optional[str] = None,
                  save_dir: Path = SAVE_DIR) -> Dict[int, List[tuple]]:
    """{pid: [(counts, pa), ...]} per cutoff window, OLDEST first.

    Each window is one cached as-of board minus the one before it. `terminal`
    closes the sequence and is PASSED IN rather than re-derived: it is the newest
    and most important window, and looking it up by date would silently drop it
    whenever no board happened to be cached that day, leaving a rate layer that
    quietly ignored the last three weeks. `as_of` drops cutoffs on or after it —
    the backtest's one rule, applied here too.
    """
    cuts = [c for c in available_asof_cutoffs(season, save_dir)
            if as_of is None or c < as_of]
    # **A cutoff board NEWER than the terminal one is not a window, it is a
    # contradiction** — the differencing would hand the rate layer a season the
    # rest of it has never seen, silently, because the sequence still looks well
    # formed. Drop those cutoffs instead.
    have = board_pa_per_club(terminal, side)
    if have > 0:
        cuts = [c for c in cuts
                if board_pa_per_club(
                    load_board_asof(side, season, c, save_dir) or [],
                    side) <= have]
    if not cuts:
        return {}
    cum: List[Dict[int, Tuple[List[float], float]]] = []
    for c in list(cuts) + [None]:
        rows = (terminal if c is None
                else load_board_asof(side, season, c, save_dir))
        got: Dict[int, Tuple[List[float], float]] = {}
        for row in rows or []:
            pid = _row_id(row)
            if pid is None:
                continue
            counts, pa = outcome_counts(row, side)
            if pa > 0:
                got[pid] = (counts, pa)
        cum.append(got)

    out: Dict[int, List[tuple]] = {}
    pids = {pid for snap in cum for pid in snap}
    zero = ([0.0] * N_OUTCOMES, 0.0)
    for pid in pids:
        seq = []
        prev = zero
        for snap in cum:
            cur = snap.get(pid, prev)
            pa = cur[1] - prev[1]
            if pa > 0:
                seq.append(([max(a - b, 0.0)
                             for a, b in zip(cur[0], prev[0])], pa))
            prev = cur
        if seq:
            out[pid] = seq
    return out


def recency_counts(windows: Sequence[tuple],
                   half_life: float = 500.0) -> Tuple[List[float], float]:
    """Recency-weighted counts from a chronological sequence of windows.

    The per-PA schedule of `recency_weights` integrated over each window, so
    this and `weighted_counts` agree in the limit of one PA per window. The
    newest plate appearance carries weight 1, so the returned total is an
    EFFECTIVE sample size and is smaller than the raw one — which is correct
    and is the cost of the method: a recency-weighted estimate has genuinely
    seen less, and shrinkage must be told so rather than handed the raw count.
    """
    decay = math.log(2.0) / half_life if half_life > 0 else 0.0
    ages: List[Tuple[float, float]] = []          # (age at window end, at start)
    age = 0.0
    for _, pa in reversed(windows):               # newest first
        ages.append((age, age + pa))
        age += pa
    ages.reverse()
    counts = [0.0] * N_OUTCOMES
    eff = 0.0
    for (c, pa), (a0, a1) in zip(windows, ages):
        if pa <= 0:
            continue
        if decay <= 0:
            w = 1.0
        else:
            w = (math.exp(-decay * a0) - math.exp(-decay * a1)) / (decay * pa)
        for i in range(N_OUTCOMES):
            counts[i] += c[i] * w
        eff += pa * w
    return counts, eff


# ---------------------------------------------------------------------------
# Board loading
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# AS-OF boards — the seam a leakage-free backtest needs
# ---------------------------------------------------------------------------
# A season board on disk is season-FINAL: projecting a May game off it feeds the
# model the rest of that season, the game included. **FanGraphs will serve the
# board as of a date** — `month=1000` with `startdate`/`enddate`, previously
# recorded here as impossible. Two checked differences: pitching omits 1B/2B/3B
# (already derived) and batting omits `XBR`, which degrades to speed alone.
# `/api/leaders` 403s a plain request, so this needs headless Firefox. A.9.
ASOF_DIR = SAVE_DIR / "asof"



def asof_board_path(side: str, season: int, as_of: str,
                    save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / "asof" / f"fg_{side}_{season}_{as_of}.json"


# The same board WITHOUT a date window. `month=0` is the full season, which is
# what `fg_{bat,pit}_<season>.json` on disk are.


_ASOF_BOARDS: Dict[tuple, Optional[List[dict]]] = {}


def _read_json_list(path: Path) -> Optional[List[dict]]:
    """A JSON file expected to hold a list of rows, or None if it is absent or
    holds something else.

    Only the READ is shared with `load_board`, deliberately not the memo
    check: `load_board` is reached per-PLAYER (3,700 times on a full slate),
    and folding the lookup in here would make every cache HIT pay a path
    construction it does not pay today.
    """
    if not path.exists():
        return None
    with open(path) as fh:
        got = json.load(fh)
    return got if isinstance(got, list) else None


def load_board_asof(side: str, season: int, as_of: str,
                    save_dir: Path = SAVE_DIR) -> Optional[List[dict]]:
    """The cached as-of board, or None. Never fetches — fetch in a batch.

    Memoised for the same reason `load_board` is: `board_windows` reads every
    cutoff up to its own to difference them, so one rate build touches up to
    twenty multi-MB files and a backtest would re-parse each of them once per
    cutoff. Rows are read-only everywhere.
    """
    key = (side, int(season), str(as_of), str(save_dir))
    if key in _ASOF_BOARDS:
        return _ASOF_BOARDS[key]
    data = _read_json_list(asof_board_path(side, season, as_of, save_dir))
    _ASOF_BOARDS[key] = data
    return data


def build_rates_asof(side: str, season: int, as_of: str,
                     half_life: Optional[float] = None,
                     save_dir: Path = SAVE_DIR
                     ) -> Tuple[Dict[int, dict], List[float]]:
    """`build_rates` with the newest season TRUNCATED at `as_of`.

    Older seasons are used whole, which is correct — they finished before the
    game being predicted — and `rebase_to_season` maps them onto the AS-OF
    environment, because the target league baseline is computed from the
    truncated board like everything else.
    """
    half_life = SEASON_HALF_LIFE if half_life is None else float(half_life)
    rows = load_board_asof(side, season, as_of, save_dir)
    if rows is None:
        raise FileNotFoundError(
            f"mlb_sim: no cached as-of board for {side} {season} {as_of}. "
            f"Run fetch_boards_asof([...]) first — it needs headless Firefox.")
    older = ((list(RateIngest.rate_seasons(side)) or available_seasons(side, save_dir))
             if RateIngest.use_season_blend(side) else [])
    boards = {s: load_board(side, s, save_dir) for s in older if s < season}
    boards[season] = rows
    # The contact prior is as-of too: pitches strictly before the cutoff, with
    # the PRIOR season whole underneath it (it finished before any game being
    # priced, so it leaks nothing).
    return build_rates(side, half_life=half_life, save_dir=save_dir,
                       boards=boards,
                       # Passed unconditionally now: this names WHICH season's
                       # profiles to read, and `build_rates` owns the on/off.
                       bmielke_season=season,
                       bmielke_asof_date=as_of,
                       # Within-season recency reads the SAME cutoff rule: only
                       # boards strictly before this one may contribute a
                       # window, and this board closes the sequence.
                       as_of=as_of)


_BOARDS: Dict[tuple, Optional[List[dict]]] = {}


def load_board(side: str, season: int,
               save_dir: Path = SAVE_DIR) -> Optional[List[dict]]:
    """Load a cached FanGraphs board. Returns None when it is not on disk.

    **Memoised, and it matters far more than it looks.** These are multi-MB
    JSON files and several callers reach for one per PLAYER rather than per
    run — `start_bf_estimate` scans the pitching board for a single id, so a
    slate of 1,848 games re-read and re-parsed it 3,700 times and spent 95% of
    the harness in `json.load`. Rows are treated as read-only everywhere; the
    only mutation in the module is on objects built FROM them.
    """
    key = (side, int(season), str(save_dir))
    if key in _BOARDS:
        return _BOARDS[key]
    data = _read_json_list(RateIngest._shared(save_dir)
                           / f"fg_{side}_{season}.json")
    _BOARDS[key] = data
    return data


def available_seasons(side: str, save_dir: Path = SAVE_DIR) -> List[int]:
    out = []
    for p in RateIngest._shared(save_dir).glob(f"fg_{side}_*.json"):
        try:
            out.append(int(p.stem.rsplit("_", 1)[1]))
        except (ValueError, IndexError):
            continue
    return sorted(out)


def _row_id(row: dict) -> Optional[int]:
    v = row.get("xMLBAMID")
    return int(v) if isinstance(v, (int, float)) and v else None


# --- platoon splits --------------------------------------------------------

def _bat_hand(code: object) -> str:
    """Canonical batting hand: "L", "R", or "B" for a switch hitter.

    **FanGraphs spells a switch hitter "B", not "S", and this cost the platoon
    term a tenth of the league** — the table was built under ("L", "R", "S") so
    the row was never written, and the lookup then missed. Silent both ways: a
    two-row table looks deliberate, and a miss is indistinguishable from
    "handedness unknown". 10.7% of league PA, against a real gap of -0.0060 RV/PA.

    **Not applied at the source.** `Batter.bats` keeps the raw board value:
    `mlb_ml._HAND_CODE` also keys switch hitters on "S", and its models were
    TRAINED through that encoding. That twin needs a fix and a retrain together.
    """
    c = str(code or "").strip().upper()[:1]
    return "B" if c == "S" else c


# FanGraphs' splits API. One POST returns the WHOLE LEAGUE for one split.
# **Deliberately DUPLICATED from `EffortMLB.fetch_fg_split_sync`** for the same
# reason `VENUE_ALIASES` is — importing EffortMLB drags in Qt. If the endpoint
# or the split ids change, both need the edit. The payload is COLUMN-oriented,
# unlike every other FanGraphs endpoint, and rows key on FanGraphs' `playerId`,
# not MLBAM — `load_board` already has the row, which is the only reason this
# joins at all.
class Boards:
    """Fetching and loading the boards themselves — season, as-of, and splits."""

    @staticmethod
    @contextlib.contextmanager
    def _fg_driver():
        """ONE headless Firefox for a whole batch of board fetches.

        Starting it costs several seconds, so a per-fetch browser would dominate a
        weekly cutoff grid. Selenium is imported lazily so the module stays
        importable without it.
        """
        from selenium import webdriver                      # lazy: heavy, optional
        from selenium.webdriver.firefox.options import Options

        opts = Options()
        opts.add_argument("-headless")
        driver = webdriver.Firefox(options=opts)
        driver.set_page_load_timeout(60)
        try:
            driver.get("https://www.fangraphs.com/robots.txt")
            yield driver
        finally:
            driver.quit()

    @staticmethod
    def _fg_rows(driver, path: str, label: str, verbose: bool = True,
                 wait_s: int = 90) -> Optional[List[dict]]:
        """Run one `/api/leaders` fetch INSIDE the page and return its `data`.

        The request has to originate from a fangraphs.com document — a plain
        request 403s at Cloudflare — so it goes through `fetch()` in the page and
        the result is polled off `window`.
        """
        driver.execute_script(
            "window.__fgasof=null;"
            f"fetch('{path}').then(r=>r.text())"
            ".then(t=>{window.__fgasof=t}).catch(e=>{window.__fgasof='ERR:'+e});")
        out = None
        for _ in range(wait_s):
            time.sleep(1)
            out = driver.execute_script("return window.__fgasof")
            if out:
                break
        if not out or str(out).startswith("ERR:"):
            if verbose:
                print(f"[fg] {label}: FAILED {str(out)[:60]}")
            return None
        try:
            return json.loads(out).get("data", [])
        except ValueError:
            # An HTML challenge page parses as "Expecting value: line 1 column 1",
            # which says nothing about the cause. Say what it is.
            if verbose:
                print(f"[fg] {label}: non-JSON ({len(out)}B), "
                      f"likely a Cloudflare challenge")
            return None

    @staticmethod
    def fetch_boards_asof(dates: Sequence[str], season: Optional[int] = None,
                          sides: Sequence[str] = ("bat", "pit"),
                          save_dir: Path = SAVE_DIR,
                          force: bool = False, verbose: bool = True
                          ) -> Dict[tuple, int]:
        """Fetch and cache season-to-date boards for each cutoff in `dates`."""
        season = CURRENT_SEASON if season is None else int(season)
        todo = [(side, d) for d in dates for side in sides
                if force or not asof_board_path(side, season, d, save_dir).exists()]
        got: Dict[tuple, int] = {}
        if not todo:
            if verbose:
                print("[asof] all cutoffs already cached")
            return got

        with Boards._fg_driver() as driver:
            for side, as_of in todo:
                path = FanGraphs.ASOF_PATH.format(stats=side, season=season,
                                           start=f"{season}-01-01", end=as_of)
                rows = Boards._fg_rows(driver, path, f"{side} {as_of}", verbose)
                if rows is None:
                    continue
                dest = asof_board_path(side, season, as_of, save_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "w") as fh:
                    json.dump(rows, fh)
                got[(side, as_of)] = len(rows)
                if verbose:
                    print(f"[asof] {side} {as_of}: {len(rows)} rows")
        return got

    @staticmethod
    def fetch_season_boards(seasons: Sequence[int],
                            sides: Sequence[str] = ("bat", "pit"),
                            save_dir: Path = SAVE_DIR,
                            force: bool = False, verbose: bool = True
                            ) -> Dict[tuple, int]:
        """Fetch and cache FULL-SEASON boards — `OddsAPI/savedata/fg_<side>_<season>.json`.

        Hitters carried 2026 only while pitchers carried 2024-26, so every matchup
        blended three years of arm against one year of bat. `build_rates` picks up
        whatever seasons are on disk, so this is a data fetch and not a model
        change — but it moves every hitter's effective sample, so A/B it on the
        slate rather than assuming it helps.
        """
        todo = [(side, s) for s in seasons for side in sides
                if force or not (RateIngest._shared(save_dir) / f"fg_{side}_{s}.json").exists()]
        got: Dict[tuple, int] = {}
        if not todo:
            if verbose:
                print("[boards] all seasons already cached")
            return got

        with Boards._fg_driver() as driver:
            for side, season in todo:
                path = FanGraphs.SEASON_PATH.format(stats=side, season=season)
                rows = Boards._fg_rows(driver, path, f"{side} {season}", verbose)
                if rows is None:
                    continue
                dest = RateIngest._shared(save_dir) / f"fg_{side}_{season}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "w") as fh:
                    json.dump(rows, fh)
                got[(side, season)] = len(rows)
                _BOARDS.pop((side, int(season), str(save_dir)), None)
                if verbose:
                    print(f"[boards] {side} {season}: {len(rows)} rows")
        return got

    @staticmethod
    def fetch_fg_split(split_id: int, stat_type: str = FanGraphs.SPLIT_STANDARD,
                       position: str = "B", season: Optional[int] = None,
                       save_dir: Path = SAVE_DIR, refresh: bool = False,
                       timeout: float = 45.0) -> Dict[int, dict]:
        """One split for the whole league, keyed by FanGraphs playerId.

        Cached to disk — splits move once a day at most. Returns {} on failure,
        which callers must treat as "unavailable", never as "zero PA".
        """
        season = CURRENT_SEASON if season is None else int(season)
        path = save_dir / f"fg_split_{position}_{stat_type}_{split_id}_{season}.json"
        if path.exists() and not refresh:
            try:
                with open(path) as fh:
                    return {int(k): v for k, v in json.load(fh).items()}
            except (OSError, ValueError):
                pass
        body = {
            "strPlayerId": "all", "strSplitArr": [split_id],
            "strGroup": "season", "strPosition": position, "strType": stat_type,
            "strStartDate": f"{season}-03-01", "strEndDate": f"{season}-11-01",
            "strSplitTeams": False, "dctFilters": [], "strStatType": "player",
            "strAutoPt": "false", "arrPlayerId": [], "strSplitArrPitch": [],
            "arrWxTemperature": None, "arrWxPressure": None,
            "arrWxAirDensity": None, "arrWxElevation": None,
            "arrWxWindSpeed": None,
        }
        try:
            r = requests.post(FanGraphs.SPLITS_URL, json=body, timeout=timeout)
            if r.status_code != 200:
                print(f"mlb_sim: FG split {split_id} HTTP {r.status_code}")
                return {}
            payload = r.json()
        except Exception as e:
            print(f"mlb_sim: FG split {split_id} failed: {e}")
            return {}

        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict) and "k" in data and "v" in data:
            cols, rows = data["k"], data["v"]
            recs = [dict(zip(cols, row)) for row in rows]
        elif isinstance(data, list):
            recs = data
        else:
            return {}
        out: Dict[int, dict] = {}
        for rec in recs:
            pid = rec.get("playerId", rec.get("playerid"))
            if pid is None:
                continue
            out[int(pid)] = rec
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                json.dump({str(k): v for k, v in out.items()}, fh)
        except OSError:
            pass
        return out

    @staticmethod
    def league_platoon_gaps(season: Optional[int] = None, save_dir: Path = SAVE_DIR,
                            refresh: bool = False) -> Dict[str, List[float]]:
        """{bats: 9-vector of (vs LHP - vs RHP) rate gaps}, PA-weighted.

        Derived from the splits boards rather than hardcoded, so it tracks the
        league. Switch hitters get their own row — they turn around, so their gap
        is small and must not inherit either pure row — keyed "B" via `_bat_hand`.

        **The cache filename is VERSIONED**, because a file written before that
        fix holds a two-row table that loads without error, silently reinstates
        the bug, and is indistinguishable from a season with no switch hitters.
        """
        season = CURRENT_SEASON if season is None else int(season)
        path = save_dir / f"platoon_gaps_v2_{season}.json"
        if path.exists() and not refresh:
            try:
                with open(path) as fh:
                    return {k: list(v) for k, v in json.load(fh).items()}
            except (OSError, ValueError):
                pass

        board = load_board("bat", season, save_dir)
        bats = {int(r["playerid"]): str(r.get("Bats") or "")
                for r in board if r.get("playerid") is not None}
        vs = {"L": Boards.fetch_fg_split(FanGraphs.SPLIT_VS_LHP, FanGraphs.SPLIT_STANDARD, "B",
                                  season, save_dir),
              "R": Boards.fetch_fg_split(FanGraphs.SPLIT_VS_RHP, FanGraphs.SPLIT_STANDARD, "B",
                                  season, save_dir)}
        if not vs["L"] or not vs["R"]:
            return {}

        # PA-weighted league rate vector per (batter hand, pitcher hand)
        acc: Dict[tuple, List[float]] = {}
        tot: Dict[tuple, float] = {}
        for hand, table in vs.items():
            for pid, rec in table.items():
                b = _bat_hand(bats.get(pid))
                if not b:
                    continue
                counts, pa = outcome_counts(rec, "bat")
                if pa <= 0:
                    continue
                key = (b, hand)
                cur = acc.setdefault(key, [0.0] * N_OUTCOMES)
                for i, c in enumerate(counts):
                    cur[i] += c
                tot[key] = tot.get(key, 0.0) + pa

        out: Dict[str, List[float]] = {}
        for b in ("L", "R", "B"):
            kl, kr = (b, "L"), (b, "R")
            # Both sides need enough PA for a rate to mean anything. Switch
            # hitters clear this comfortably (4,552 vs LHP / 10,318 vs RHP in
            # 2026), so the missing row was never this gate firing.
            if tot.get(kl, 0) < 500 or tot.get(kr, 0) < 500:
                continue
            rl = [c / tot[kl] for c in acc[kl]]
            rr = [c / tot[kr] for c in acc[kr]]
            out[b] = [a - c for a, c in zip(rl, rr)]
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                json.dump(out, fh, indent=1)
        except OSError:
            pass
        return out

    @staticmethod
    def gmli_stabilizer(season: Optional[int] = None, save_dir: Path = SAVE_DIR) -> float:
        """Relief appearances at which a pitcher's gmLI is half-believed.

        MEASURED, not chosen: the league median relief-appearance count, so a
        typical arm is trusted about halfway and a one-game callup is not. Writing
        a number here by hand was the original version and it disagreed with its
        own comment by 2x.
        """
        season = CURRENT_SEASON if season is None else int(season)
        global _GMLI_STABILIZER
        if _GMLI_STABILIZER is not None:
            return _GMLI_STABILIZER
        rows = load_board("pit", season, save_dir) or []
        apps = [_num(r, "G") for r in rows
                if _num(r, "G") and _num(r, "GS") / max(_num(r, "G"), 1.0) < 0.5]
        apps.sort()
        _GMLI_STABILIZER = float(apps[len(apps) // 2]) if apps else 15.0
        return _GMLI_STABILIZER

    @staticmethod
    def runner_profile(row: dict) -> dict:
        """Per-player running game from a FanGraphs batting row.

        {steal_attempt, steal_success, speed}. Both steal terms are shrunk toward
        the league — three-for-three is not a 100% base stealer — and `speed` is
        an ODDS multiplier on every extra-base roll, not a probability. Verified
        against the 270 regulars: mean success 0.778 against their real 0.769.

        **The "known residual" here was a COUNTING BUG, fixed 2026-08-20** and
        wrongly blamed on a missing BATTERY term: a steal was inferred from the
        state change, which the wild-pitch branch produces exactly. Crediting it
        from the EVENT gives 0.7714 against a real 0.769. sim_state.md A.9.
        """
        sb, cs = _num(row, "SB"), _num(row, "CS")
        on_first = _num(row, "1B") + _num(row, "BB") + _num(row, "HBP")
        opp = max(on_first * OPP_PER_TIME_ON_FIRST, 0.0)
        att = sb + cs

        if opp > 0:
            w = opp / (opp + STABILIZE_STEAL_ATTEMPT)
            attempt = w * (att / opp) + (1.0 - w) * LG_STEAL_ATTEMPT
        else:
            attempt = LG_STEAL_ATTEMPT

        if att > 0:
            w = att / (att + STABILIZE_STEAL_SUCCESS)
            success = w * (sb / att) + (1.0 - w) * LG_STEAL_SUCCESS
        else:
            success = LG_STEAL_SUCCESS

        spd = _num(row, "Spd", LG_SPD)
        speed = math.exp(SPD_TO_ODDS * (spd - LG_SPD)) if spd > 0 else 1.0

        return {
            "steal_attempt": min(max(attempt, 0.0), 0.85),
            "steal_success": min(max(success, 0.30), 0.95),
            "speed": min(max(speed, 0.50), 2.00),
        }

    @staticmethod
    def _board_index(side: str, season: int,
                     save_dir: Path) -> Dict[int, dict]:
        """{player id: board row} for one side, built once per season+store.

        `_bat_row` and `_pit_row` were the same eleven lines with the side and
        the memo swapped.
        """
        memo = _BAT_ROWS if side == "bat" else _PIT_ROWS
        key = (int(season), str(save_dir))
        tab = memo.get(key)
        if tab is None:
            tab = {}
            for row in load_board(side, season, save_dir) or []:
                rid = _row_id(row)
                if rid:
                    tab[rid] = row
            memo[key] = tab
        return tab

    @staticmethod
    def _bat_row(pid: int, season: Optional[int] = None,
                 save_dir: Path = SAVE_DIR) -> Optional[dict]:
        """One hitter's board row, by id. Indexed, not scanned."""
        season = CURRENT_SEASON if season is None else int(season)
        return Boards._board_index("bat", season, save_dir).get(int(pid))

    @staticmethod
    def starter_pitch_hazard() -> List[float]:
        """League pitch-indexed hook curve, memoised. [] when unavailable."""
        if not _PITCH_HAZ:
            _PITCH_HAZ.append(Fatigue.real_starter_pitch_hazard() or [])
        return _PITCH_HAZ[0]

    @staticmethod
    def milb_only_pitcher(pid: int, season: Optional[int] = None,
                          save_dir: Path = SAVE_DIR, *,
                          is_starter: bool = False,
                          hazard: Optional[List[float]] = None
                          ) -> Optional[Pitcher]:
        """A pitcher with NO major-league board row, built from the minors.

        None when the ladder has nothing on him, so the caller keeps its existing
        fallback — this can only improve on "price the debut as the club's ace",
        never invent a line. His translated minor-league rate is the shrinkage
        TARGET and his translated counts are the evidence; a 333-batter Double-A
        line is real information but is not 333 major-league batters, and
        `credit` is what encodes the difference.
        """
        season = CURRENT_SEASON if season is None else int(season)
        if not USE_MILB_PRIOR:
            return None
        tr = load_milb_translation(save_dir)
        lvf = (tr.get("factor_by_level") or {}).get("pit")
        if not lvf:
            fac = (tr.get("factor") or {}).get("pit")
            if not fac:
                return None
            lvf = {"AAA": list(fac)}
        ck = "credit_applied" if MILB_CREDIT_SPEC == "applied" else "credit"
        cred = list((tr.get(ck) or {}).get("pit") or [])
        lv = (load_milb(season, save_dir).get("pit") or {}).get(str(int(pid)))
        rates, n = MiLB.milb_evidence(lv, lvf)
        if rates is None or n <= 0:
            return None
        league = league_baseline(load_board("pit", season, save_dir) or [], "pit")

        # **CHED, on the path where it matters most.** This is the DEBUT case
        # — a pitcher with no major-league board row — and it builds a `Pitcher`
        # directly, bypassing every prior `build_rates` applies. So the one arm
        # whose entire record is Triple-A was the one arm CHED could not reach.
        # It shifts the SHRINKAGE TARGET through the same helpers, so the two
        # paths cannot drift on what CHED means.
        if USE_CHED_PRIOR:
            ch_rec = load_ched(season, save_dir).get(int(pid))
            if ch_rec:
                rel = float(ch_rec.get("rel", 0.0))
                if rel > 0.0:
                    league = stuff_prior(
                        league, ched_delta(league, ch_rec["rv_delta"] * rel))

        stab = stabilize_for("pit")
        if cred and any(cred):
            eff = [cred[i] * n for i in range(N_OUTCOMES)]
            got = [(eff[i] / (eff[i] + stab[i])) * rates[i]
                   + (stab[i] / (eff[i] + stab[i])) * league[i]
                   for i in range(N_OUTCOMES)]
        else:
            got = list(rates)
        name = f"{Boards._milb_name(pid, season, save_dir) or ('#' + str(pid))} [MiLB]"
        # **`throws` used to be the empty string here, which is a second
        # silent null on the same path.** `platoon_rates` no-ops when either
        # hand is unknown — the honest default when it IS unknown — so every
        # batter faced a debut with no handedness term at all. It is not
        # unknown: the raw Triple-A cache carries `p_throws` on every pitch.
        return Pitcher(name=name, rates=_normalize(got), player_id=int(pid),
                       is_starter=is_starter, hazard=list(hazard or []),
                       pitch_hazard=(Boards.starter_pitch_hazard() if is_starter
                                     and USE_PITCH_HOOK else []),
                       throws=Boards.milb_throws(pid, season, save_dir) or "")

    @staticmethod
    def milb_throws(pid: int, season: int,
                    save_dir: Path = SAVE_DIR) -> Optional[str]:
        """"L"/"R" for a minor leaguer, off the raw Triple-A pitch cache.

        Memoised: the debut path is reached per game side, and this would
        otherwise re-open a gzip for a one-character answer.
        """
        key = (int(season), int(pid))
        if key in _MILB_THROWS:
            return _MILB_THROWS[key]
        out = None
        try:
            path = (Path(save_dir) / "milb_pitches" / MILB_ARSENAL_VERSION
                    / str(season) / f"{int(pid)}.json.gz")
            if path.exists():
                with gzip.open(path, "rt") as fh:
                    for row in json.load(fh):
                        h = (row.get("p_throws") or "").upper()[:1]
                        if h in ("L", "R"):
                            out = h
                            break
        except (OSError, ValueError):
            out = None
        _MILB_THROWS[key] = out
        return out

    @staticmethod
    def _milb_name(pid: int, season: int, save_dir: Path) -> Optional[str]:
        """A minor leaguer's name.

        The MiLB snapshot stores counts only, so this reads the roster cache —
        without it the banner prints `#807739 [MiLB]`, and a starter you cannot
        NAME is one nobody will sanity-check.
        """
        if not _MILB_NAMES:
            try:
                with open(RateIngest._shared(save_dir) / f"mlb_roster_{season}.json") as fh:
                    for p in (json.load(fh).get("players") or []):
                        if p.get("id") and p.get("fullName"):
                            _MILB_NAMES[int(p["id"])] = str(p["fullName"])
            except (OSError, ValueError, KeyError):
                _MILB_NAMES[-1] = ""
        got = _MILB_NAMES.get(int(pid))
        if got:
            return got
        # **The roster snapshot is stale for exactly the players this matters
        # for.** A debut is called up after the snapshot was taken, so the man the
        # ladder exists to price is the one it cannot name. One cheap lookup,
        # cached to disk, rather than printing an id.
        cache = Path(save_dir) / "milb_names.json"
        disk: Dict[str, str] = {}
        try:
            with open(cache) as fh:
                disk = json.load(fh)
        except (OSError, ValueError):
            pass
        if str(pid) in disk:
            _MILB_NAMES[int(pid)] = disk[str(pid)]
            return disk[str(pid)]
        try:
            r = requests.get(f"{STATSAPI}/people/{int(pid)}",
                             params={"fields": "people,id,fullName"},
                             timeout=StatsApi.TIMEOUT)
            r.raise_for_status()
            nm = ((r.json().get("people") or [{}])[0] or {}).get("fullName")
        except Exception:                                    # noqa: BLE001
            nm = None
        if nm:
            disk[str(pid)] = str(nm)
            _MILB_NAMES[int(pid)] = str(nm)
            try:
                with open(cache, "w") as fh:
                    json.dump(disk, fh)
            except OSError:
                pass
        return nm




# Share of a hitter's plate appearances taken against a LEFT-handed pitcher.
# MEASURED: 0.290 league PA-weighted, which is what the sim realises.
#
# **This is the CENTRING constant, and the whole reason the split can be applied
# at all** — a season rate is already ~71% the vs-RHP number, so adding a raw
# gap counts handedness twice. With G the vs-LHP-minus-vs-RHP gap:
#
#     rate_vs_LHP = overall + (1 - w_L) * G
#     rate_vs_RHP = overall -      w_L  * G
#
# so his PA-weighted average over his real opponent mix returns his season rate
# by construction. sim_state.md A.9.
PLATOON_PA_SHARE_VS_LHP = 0.290

# How much of a PLAYER's OWN deviation from his handedness' league gap to
# believe. **Measured, and it is small** — split-half reliability 0.26 on K,
# 0.24 on BB, and **-0.06 on HR: individual home-run platoon skill is ZERO over
# a season.** Anyone reading a hitter's own vs-LHP home-run rate is reading
# noise, and it looks perfectly reasonable while doing it. sim_state.md A.9.
PLATOON_OWN_RELIABILITY = 0.25


# **Keyed by season, like every other cache in this module.** It was a single
# Optional slot keyed on NOTHING, so `platoon_rates(season=2024)` returned the
# 2026 answer byte-for-byte — and it was the one board cache missing from both
# pool workers' clear lists, so a multi-season backtest priced every season with
# whichever loaded first.
_PLATOON_GAPS: Dict[int, Dict[str, List[float]]] = {}


def platoon_rates(rates: Sequence[float], bats: str, throws: str,
                  season: Optional[int] = None,
                  w_l: Optional[float] = None) -> List[float]:
    """A hitter's rates against THIS pitcher's hand, centred on his own mix.

    No-ops when either hand is unknown, which is the honest default — the
    alternative is to guess a handedness and apply a real effect off it.
    """
    w_l = PLATOON_PA_SHARE_VS_LHP if w_l is None else float(w_l)
    season = CURRENT_SEASON if season is None else int(season)
    if not bats or not throws:
        return list(rates)
    t = throws.upper()[:1]
    if t not in ("L", "R"):
        return list(rates)
    gaps = _PLATOON_GAPS.get(season)
    if gaps is None:
        try:
            gaps = Boards.league_platoon_gaps(season)
        except Exception:
            gaps = {}
        # cached even when empty, so a season with no splits on disk does not
        # re-attempt the load on every one of the ~3,000 PA in a game
        _PLATOON_GAPS[season] = gaps
    gap = gaps.get(_bat_hand(bats))
    if not gap:
        return list(rates)
    # Centred: the season rate already contains his real opponent mix.
    f = (1.0 - w_l) if t == "L" else -w_l
    out = [max(0.0, r + f * g) for r, g in zip(rates, gap)]
    s = sum(out)
    return [v / s for v in out] if s > 0 else list(rates)


def build_rates(side: str, seasons: Optional[Sequence[int]] = None,
                half_life: Optional[float] = None,
                save_dir: Path = SAVE_DIR,
                boards: Optional[Dict[int, List[dict]]] = None,
                bmielke_season: Optional[int] = None,
                bmielke_asof_date: Optional[str] = None,
                as_of: Optional[str] = None
                ) -> Tuple[Dict[int, dict], List[float]]:
    """Every player on the board as a shrunk outcome vector.

    Returns ({mlbam_id: {name, rates, pa}}, league_baseline).

    `boards` overrides what is loaded from disk, keyed by season — the seam the
    AS-OF path uses. Pass a partial newest season and full older ones and
    everything downstream (baseline, season rebasing, playing-time prior) is
    computed against the partial board, because each is derived from `boards`.

    `as_of` bounds which cached boards may contribute a WITHIN-SEASON recency
    window. It is a cutoff rule, not a data source: the newest season's counts
    still come from `boards`, which is what stops the recency path having a
    second, differently-frozen view of the season.
    """
    half_life = SEASON_HALF_LIFE if half_life is None else float(half_life)
    # **Two pitch-characteristic priors on the same rate vector is a DOUBLE
    # COUNT.** Loud rather than silent: both terms are individually real, so
    # the combination would read as an improvement while charging a pitcher's
    # stuff twice.
    if USE_STUFF_PRIOR and USE_CHED_PRIOR:
        raise ValueError(
            "mlb_sim: USE_STUFF_PRIOR and USE_CHED_PRIOR are both on. They "
            "read the same pitch characteristics and move the same prior — "
            "pick one.")
    if boards is None:
        # An explicit `seasons` is an INSTRUCTION and is honoured whole; the
        # flag only decides what the default is. Tested on `seasons` itself
        # rather than on the resolved list — testing the resolved list
        # truncates a caller's explicit request too, which is the opposite of
        # what the comment above it claimed.
        asked = bool(seasons)
        seasons = (list(seasons) if seasons
                   else list(RateIngest.rate_seasons(side))
                   or available_seasons(side, save_dir))
        if seasons and not asked and not RateIngest.use_season_blend(side):
            seasons = [max(seasons)]
        # `boards` is not gated here at all: the as-of path builds it and has
        # already applied the gate.
        boards = {s: load_board(side, s, save_dir) for s in seasons}
    boards = {s: b for s, b in boards.items() if b}
    if not boards:
        raise FileNotFoundError(
            f"mlb_sim: no cached fg_{side}_*.json boards in "
            f"{RateIngest._shared(save_dir)}")

    newest = max(boards)
    older = [s for s in boards if s < newest]
    # A PARTIAL newest board is not the season's environment — it is the
    # environment of the weeks played so far, and in April that is 15% short
    # on home runs while the weather term charges the same cold again.
    league = RateIngest.projected_league_baseline(
        boards[newest], side, boards[max(older)] if older else None)
    newest_rows = {pid: row for row in boards[newest]
                   if (pid := _row_id(row)) is not None}
    # Each season's OWN league, so an older line can be re-expressed in the
    # newest season's run environment before it is blended in. See
    # `rebase_to_season` — skipping this put every multi-season pitcher ~0.8%
    # too good and cost the model ~0.11 runs a game.
    season_league = {s: league_baseline(rows, side) for s, rows in boards.items()}

    # One club's playing time on each season's board, so a player's role can
    # be expressed as a SHARE. See `board_pa_per_club`.
    per_club = {s: board_pa_per_club(rows, side) for s, rows in boards.items()}

    # Per-hitter contact PROFILE, centred on the population it is applied to.
    # See `contact_profiles` and `contact_prior`.
    bm_rel: Dict[int, Tuple[List[float], int]] = {}

    # Per-PITCHER stuff delta, from pitch characteristics only. Unlike the
    # contact work this is disjoint from the outcomes being shrunk, so the
    # pitcher's own data does not enter twice — see `fit_stuff_model`.
    st_delta: Dict[int, List[float]] = {}
    st_stab: Tuple[float, ...] = ()
    if side == "pit" and USE_STUFF_PRIOR:
        st_delta = stuff_deltas(
            Stuff.stuff_source_board(newest, boards[newest], as_of, save_dir),
            stuff_model_for(newest, save_dir))
        if st_delta:
            st_stab = stuff_stabilize(stabilize_for(side))

    ched_tab: Dict[int, dict] = {}
    if side == "pit" and USE_CHED_PRIOR:
        ched_tab = load_ched(newest, save_dir)
        if not ched_tab:
            # A silent empty here would run the baseline under CHED's name,
            # which is the failure `_ml_adjuster` records for hierarchy arms.
            print(f"mlb_sim: USE_CHED_PRIOR is on but ched_{newest}.json is "
                  f"missing or empty — no CHED applied. Run "
                  f"`ched_train.export({newest})`.")

    # Triple-A lines and their fitted translation. The rule is DATE-AWARE, not
    # season-aware — count what was played before the game being priced. The
    # season rule was wrong in BOTH directions at once: it threw away a callup's
    # record (the case the feature exists for) while a demoted veteran's line
    # postdated the replayed game and leaked the outcome backwards.
    #
    # **The prior season counts too, and measurably so** — current-season only
    # covers 18% of thin arms at an April cutoff with a MEDIAN of 0 batters
    # faced; adding prior seasons takes that to 62%, median 25. sim_state.md A.9c.
    milb_tabs: Dict[int, dict] = {}
    milb_sw: Dict[int, float] = {}
    milb_fac: List[float] = []
    _lvf: Dict[str, List[float]] = {}
    milb_cred: List[float] = []
    if USE_MILB_PRIOR:
        _tr = load_milb_translation(save_dir)
        milb_fac = list((_tr.get("factor") or {}).get(side) or [])
        _ck = ("credit_applied" if MILB_CREDIT_SPEC == "applied" else "credit")
        milb_cred = list((_tr.get(_ck) or {}).get(side) or [])
        # Per-level factors when the artifact carries them; a translation file
        # fitted before the ladder existed falls back to Triple-A only, which
        # is the OLD behaviour rather than a wrong one.
        _lvf = ((_tr.get("factor_by_level") or {}).get(side)
                or {"AAA": milb_fac})
        if milb_fac and milb_cred and any(milb_cred):
            for _s in sorted(boards):
                if _s == newest and as_of:
                    # A replay. The newest season MUST come from the snapshot
                    # cut at the same date as the board beside it — and when
                    # no snapshot was collected the season is DROPPED, never
                    # served season-final. Falling back is the leak.
                    _t = (load_milb_asof(newest, as_of,
                                         save_dir).get(side) or {})
                else:
                    _t = (load_milb(_s, save_dir).get(side) or {})
                if _t:
                    milb_tabs[_s] = _t
            milb_sw = RateIngest.season_weights(sorted(milb_tabs), half_life)

    # WITHIN-SEASON recency, on the newest board only — the older seasons are
    # already decayed by `SEASON_HALF_LIFE` and no as-of boards exist for them.
    # Windows come from differencing the cached as-of boards; a player with no
    # window sequence keeps his season totals, which is the pre-recency
    # behaviour rather than a hole.
    windows: Dict[int, List[tuple]] = {}
    if USE_RECENCY and recency_half_life(side) > 0:
        windows = board_windows(side, newest, boards[newest], as_of, save_dir)

    per_player: Dict[int, Dict[int, Tuple[List[float], float]]] = {}
    # The RAW plate appearances, kept separate from the recency-weighted ones.
    # **How much evidence we have and what role he fills are different
    # questions** — the shrinkage weight wants the effective count, the
    # playing-time prior wants the real one, and summing the two together is
    # the recorded trap that made a part-timer index as a regular.
    raw_pa: Dict[int, Dict[int, float]] = {}
    names: Dict[int, str] = {}
    hands: Dict[int, str] = {}
    for season, rows in boards.items():
        for row in rows:
            pid = _row_id(row)
            if pid is None:
                continue
            counts, pa = outcome_counts(row, side)
            if pa <= 0:
                continue
            raw_pa.setdefault(pid, {})[season] = pa
            seq = windows.get(pid) if season == newest else None
            if seq:
                counts, pa = recency_counts(seq, recency_half_life(side))
                if pa <= 0:
                    counts, pa = outcome_counts(row, side)
            # Including the NEWEST season, because `league` is the PROJECTED
            # full-season environment, not that board's own. Leaving it
            # un-rebased would price a busy April hitter and a 20-PA one two
            # different ways. On a complete board this is an identity.
            counts = rebase_to_season(counts, season_league[season], league)
            # **Strip the player's OWN park before the shrink.** The
            # stabilisers estimate TALENT, but the line handed to them carries
            # the park he played in — roughly half his PAs at his club's field,
            # on RAW counts. Gated with `park_run_tilt` on one flag, because a
            # park-neutral input only makes sense with the matching change.
            if USE_PARK_DECONTAM:
                counts = ParkFactors.decontaminate_counts(
                    counts, pa, pid, side, season, save_dir=save_dir)
            per_player.setdefault(pid, {})[season] = (counts, pa)
            names.setdefault(pid, row.get("PlayerName") or str(pid))
            # Handedness, from the newest board row that carries it. Needed by
            # `platoon_rates`; STARTERS had no `throws` at all before this.
            h = row.get("Throws") if side == "pit" else row.get("Bats")
            if h:
                hands[pid] = str(h)

    bm_lg: List[float] = []
    # **The gate lives HERE and nowhere else, and that is the fix.** It used to
    # be applied only by `build_rates_asof`, so the flag reached the AS-OF path
    # and the LIVE path never saw it — with it on, 430/1,608 hitters moved as-of
    # and 0/1,650 live. `bmielke_season` is now a DATA argument: it says WHICH
    # season's profiles to read, not whether to read any.
    if side == "bat" and USE_CONTACT_PRIOR:
        bm_season = newest if bmielke_season is None else int(bmielke_season)
        bm_rel, bm_lg = Contact.contact_profiles(list(per_player), bm_season,
                                         bmielke_asof_date, save_dir)

    # BMIELKE — a hitter's SWINGS as the level of his contact prior, gated to
    # the thin-sample regime where the metric is validated to beat his own
    # xwOBAcon (§17e). Same DATA arguments as the contact map: `bmielke_season`
    # names which season's swings to read, `build_rates` owns the on/off — the
    # gate lives HERE and nowhere else, which is the fix `USE_CONTACT_PRIOR`
    # needed on 2026-08-25.
    bm_prof: Dict[int, Tuple[List[float], int, float]] = {}
    bm_shape_lg: List[float] = []
    bm_dir: Optional[List[float]] = None
    if side == "bat" and USE_BMIELKE_PRIOR:
        bm_season = newest if bmielke_season is None else int(bmielke_season)
        bm_prof, bm_shape_lg = Bm.bmielke_profiles(
            list(per_player), bm_season, bmielke_asof_date, save_dir)
        bm_dir = contact_quality_direction(bm_season, save_dir)

    # Blend ONCE. The Triple-A centring below needs every player's MLB sample
    # before the per-player loop can start, and blending twice is the same
    # work done twice on the hot path of the backtest.
    blended: Dict[int, Tuple[List[float], float]] = {
        pid: RateIngest.blend_seasons(by_season, half_life, newest)
        for pid, by_season in per_player.items()}

    # --- the Triple-A evidence, and the population it is centred on --------
    # Gathered before the loop because `milb_center` is a property of the
    # POPULATION that passes the gate, not of any one player. See `milb_prior`:
    # the line is evidence about where a player sits among his peers, and
    # subtracting the peer mean is what keeps it from moving the league level.
    milb_ev: Dict[int, Tuple[List[float], float]] = {}
    milb_center: List[float] = []
    if milb_tabs:
        for pid in per_player:
            if MILB_MLB_PA_GATE > 0 and blended[pid][1] >= MILB_MLB_PA_GATE:
                continue
            ac = [0.0] * N_OUTCOMES
            an = 0.0
            for _s, _tab in milb_tabs.items():
                _c, _n = MiLB._milb_level_evidence(_tab.get(str(pid)), _lvf, milb_fac)
                if _c is None or _n <= 0:
                    continue
                _w = milb_sw.get(_s, 0.0)
                for i in range(N_OUTCOMES):
                    ac[i] += _w * _c[i]
                an += _w * _n
            if an > 0:
                # already MLB-equivalent — `_milb_level_evidence` translates
                # each level before pooling, because the levels are on
                # different scales until it does.
                milb_ev[pid] = ([ac[i] / an for i in range(N_OUTCOMES)], an)
        if milb_ev:
            # **Weighted by the same `w` the deviation is multiplied by, and
            # that is not a detail.** What must vanish is sum(w_p * (tr_p - c)),
            # not sum(tr_p - c): `w` correlates with the line, so heavy-sample
            # players set the level. Unweighted, the centre moved pitchers
            # -1.05% and hitters +1.29% — both WORSE than no prior at all.
            _sw = [0.0] * N_OUTCOMES
            _sx = [0.0] * N_OUTCOMES
            _st = stabilize_for(side)
            for _tr, _an in milb_ev.values():
                for i in range(N_OUTCOMES):
                    _eff = milb_cred[i] * _an
                    _w = _eff / (_eff + _st[i]) if _eff > 0 else 0.0
                    _sw[i] += _w
                    _sx[i] += _w * _tr[i]
            milb_center = [(_sx[i] / _sw[i]) if _sw[i] > 0 else 0.0
                          for i in range(N_OUTCOMES)]

    def _share(pid, by_season) -> float:
        # Recency-weighted MEAN share, not a sum: three seasons of 200 PA is a
        # part-timer with plenty of evidence, not a regular.
        sw = RateIngest.season_weights(list(by_season) + ([newest] if newest not in
                                               by_season else []), half_life)
        wsum = sum(sw[s] for s in by_season) or 1.0
        # RAW playing time here, never the recency-weighted count: the curve
        # encodes what ROLE this much work implies, and a recency weight would
        # index every regular as a part-timer.
        return sum(sw[s] * (raw_pa[pid].get(s, 0.0) / per_club[s]
                            if per_club.get(s) else 0.0)
                   for s in by_season) / wsum

    # The prior's centring is solved HERE, over the same blended counts the
    # loop below shrinks against — see `solve_bat_prior_tilt`. Solving it from
    # the board instead was worth -0.79 runs a game in April.
    #
    # **Solved per call, not frozen as a constant**, which is what lets one
    # mechanism absorb a gap that runs -0.41 runs/team-game in April and -0.02
    # in August without anyone fitting a seasonal term.
    centre_tilt = 0.0
    if side in _prior_sides() and _prior_centred(side):
        _curve = prior_curve(side, newest, save_dir, boards[newest])
        if _curve:
            # **Solved over the WHOLE BOARD, which is the population the tilt is
            # applied to.** Narrowing this to the arms that actually pitch is
            # better-reasoned and scores WORSE — see `PIT_PRIOR_CENTRE_POP`,
            # head-to-head t -4.40 (2026) and t -1.93 (2025).
            #
            # The HITTER side keeps the whole board for the same reason plus
            # one more: 4e's centring was solved and scored that way, and
            # `batprior`'s recorded results belong to that population.
            _pop_ids = (engine_pitcher_ids(boards[newest])
                        if side == "pit" and PIT_PRIOR_CENTRE_POP == "engine"
                        else None)
            centre_tilt = RateIngest.solve_bat_prior_tilt(
                [(_share(pid, bs), blended[pid][0])
                 for pid, bs in per_player.items()
                 if _pop_ids is None or pid in _pop_ids],
                _curve, league, stabilize_for(side))

    out: Dict[int, dict] = {}
    for pid, by_season in per_player.items():
        counts, pa = blended[pid]
        # The shrinkage TARGET depends on playing time, because playing time in
        # MLB is selected on performance — see `playing_time_prior`. Shrinking
        # a 40-batter reliever toward the league mean called him a 0.331 arm
        # when arms with that little work threw 0.368.
        # The prior must be built from the SAME board the rates are — passing
        # `boards[newest]` is what keeps an as-of run from regressing toward a
        # full-season population.
        share = _share(pid, by_season)
        prior = playing_time_prior(share, side, league, newest, save_dir,
                                   boards[newest], centre_tilt)
        # A hitter's CONTACT prior, when a contact model can see him. Hitters
        # otherwise regress to league on 2B/HR, and that prior carries 80% of
        # the weight even for a 600-PA regular.
        #
        # **What he did at TRIPLE-A DISPLACES the playing-time prior and does
        # not stack on it** — both encode "below league" and for a callup they
        # encode it for the SAME REASON, so stacking marks him down twice
        # (0.130-0.146 runs a game on the hitter side alone). Applied ahead of
        # the contact and stuff priors, which are independent evidence that
        # should refine whatever prior survives. sim_state.md A.9c.
        got_milb = milb_ev.get(pid)
        if got_milb is not None and milb_center:
            prior = milb_prior(prior, got_milb[0], got_milb[1], milb_cred,
                              stabilize_for(side), center=milb_center)
        # The prior BEFORE either contact term, kept so the hitter's contact
        # FREQUENCY can be restored after shrinkage — see `hold_bip_rate`.
        # **Captured ahead of BOTH**, because §3d.7's map redistributes the
        # in-play block exactly as §17e's level does and leaks the same way;
        # capturing between them would correct only the second.
        prior_no_contact = list(prior)
        got = bm_rel.get(pid)
        if got is not None:
            prior = Contact.contact_prior(prior, got[0], got[1], bm_lg)
        # **Everything below refines the prior and PAYS for it in `stab`.** The
        # rule is one line and it applies to all three: a prior that explains
        # part of a player's talent leaves less for his own line to resolve, so
        # the observed rates are trusted LESS, not more.
        stab = stabilize_for(side)
        # BMIELKE, for a hitter thin enough that his SWINGS beat his own
        # batted-ball results. Refines whatever prior survived above — league,
        # or the Triple-A line for a callup — rather than displacing it: the
        # metric is evidence about his CONTACT, and `milb_prior` is evidence
        # about his LEVEL, so they are independent and compose. Above
        # `BMIELKE_MAX_BBE` this returns nothing and the hitter is untouched.
        bmp = bm_prof.get(pid)
        if bmp is not None and bm_shape_lg:
            prior = bmielke_prior(prior, bmp[0], bmp[1], bm_shape_lg, bmp[2],
                                  bm_dir)
        _contact_moved = (got is not None
                          or (bmp is not None and bool(bm_shape_lg)))
        # An arm whose PITCHES say something his results have not had time to.
        delta = st_delta.get(pid)
        if delta is not None:
            prior = stuff_prior(prior, delta)
            stab = st_stab or stab
        # CHED occupies the same seam and is gated on the same argument: the
        # prior now knows something his own line has not had time to say, so
        # the observed rates are trusted LESS, not more.
        ch_rec = ched_tab.get(pid)
        if ch_rec is not None:
            # **No gate. The weight IS the gate.** `rel` is n_eff/(n_eff+80)
            # with Triple-A pitches counted at their measured worth, so a thin
            # arm is trusted a little and an unsampled one approaches zero
            # smoothly — no cliff at 80, the same objection this file makes to a
            # fatigue step at batter 19.
            rel = float(ch_rec.get("rel", 1.0))
            if rel > 0.0:
                prior = stuff_prior(
                    prior, ched_delta(prior, ch_rec["rv_delta"] * rel))
                stab = stuff_stabilize(stabilize_for(side))
        rec = {
            "name": names[pid],
            # Stabilisation is PER SIDE — a pitcher's own home-run and contact
            # rates are far noisier than a hitter's and must be regressed far
            # harder. See `STABILIZE_PA_PIT`.
            "rates": (hold_bip_rate(
                          shrink_rates(counts, prior, stab),
                          shrink_rates(counts, prior_no_contact, stab))
                      if _contact_moved
                      else shrink_rates(counts, prior, stab)),
            "pa": pa,
            "hand": hands.get(pid, ""),
        }
        # The running game is read off the MOST RECENT season only. Legs go
        # first and clubs change their instruction — blending three years of
        # steal attempts describes a player who no longer exists.
        if side == "bat":
            latest = newest_rows.get(pid)
            if latest is not None:
                rec["run"] = Boards.runner_profile(latest)
        out[pid] = rec
    return out, league


def team_roster(side: str, season: int, save_dir: Path = SAVE_DIR
                ) -> Dict[str, List[dict]]:
    """Board rows grouped by club, most-used first.

    Rows whose team reads "2 Tms" / "3 Tms" are a player's COMBINED line
    across a mid-season trade, not a club's roster, and including them would
    put the same player on no real team while inflating nobody's lineup.
    They are dropped here and picked up by id from the league-wide table.
    """
    rows = load_board(side, season, save_dir) or []
    key = "PA" if side == "bat" else "TBF"
    out: Dict[str, List[dict]] = {}
    for row in rows:
        abbr = row.get("TeamNameAbb")
        if not abbr or "Tms" in str(abbr):
            continue
        out.setdefault(abbr, []).append(row)
    for abbr in out:
        out[abbr].sort(key=lambda r: -_num(r, key))
    return out


# How many relievers a club carries into the simulation.
#
# **MEASURED, and 8 was badly wrong.** A real club uses 24.2 distinct relievers
# across a season while the sim carried 8, so those 8 absorbed ALL the bullpen
# work — over-using every modelled arm by ~20 points of appearance rate. It is
# not only usage fidelity: those other arms are WORSE, and a real club's late
# innings are regularly covered by them, which is the likeliest reason the sim's
# eighth inning scored 0.468 against a real 0.521. Depth alone does not fix
# usage — an arm that pitched yesterday must also be less likely to pitch today.
# sim_state.md A.9.
PEN_DEPTH = 14   # raising it changes nothing: the board yields ~14 per club


def _is_relief_role(row: dict) -> bool:
    """`build_side`'s own pen test, lifted so there is ONE copy of it."""
    return _num(row, "GS") / max(_num(row, "G"), 1.0) < 0.5


def engine_pitcher_ids(rows: Sequence[dict],
                       pen_depth: Optional[int] = None) -> set:
    """The arms the engine can actually put on a mound, as mlbam ids.

    `build_side`'s selection generalised one step: every ROTATION arm, because
    any of them can be tonight's probable, plus each club's top `pen_depth`
    relievers by batters faced. Everything below that cut sits on the board and
    never pitches.

    Built to solve the prior's CENTRING over it, on the argument that zero ON
    AVERAGE is not zero WITHIN — post-tilt the residual runs +0.060 on
    sub-1%-share arms against -0.007 on the bulk, and that thin end is ~13% of
    board PA that `PEN_DEPTH` cuts before a game is simulated.

    **That argument is sound and the change SCORED WORSE.** Reachable only via
    `PIT_PRIOR_CENTRE_POP = "engine"`, which records why. Kept because it is a
    measured negative worth being able to reproduce, and because `build_side`
    shares `_is_relief_role` with it.
    """
    pen_depth = PEN_DEPTH if pen_depth is None else int(pen_depth)
    by_club: Dict[str, List[dict]] = {}
    for row in rows:
        abbr = row.get("TeamNameAbb")
        # "2 Tms" is a COMBINED line across a trade, not a roster — same
        # exclusion `team_roster` makes, and for the same reason. His per-club
        # rows are on the board separately and are what get counted.
        if not abbr or "Tms" in str(abbr):
            continue
        by_club.setdefault(abbr, []).append(row)
    keep: set = set()
    for club_rows in by_club.values():
        used = 0
        for row in sorted(club_rows, key=lambda r: -_num(r, "TBF")):
            pid = _row_id(row)
            if pid is None:
                continue
            if _is_relief_role(row):
                if used >= pen_depth:
                    continue
                used += 1
            keep.add(pid)
    return keep


def build_side(abbr: str, bat_table: Dict[int, dict],
               pit_table: Dict[int, dict], season: Optional[int] = None,
               hazard: Optional[List[float]] = None,
               save_dir: Path = SAVE_DIR):
    """A ready-to-simulate TeamSide for one club, straight off the boards.

    Lineup is the nine most-used bats; the starter is the club's highest-GS
    arm and the pen is the rest by innings. This is the OFFLINE stand-in for
    a posted lineup and a named probable — good enough to validate the engine,
    and the seam where the live `EffortMLB` lineup/probable path plugs in.
    """
    season = CURRENT_SEASON if season is None else int(season)
    bats = team_roster("bat", season, save_dir).get(abbr, [])
    pits = team_roster("pit", season, save_dir).get(abbr, [])
    if len(bats) < 9 or not pits:
        known = sorted(team_roster("bat", season, save_dir))
        raise ValueError(
            f"mlb_sim: no {abbr!r} on the {season} board. The board spells "
            f"seven clubs differently from StatsAPI (TB->TBR, SD->SDP, "
            f"SF->SFG, KC->KCR, AZ->ARI, CWS->CHW, WSH->WSN); "
            f"`normalize_club()` maps them. Known: {', '.join(known)}")

    lineup = []
    for row in bats[:9]:
        pid = _row_id(row)
        # Handedness and advancement come out of `make_batter` now — they used
        # to be patched on here, which meant the posted-lineup path silently
        # went without them.
        b = make_batter(pid, bat_table, season, save_dir) if pid else None
        lineup.append(b or Batter(row.get("PlayerName", "?"),
                                  list(bat_table and next(iter(bat_table.values()))["rates"])))

    starters = sorted(pits, key=lambda r: -_num(r, "GS"))
    sp_row = starters[0]
    sp = make_pitcher(_row_id(sp_row), pit_table, is_starter=True,
                      hazard=hazard or [])
    if sp is None:
        # **A starter with no rate row gets a REPLACEMENT-LEVEL line, not a
        # None.** Dropping an entity is not neutral (5.5a, 5.6a); the starter
        # was the one case never given that treatment, and it returned a
        # TeamSide whose `.starter` was None, which `_game_side` raises on.
        #
        # It has never fired on the shipped model, and that is the interesting
        # part: measured over 6 cutoffs x 30 clubs, 0 with the season blend on
        # and 2 with it off. **The blend is load-bearing for COVERAGE**, not
        # only for accuracy.
        sp = Pitcher(name=str(sp_row.get("PlayerName") or "replacement-SP"),
                     rates=replacement_pitcher_rates(),
                     player_id=_row_id(sp_row), is_starter=True,
                     hazard=hazard or [])

    pen_rows = [r for r in pits if _is_relief_role(r)][:PEN_DEPTH]
    team_g = _team_games(season, save_dir).get(abbr, 122.0) or 122.0
    pen = []
    for r in pen_rows:
        arm = make_pitcher(_row_id(r), pit_table)
        if arm is None:
            # He is on this club's board, so the club carries him; we simply
            # have no rate row. **Dropping him is not neutral** — it shortens
            # the pen and hands his innings to better arms. It bites hardest
            # AS-OF, where a reliever who has not pitched yet is absent by
            # construction: the April pen came out 11.4 arms against 13.6,
            # positively selected, because a manager uses his best first.
            pid = _row_id(r)
            arm = Pitcher(name=r.get("PlayerName") or str(pid),
                          rates=replacement_pitcher_rates(), player_id=pid)
        g = _num(r, "G")
        tr = RelieverTraits.load_reliever_traits(season).get(arm.player_id or -1) or {}
        # **Do NOT default a missing traits row to league-average usage.** It
        # made an arm we know NOTHING about a workhorse ready every day —
        # exactly backwards, and only visible once PEN_DEPTH went to 14: the
        # five Oakland arms with no traits row ran 27-34% simulated against a
        # real 2-7%. Absence of a row means a fringe arm, so fall back to his own
        # appearance count.
        arm.app_rate = float(tr.get("app_rate") if tr.get("app_rate") is not None
                             else min(0.35, g / max(team_g, 1.0)))
        arm.bf_per_outing = float(tr.get("bf_per_outing", 4.0))
        arm.avg_inning = tr.get("itp_avg_inning")
        arm.avg_run_diff = tr.get("itp_avg_run_diff")
        arm.back_to_back = tr.get("itp_back_to_back")
        arm.save_share = (float(tr.get("sv", 0.0)) / g) if g else 0.0
        arm.throws = str(r.get("Throws") or "")
        raw_li = _num(r, "gmLI", 1.0) or 1.0
        # Shrink gmLI toward the league by APPEARANCES rather than gating on a
        # chosen minimum: a one-game callup can post a 2.39 gmLI off a single
        # high-leverage cameo, and that is one appearance, not a closer. Same
        # empirical-Bayes treatment every other rate in this module gets.
        w = g / (g + Boards.gmli_stabilizer(season, save_dir))
        arm.gm_li = w * raw_li + (1.0 - w) * 1.0
        # A long man is inferred from innings per appearance, not labelled.
        arm.multi_inning = float(tr.get("ip_per_outing", 1.0)) >= 1.25
        pen.append(arm)
    # Order by the leverage a manager actually uses him in.
    pen.sort(key=lambda a: -a.gm_li)
    # Savant's OAA and framing leaderboards IGNORE date parameters (§3c), so an
    # as-of run gets the FULL season — future information for an April game.
    # `TEAM_CONTEXT_LAG = 1` takes the prior season's instead: stale, but it
    # predates every game priced. Worth all of the model's apparent advantage
    # over the closing line (§3d.1).
    ctx = season - TEAM_CONTEXT_LAG
    d = load_team_defense(ctx).get(abbr) or {}
    # Framing is a season TOTAL, so it must be divided by the games of ITS OWN
    # season. Dividing last year's 162-game total by this year's 122 played so
    # far would inflate every club by a third.
    games = _team_games(ctx, save_dir).get(abbr, 122.0) or 122.0
    fr = team_framing_per_game(ctx, abbr, games, save_dir)
    return TeamSide(lineup=lineup, starter=sp, bullpen=pen,
                    oaa=float(d.get("oaa") or 0.0), of_arm=d.get("of_arm"),
                    framing=fr)


# --- the running game, per player -----------------------------------------
# Steal OPPORTUNITY is PAs with the runner on first and second base open.
# **CALIBRATED, not derived, and the two disagree**: counting directly gives
# 1.027 per arrival at first, which produces 1.07 attempts a game against a real
# 0.92, while 1.65 reproduces the real rate. The gap is opportunity
# CONCENTRATION in a nine-man lineup with no bench. Re-solve against league
# SB+CS whenever lineup construction changes — it is the only place that
# absorbs it.
OPP_PER_TIME_ON_FIRST = 1.65

_GMLI_STABILIZER: Optional[float] = None


LG_STEAL_ATTEMPT = 0.096
LG_STEAL_SUCCESS = 0.78
STABILIZE_STEAL_ATTEMPT = 60.0   # opportunities
STABILIZE_STEAL_SUCCESS = 20.0   # attempts
LG_SPD = 4.5                     # league mean Bill James speed score
SPD_TO_ODDS = 0.188              # Spd 7.0 -> ~1.6x odds of taking a base


_BAT_ROWS: Dict[tuple, Dict[int, dict]] = {}


def make_batter(pid: int, table: Dict[int, dict], season: Optional[int] = None,
                save_dir: Path = SAVE_DIR) -> Optional[Batter]:
    season = CURRENT_SEASON if season is None else int(season)
    rec = table.get(pid)
    if rec is None:
        return None
    run = rec.get("run") or {}
    # Advancement is looked up HERE for the same reason handedness is: it was
    # attached in `build_side` only, so every hitter arriving through a POSTED
    # LINEUP ran on the league constants while the board-built lineup ran on
    # his own rates. Same silent-fallback shape as the `bats` bug in 5b.1.
    brow = Boards._bat_row(pid, season, save_dir)
    adv = (RunnerAdvance.runner_advance_rates(pid, _num(brow, "XBR"), _num(brow, "Spd"))
           if brow is not None else RunnerAdvance.runner_advance_rates(pid))
    # `bats` must be set HERE, not only in `build_side`. The live path
    # (`build_side_live`, posted lineups) builds hitters through this function
    # and never touched handedness, so the platoon term would have silently
    # no-opped on exactly the lineups that matter most.
    return Batter(name=rec["name"], rates=rec["rates"], player_id=pid,
                  bats=rec.get("hand", ""), adv=adv,
                  steal_attempt=run.get("steal_attempt", LG_STEAL_ATTEMPT),
                  steal_success=run.get("steal_success", LG_STEAL_SUCCESS),
                  speed=run.get("speed", 1.0))


_PITCH_HAZ: List[List[float]] = []


_MILB_NAMES: Dict[int, str] = {}


def make_pitcher(pid: int, table: Dict[int, dict], is_starter: bool = False,
                 hazard: Optional[List[float]] = None) -> Optional[Pitcher]:
    rec = table.get(pid)
    if rec is None:
        return None
    return Pitcher(name=rec["name"], rates=rec["rates"], player_id=pid,
                   is_starter=is_starter, hazard=list(hazard or []),
                   pitch_hazard=(Boards.starter_pitch_hazard() if is_starter
                                 and USE_PITCH_HOOK else []),
                   throws=rec.get("hand", ""))


# ---------------------------------------------------------------------------
# `python mlb_sim.py rates` — ingest report
# ---------------------------------------------------------------------------




# ===========================================================================
# 9c. MINOR LEAGUE LINES — the evidence a callup's MLB row does not have
# ===========================================================================
# **The game that surfaced it**: LAA @ HOU priced at 10.84 and WSN @ TEX at
# 10.37 against actual totals of 4 and 5, both with a starter of 19 and 76
# major-league batters faced whom the playing-time prior regressed to .394 and
# .373 against a league .316.
#
# **The prior is not malfunctioning; it is answering a different question.** Low
# volume usually means low quality, but for a rookie's second start it means
# NEWLY ARRIVED, and playing time alone cannot separate them — Klassen had 395
# batters faced at Triple-A. **It costs nothing to have**: StatsAPI serves every
# level, keyless, on the SAME MLBAM id. **This module collects; it does not
# translate.** sim_state.md A.9c.

MILB_LEVELS: Dict[int, str] = {11: "AAA", 12: "AA", 13: "A+", 14: "A",
                               16: "ROK"}
MILB_CACHE_FMT = "milb_{season}.json"


# The StatsAPI keys that carry the counts `outcome_counts` needs. Stored under
# the SOURCE's names, not remapped on the way in: a cache that has already been
# interpreted cannot be re-interpreted when the interpretation turns out to be
# wrong, and this one is going to change once level factors are measured.
_MILB_PIT_KEYS = ("battersFaced", "inningsPitched", "strikeOuts",
                  "baseOnBalls", "intentionalWalks", "hitByPitch", "hits",
                  "doubles", "triples", "homeRuns", "groundOuts", "airOuts",
                  "sacFlies", "sacBunts", "earnedRuns", "runs",
                  "gamesPitched", "gamesStarted", "numberOfPitches")
_MILB_BAT_KEYS = ("plateAppearances", "atBats", "strikeOuts", "baseOnBalls",
                  "intentionalWalks", "hitByPitch", "hits", "doubles",
                  "triples", "homeRuns", "groundOuts", "airOuts", "sacFlies",
                  "sacBunts", "stolenBases", "caughtStealing", "runs", "rbi",
                  "gamesPlayed")


_MILB: Dict[int, dict] = {}


def load_milb(season: int, save_dir: Path = SAVE_DIR) -> dict:
    """The cached minor league season, or {} when it has not been collected."""
    if season in _MILB:
        return _MILB[season]
    try:
        with open(MiLB.milb_cache_path(season, save_dir)) as fh:
            _MILB[season] = json.load(fh)
    except (OSError, ValueError):
        _MILB[season] = {}
    return _MILB[season]


# --- Triple-A PARK FACTORS, per outcome (5.11.1) --------------------------
# **Triple-A parks are far more extreme than major league ones and nothing in
# the translation corrected for it** — HR runs 0.547 to 1.573, and the club run
# factor persists year over year at +0.49 to +0.75 against a major league
# season's +0.27 to +0.34. Large AND persistent is what makes a factor real; the
# altitude of the Pacific Coast League is why. **Hitter home runs carry the
# LARGEST credit in the translation**, so the outcome the model trusts most from
# Triple-A is the one the park distorts most. sim_state.md A.9c.

MILB_PARK_FMT = "milb_park_{season}.json"
# One season of park factor is mostly noise and averaging is an arithmetic
# improvement rather than a fitted one — the same argument as §8's three-season
# major league window, and the reason `PARK_RUN_WINDOW` ships at 3.
MILB_PARK_WINDOW = 3


# --- MINOR-LEAGUE STATCAST -------------------------------------------------
# **Hawk-Eye is in Triple-A and the Florida State League, and NOT in Double-A** —
# probed, not assumed — so this refines a Triple-A callup and says nothing about
# a Double-A one. **`bat_speed` and `swing_length` are 0% populated at every
# level**, so BMIELKE cannot be extended down: a hard stop, not a scraping
# problem. Four probe traps (`minors=true` is the switch, a 25,000-row cap that
# is not reported, no level column, a UTF-8 BOM on the first CSV column) are
# written up in sim_state.md A.9c. What IS carried is aggregated into the SAME
# column names the FanGraphs board uses, so `_arsenal_block` needs no branch.
MILB_STATCAST_LEVELS: Tuple[str, ...] = ("AAA",)
MILB_STATCAST_CHUNK_DAYS = 3
# Bumped when the arsenal aggregate GAINS a column. v1 carried shape only
# (usage/spin/velo/X/Z per type); v2 adds the DELIVERY — arm angle, extension,
# release position and its spread — without which the CHED slot regression has
# no right-hand side.
MILB_ARSENAL_VERSION = "v2"
_MILB_THROWS: Dict[tuple, Optional[str]] = {}
# Chunk fetches run in parallel. The major-league scrape did 2,516
# pitcher-seasons at 5 workers with 0 failures; the windows here are
# independent and every one that lands is cached, so a rate-limited window
# costs exactly itself and is picked up by the next run. That is what makes a
# high worker count cheap to try rather than a gamble.
MILB_STATCAST_WORKERS = 22
# What the raw minor-league pitch cache keeps. Mirrors `scrape_pitches.KEEP`
# so a Triple-A pitch and a major-league one are the same record.
MILB_RAW_KEEP = (
    "game_pk", "game_date", "pitcher", "batter", "pitch_type",
    "p_throws", "stand",
    "release_speed", "release_spin_rate", "spin_axis",
    "release_pos_x", "release_pos_y", "release_pos_z",
    "release_extension", "arm_angle",
    "vx0", "vy0", "vz0", "ax", "ay", "az", "pfx_x", "pfx_z",
    "plate_x", "plate_z", "sz_top", "sz_bot",
    "description", "events", "balls", "strikes",
)
_MILB_SPORT_LEVEL = {11: "AAA", 12: "AA", 13: "A+", 14: "A", 16: "ROK",
                     17: "ROK"}
_MILB_GAME_LEVEL: Dict[int, str] = {}


# Savant's pitch codes are the MODERN Statcast set; the board's `pfx` columns
# are PITCHf/x vocabulary, and they disagree on the most common pitch in
# baseball — four-seam is `FF` against the board's `FA`, so emitting `FF` drops
# it out of the fastball family entirely. Break is in FEET here and INCHES
# there, but x12 alone lands 1.7x too big: different BREAK CONVENTIONS, not
# different units. FITTED on 47,184 pitches, both axes agreeing to within 1%.
# **This is why an earlier pass "found" Triple-A arms with twice the movement** —
# the comparison measured the transform. sim_state.md A.9c.
MILB_PFX_BREAK_TO_BOARD = 0.593

_SAVANT_TO_PFX: Dict[str, str] = {
    "FF": "FA",        # four-seam: the whole point of this table
    "SV": "CV",        # slurve -> the board's curve variant
    "CS": "CV",        # slow curve
    "FT": "FT", "SI": "SI", "FC": "FC", "FA": "FA",
    "SL": "SL", "ST": "ST", "CU": "CU", "KC": "KC", "SC": "SC",
    "CH": "CH", "FS": "FS", "FO": "FO", "EP": "EP", "KN": "KN",
}
# Not pitches: a pitchout is a tactic and an intentional ball is not thrown to
# be hit, so neither belongs in an arsenal average.
_SAVANT_DROP = {"PO", "IN", "AB", "UN", ""}


_MILB_PARK: Dict[int, dict] = {}


# --- AS-OF minor league snapshots — the date-aware rule (5.11.1) ----------
# The two populations a season rule lumps together are chronologically OPPOSITE:
# a CALLUP's Triple-A innings all PRECEDE his debut and are legal evidence the
# season rule throws away; a DEMOTED veteran's POSTDATE the replayed game and
# leak twice, because the LINE'S MERE EXISTENCE encodes the outcome — he was
# sent down for pitching badly.
#
# `stats=byDateRange` serves exactly that, and **was verified against the season
# call before anything was built on it** (same 1,264 players, same batters
# faced, 0 mismatches). AAA only, on the boards' own cutoff grid. A.9c.

MILB_ASOF_SPORT = 11                                  # AAA — see 5.11's table
MILB_ASOF_FMT = "milb_{season}_{as_of}.json"


_MILB_ASOF: Dict[tuple, dict] = {}


def load_milb_asof(season: int, as_of: str,
                   save_dir: Path = SAVE_DIR) -> dict:
    """The Triple-A snapshot at a cutoff, or {} when it was never collected.

    **{} means the prior is off for that run, not that it falls back to the
    season total.** Falling back is the leak this whole seam exists to close.
    """
    key = (season, as_of)
    if key in _MILB_ASOF:
        return _MILB_ASOF[key]
    try:
        with open(MiLB.milb_asof_path(season, as_of, save_dir)) as fh:
            _MILB_ASOF[key] = json.load(fh)
    except (OSError, ValueError):
        _MILB_ASOF[key] = {}
    return _MILB_ASOF[key]


def available_milb_asof(season: Optional[int] = None,
                        save_dir: Path = SAVE_DIR) -> List[str]:
    """Cutoffs with a cached Triple-A snapshot, ascending."""
    season = CURRENT_SEASON if season is None else int(season)
    d = Path(save_dir) / "asof"
    if not d.is_dir():
        return []
    pre = f"milb_{season}_"
    return sorted(f.name[len(pre):-len(".json")] for f in d.glob(f"{pre}*.json"))


# --- AAA -> MLB translation, MEASURED -------------------------------------
# Every number is measured off matched players; absent the measurement the
# feature is OFF, because a made-up level factor is the exact failure 5.6c spent
# a session undoing. **Only AAA** — the lower levels have single-digit movers.
#
# Two fitted quantities answering different questions:
#   FACTOR   what a AAA rate becomes in MLB. Matched movers in log-odds, BOTH
#            directions — promotions alone charge regression to the mean as
#            difficulty.
#   CREDIT   how many MLB plate appearances one AAA PA is worth, per outcome and
#            side, fitted OUT OF SAMPLE. This is where the pitcher/hitter
#            asymmetry lands (AAA home-run rate: corr +0.044 against +0.579).
#
# **Translation and regression are two operations and it is easy to do one
# twice.** The factor handles the level; `credit` feeding the EXISTING
# stabiliser handles the regression. sim_state.md A.9c.

MILB_TRANSLATION_PATH = SAVE_DIR / "milb_translation.json"
# Minimum sample on each side of a matched pair. Low enough to keep the pairs,
# high enough that a logit is meaningful.
MILB_PAIR_MIN = 50
class MiLB:
    """Minor-league lines, park factors and the AAA->MLB translation (9c)."""

    @staticmethod
    def _accumulate(acc: dict, side: str, level: str,
                    rows: Sequence[dict], keys: Sequence[str]) -> None:
        """Fold one split response into `acc[side][pid][level]`, in place.

        A player with no usable stat in `keys` is skipped rather than stored
        empty — an empty record reads downstream as "he played and did
        nothing", which is not the same as "he has no line at this level".
        `collect_milb` and `collect_milb_asof` had this identical loop.
        """
        for sp in rows:
            pid = ((sp.get("player") or {}).get("id"))
            st = sp.get("stat") or {}
            if pid is None:
                continue
            rec = {k: st.get(k) for k in keys if st.get(k) is not None}
            if not rec:
                continue
            rec.update(MiLB._milb_team(sp))
            acc[side].setdefault(str(int(pid)), {})[level] = rec

    @staticmethod
    def milb_cache_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
        return Path(save_dir) / MILB_CACHE_FMT.format(season=season)

    @staticmethod
    def fetch_milb_split(season: int, sport_id: int, group: str,
                         timeout: float = 90.0) -> List[dict]:
        """One level, one side, one season — every player in the pool.

        `playerPool=ALL` is required: the default returns only qualified players,
        which would drop exactly the thin-sample arms this whole section exists
        for. The limit is set past the largest level (Rookie ball, ~2,100 rows) so
        a silent truncation cannot happen; `totalSplits` is checked against what
        came back rather than trusted.
        """
        return MiLB._stats_rows(
            {"stats": "season", "group": group, "sportId": sport_id,
             "season": season, "playerPool": "ALL", "limit": 5000},
            timeout, f"MiLB {season} sport {sport_id} {group}")

    @staticmethod
    def _stats_rows(params: dict, timeout: float, what: str) -> List[dict]:
        """One StatsAPI `/stats` call, unpacked, with the truncation check.

        The three MiLB fetchers were carrying this identical six lines each. The
        check is the point: the endpoint answers a too-small `limit` with a
        SHORT list and a 200, so a level that quietly lost half its players is
        indistinguishable from a small level. `totalSplits` is what it should
        have sent, so compare and raise rather than shipping the truncation.
        """
        r = requests.get(f"{STATSAPI}/stats", params=params, timeout=timeout)
        r.raise_for_status()
        blk = (r.json().get("stats") or [{}])[0]
        rows = blk.get("splits") or []
        want = blk.get("totalSplits")
        if want and len(rows) < want:
            raise RuntimeError(
                f"mlb_sim: {what} returned {len(rows)} of {want} rows — raise "
                f"the limit rather than shipping a silently truncated level.")
        return rows

    @staticmethod
    def _milb_team(sp: dict) -> Dict[str, object]:
        """The affiliate on a StatsAPI split.

        **This used to read `abbreviation` and always got nothing** — the nested
        team object carries only `{id, name, link}`, so `or ""` swallowed the miss
        and `team` was empty in 100% of 22,684 rows. 5.11.1's park item was
        blocked on that, not on missing data. `id` is the key because affiliates
        rename and relocate and it is the only stable join to `/teams`.
        """
        t = sp.get("team") or {}
        out: Dict[str, object] = {}
        if t.get("id") is not None:
            out["team_id"] = int(t["id"])
        if t.get("name"):
            out["team"] = str(t["name"])
        return out

    @staticmethod
    def collect_milb(seasons: Sequence[int] = (2026,), refresh: bool = False,
                     save_dir: Path = SAVE_DIR, verbose: bool = True) -> dict:
        """Every affiliated minor league line, per season, cached to disk.

        Shape: {"pit": {pid: {level: {...counts}}}, "bat": {...}}. A player who
        moved up mid-season appears under EVERY level he threw at, because the
        levels have to stay separable — averaging a man's Double-A and Triple-A
        lines before the level factors are known destroys the thing that makes
        them measurable.
        """
        out: Dict[str, Dict[str, Dict[str, dict]]] = {}
        for season in seasons:
            path = MiLB.milb_cache_path(season, save_dir)
            if path.exists() and not refresh:
                try:
                    with open(path) as fh:
                        got = json.load(fh)
                    if got.get("pit"):
                        out[str(season)] = got
                        if verbose:
                            print(f"[milb] {season}: cached "
                                  f"({len(got['pit'])} pitchers, "
                                  f"{len(got.get('bat', {}))} hitters)")
                        continue
                except (OSError, ValueError):
                    pass
            acc: Dict[str, Dict[str, Dict[str, dict]]] = {"pit": {}, "bat": {}}
            for sid, level in MILB_LEVELS.items():
                for side, group, keys in (("pit", "pitching", _MILB_PIT_KEYS),
                                          ("bat", "hitting", _MILB_BAT_KEYS)):
                    try:
                        rows = MiLB.fetch_milb_split(season, sid, group)
                    except Exception as e:
                        print(f"[milb] {season} {level} {group} FAILED: {e}")
                        continue
                    MiLB._accumulate(acc, side, level, rows, keys)
                    if verbose:
                        print(f"[milb] {season} {level:>3s} {group:<8s} "
                              f"{len(rows):>5d} rows", flush=True)
            payload = {"season": season, "levels": list(MILB_LEVELS.values()),
                       **acc}
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                json.dump(payload, fh)
            out[str(season)] = payload
            if verbose:
                print(f"[milb] {season}: {len(acc['pit'])} pitchers, "
                      f"{len(acc['bat'])} hitters -> {path.name}")
        return out

    @staticmethod
    def milb_line(pid: int, season: int, side: str = "pit",
                  save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
        """{level: counts} for one player in one season. Empty when he has none."""
        got = load_milb(season, save_dir).get(side) or {}
        return got.get(str(int(pid))) or {}

    @staticmethod
    def milb_park_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
        return Path(save_dir) / MILB_PARK_FMT.format(season=season)

    @staticmethod
    def _split_counts(st: dict, side: str) -> Tuple[Optional[List[float]], float]:
        """One home/away split as the engine's nine outcomes."""
        return MiLB._counts_from_stat(st)

    @staticmethod
    def _counts_from_stat(st: dict) -> Tuple[Optional[List[float]], float]:
        """A StatsAPI `stat` block as the engine's nine outcomes, and its PA.

        The ONE implementation behind `_split_counts` and `_milb_counts`, which
        carried this arithmetic twice. Mirrors `outcome_counts`: singles by
        subtraction, balls in play split by the feed's OWN ground/air ratio.
        **Every read is `or 0`-guarded** — the feed sends a JSON null for a stat a
        level does not track, and unguarded that RAISED on one of the two paths.
        """
        n = float(st.get("battersFaced") or st.get("plateAppearances") or 0.0)
        if n <= 0:
            return None, 0.0
        h = float(st.get("hits", 0) or 0); d = float(st.get("doubles", 0) or 0)
        t = float(st.get("triples", 0) or 0); hr = float(st.get("homeRuns", 0) or 0)
        k = float(st.get("strikeOuts", 0) or 0)
        bb = float(st.get("baseOnBalls", 0) or 0)
        hbp = float(st.get("hitByPitch", 0) or 0)
        go = float(st.get("groundOuts", 0) or 0)
        ao = float(st.get("airOuts", 0) or 0)
        outs = max(n - k - bb - hbp - h, 0.0)
        gshare = go / (go + ao) if (go + ao) > 0 else 0.5
        c = [0.0] * N_OUTCOMES
        c[K], c[BB], c[HBP] = k, bb, hbp
        c[GB_OUT], c[AIR_OUT] = outs * gshare, outs * (1.0 - gshare)
        c[S1B], c[S2B], c[S3B], c[HR] = max(h - d - t - hr, 0.0), d, t, hr
        return c, n

    @staticmethod
    def fetch_milb_park_splits(season: int, group: str,
                               timeout: float = 180.0) -> List[dict]:
        """Every player's HOME and AWAY line at Triple-A, one request."""
        return MiLB._stats_rows(
            {"stats": "statSplits", "group": group,
             "sportId": MILB_ASOF_SPORT, "season": season,
             "sitCodes": "h,a", "playerPool": "ALL", "limit": 10000},
            timeout, f"Triple-A {season} {group} home/away")

    @staticmethod
    def _milb_game_level(pk: int, timeout: float = 15.0) -> Optional[str]:
        """The level a minor-league game was played at, via StatsAPI."""
        pk = int(pk)
        if pk in _MILB_GAME_LEVEL:
            return _MILB_GAME_LEVEL[pk]
        try:
            r = requests.get(f"{STATSAPI}.1/game/{pk}/feed/live",
                             params={"fields": "gameData,teams,home,sport,id"},
                             timeout=timeout)
            r.raise_for_status()
            sid = (((r.json().get("gameData") or {}).get("teams") or {})
                   .get("home") or {}).get("sport", {}).get("id")
            lvl = _MILB_SPORT_LEVEL.get(sid)
        except Exception:                                     # noqa: BLE001
            lvl = None
        _MILB_GAME_LEVEL[pk] = lvl
        return lvl

    @staticmethod
    def _milb_chunk_path(start: str, end: str, save_dir: Path = None) -> Path:
        root = Path(save_dir or SAVE_DIR) / "milb_chunks" / start[:4]
        return root / f"{start}_{end}.json.gz"

    @staticmethod
    def _milb_statcast_chunk(start: str, end: str, timeout: float = 120.0,
                             save_dir: Path = None) -> List[dict]:
        """One date window of minor-league Statcast, as dict rows.

        **CACHED PER WINDOW, because the collector could not resume**: it
        accumulated in memory and wrote at the END of a season, so an interrupt 45
        chunks deep threw away every request. A finished window in a finished
        season cannot change, so this is permanent on the same argument as the
        play-by-play store. Trimmed to `MILB_RAW_KEEP` on write.
        """
        dest = MiLB._milb_chunk_path(start, end, save_dir)
        if dest.exists():
            try:
                with gzip.open(dest, "rt") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass                      # corrupt: fall through and refetch
        r = requests.get("https://baseballsavant.mlb.com/statcast_search/csv",
                         params={"all": "true", "hfSea": f"{start[:4]}|",
                                 "game_date_gt": start, "game_date_lt": end,
                                 "type": "details", "minors": "true"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        r.raise_for_status()
        text = r.text.lstrip("\ufeff")          # the BOM, see the header note
        rows = [{k: x.get(k) for k in MILB_RAW_KEEP + ("game_pk",)}
                for x in csv.DictReader(io.StringIO(text))]
        truncated = len(rows) >= 25000
        if truncated:
            Archive._progress(f"milb-statcast: {start}..{end} hit the 25,000-row cap — "
                      f"NARROW THE WINDOW, this window is truncated")
        # **A truncated window is NOT cached.** Caching it would make the
        # 25,000-row cap permanent and silent: every later run would read the
        # short file back and never learn the window was clipped.
        if not truncated:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                with gzip.open(tmp, "wt") as fh:
                    json.dump(rows, fh)
                tmp.replace(dest)                 # atomic
            except OSError:
                pass
        return rows

    @staticmethod
    def milb_arsenal_row(pitches: Sequence[dict]) -> Dict[str, float]:
        """Per-pitch-type aggregates under the FANGRAPHS board's column names.

        Emitting the board's own names is the point: `_arsenal_block` reads a
        Triple-A arm with no branch. Spin and velocity land on the board's scale;
        **MOVEMENT needs a calibration** — the earlier "NOT comparable" finding
        was an artifact of averaging a SIGNED quantity across handedness. Fit per
        pitch type it is near-linear, so use `ched_core.calibrate_arsenal_row`
        and do not re-derive `MOVEMENT_CAL` here.

        **A units trap the name-matching hid**: `pfx<TYPE>%` is a PERCENT here and
        a FRACTION on the board, so a `usage >= 5` gate matched 0 of 238 rows and
        returned an EMPTY fit rather than a wrong one — the only reason it
        surfaced. Normalise on read (`ched_core.usage_pct`); changing the
        collector would break EffortMLB. sim_state.md A.9c.
        """
        by: Dict[str, List[dict]] = {}
        for p in pitches:
            pt = (p.get("pitch_type") or "").strip().upper()
            if pt in _SAVANT_DROP:
                continue
            pt = _SAVANT_TO_PFX.get(pt, pt)
            by.setdefault(pt, []).append(p)
        n_tot = sum(len(v) for v in by.values())
        if not n_tot:
            return {}
        out: Dict[str, float] = {}

        def _mean(rows, key, scale=1.0):
            vals = []
            for r in rows:
                v = r.get(key)
                try:
                    if v not in (None, "", "null"):
                        vals.append(float(v) * scale)
                except (TypeError, ValueError):
                    continue
            return sum(vals) / len(vals) if vals else None

        for pt, rows in by.items():
            out[f"pfx{pt}%"] = 100.0 * len(rows) / n_tot
            for key, col, sc in (("pfxsp{pt}", "release_spin_rate", 1.0),
                                 ("pfxv{pt}", "release_speed", 1.0),
                                 ("pfx{pt}-X", "pfx_x",
                                  12.0 * MILB_PFX_BREAK_TO_BOARD),
                                 ("pfx{pt}-Z", "pfx_z",
                                  12.0 * MILB_PFX_BREAK_TO_BOARD)):
                v = _mean(rows, col, sc)
                if v is not None:
                    out[key.format(pt=pt)] = v
        out["milb_pitches"] = float(n_tot)

        # **THE DELIVERY, which this used to throw away.** Everything above is
        # the pitch's SHAPE; CHED's thesis is that shape only means something
        # relative to the arm it came from, so the slot regression needs
        # velocity, ARM ANGLE, extension and release position. The raw rows
        # carry all four and the aggregation simply did not emit them, leaving
        # the Triple-A arsenal unable to answer the one question it was
        # collected for. Pitcher-level, not per-type — a pitcher has ONE slot.
        for key, col in (("milb_arm_angle", "arm_angle"),
                         ("milb_extension", "release_extension"),
                         ("milb_rel_x", "release_pos_x"),
                         ("milb_rel_z", "release_pos_z"),
                         ("milb_plate_z", "plate_z")):
            v = _mean(pitches, col)
            if v is not None:
                out[key] = v
        for key, col in (("milb_rel_x_sd", "release_pos_x"),
                         ("milb_rel_z_sd", "release_pos_z")):
            vals = []
            for r in pitches:
                x = r.get(col)
                try:
                    if x not in (None, "", "null"):
                        vals.append(float(x))
                except (TypeError, ValueError):
                    continue
            if len(vals) >= 20:
                mu = sum(vals) / len(vals)
                out[key] = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5
        return out

    @staticmethod
    def _write_milb_raw(season: int, by_pitcher: Dict[str, List[dict]],
                        save_dir: Path, verbose: bool = True) -> None:
        """Raw minor-league pitches, one gzip per pitcher, keyed by season.

        Trimmed to `MILB_RAW_KEEP` — the full Savant row is ~90% columns we
        never read, and the difference is ~60 MB against ~1.5 GB a season, the
        same trade `framing_pitches` makes.
        """
        root = Path(save_dir) / "milb_pitches" / MILB_ARSENAL_VERSION / str(season)
        root.mkdir(parents=True, exist_ok=True)
        n = 0
        for pid, rows in by_pitcher.items():
            trimmed = [{k: r.get(k) for k in MILB_RAW_KEEP} for r in rows]
            tmp = root / f"{pid}.json.gz.tmp"
            try:
                with gzip.open(tmp, "wt") as fh:
                    json.dump(trimmed, fh)
                tmp.replace(root / f"{pid}.json.gz")   # atomic
                n += 1
            except OSError:
                continue
        if verbose:
            print(f"[milb-statcast] {season}: raw kept for {n} pitchers -> {root}")

    @staticmethod
    def collect_milb_statcast(seasons: Sequence[int] = (2026,),
                              refresh: bool = False,
                              save_dir: Path = SAVE_DIR,
                              levels: Optional[Sequence[str]] = None,
                              start: str = "-03-15", end: str = "-10-05",
                              verbose: bool = True) -> dict:
        """Minor-league Statcast, aggregated per pitcher and stored ALONGSIDE the
        existing minor-league lines in `milb_<season>.json` under `"arsenal"`.

        Deliberately not a separate artifact: it is the same players, the same
        season and the same cache, and a second file would drift out of step with
        the first the moment one of them is refreshed.
        """
        levels = MILB_STATCAST_LEVELS if levels is None else levels
        out: Dict[str, dict] = {}
        for season in seasons:
            path = MiLB.milb_cache_path(season, save_dir)
            got = {}
            if path.exists():
                try:
                    with open(path) as fh:
                        got = json.load(fh)
                except (OSError, ValueError):
                    got = {}
            if got.get("arsenal") and not refresh:
                if verbose:
                    print(f"[milb-statcast] {season}: cached "
                          f"({len(got['arsenal'])} pitchers)")
                out[str(season)] = got
                continue
            by_pitcher: Dict[str, List[dict]] = {}
            d0 = datetime.date.fromisoformat(f"{season}{start}")
            d1 = datetime.date.fromisoformat(f"{season}{end}")
            today = datetime.date.today()
            if d1 > today:
                d1 = today
            cur = d0
            kept = seen = 0
            windows = []
            while cur <= d1:
                hi = min(cur + datetime.timedelta(days=MILB_STATCAST_CHUNK_DAYS - 1),
                         d1)
                windows.append((cur.isoformat(), hi.isoformat()))
                cur = hi + datetime.timedelta(days=1)

            # **Fetched in parallel, consumed in order.** Independent date
            # ranges; the sequential loop spent ~50s of wall clock per chunk on
            # one socket. The LEVEL JOIN stays single-threaded — `_milb_game_
            # level` memoises into a shared dict and costs nothing.
            def _grab(w):
                try:
                    return w, MiLB._milb_statcast_chunk(w[0], w[1],
                                                        save_dir=save_dir)
                except Exception as e:                        # noqa: BLE001
                    Archive._progress(f"milb-statcast {w[0]}..{w[1]}: "
                                      f"{type(e).__name__} {e}")
                    return w, []

            done = 0
            with ThreadPoolExecutor(max_workers=MILB_STATCAST_WORKERS) as ex:
                for (lo_s, hi_s), rows in ex.map(_grab, windows):
                    seen += len(rows)
                    for r in rows:
                        pk = r.get("game_pk")
                        if not pk:
                            continue
                        lvl = MiLB._milb_game_level(pk)
                        if lvl not in levels:
                            continue
                        pid = r.get("pitcher")
                        if not pid:
                            continue
                        by_pitcher.setdefault(str(int(float(pid))), []).append(r)
                        kept += 1
                    done += 1
                    if verbose:
                        print(f"[milb-statcast] {season} {lo_s}..{hi_s}  "
                              f"[{done}/{len(windows)}] {len(rows):6d} rows, "
                              f"kept {kept}", flush=True)
            arsenal = {pid: MiLB.milb_arsenal_row(v) for pid, v in by_pitcher.items()}
            arsenal = {k: v for k, v in arsenal.items() if v}
            got["arsenal"] = arsenal
            got["arsenal_levels"] = list(levels)
            # **Version the aggregate.** v1 had no delivery columns, and a v1
            # file loads without error while every slot regressor reads None —
            # the silent-corruption shape `framing_pitches` is versioned
            # against. A consumer that needs the delivery must check this.
            got["arsenal_version"] = MILB_ARSENAL_VERSION
            # **Keep the RAW pitches.** They were fetched, aggregated and
            # dropped, so changing what we extract meant re-scraping the whole
            # minor-league season — which is what this cost the first time.
            # Same argument the play-by-play cache records: the win is being
            # able to change the extraction without going back over the wire.
            MiLB._write_milb_raw(season, by_pitcher, save_dir, verbose)
            got.setdefault("season", season)
            with open(path, "w") as fh:
                json.dump(got, fh)
            if verbose:
                print(f"[milb-statcast] {season}: {seen} pitches seen, {kept} at "
                      f"{'/'.join(levels)}, {len(arsenal)} pitchers -> {path}")
            out[str(season)] = got
        return out

    @staticmethod
    def collect_milb_park(seasons: Sequence[int] = (2026,), refresh: bool = False,
                          save_dir: Path = SAVE_DIR, verbose: bool = True) -> dict:
        """Per-club, per-outcome Triple-A park factors, cached per season.

        Shape: {"pit": {team_id: [factor x 9]}, "bat": {...}, "n": {...}}.

        The factor is a club's HOME rate over its own ROAD rate, which is the
        standard construction: it holds the roster fixed, so it cannot be read as
        "this park scores a lot" when what is really true is "the two clubs who
        play here are good". Raw runs at a venue is NOT a park factor (§6.1).
        """
        out: Dict[str, dict] = {}
        for season in seasons:
            path = MiLB.milb_park_path(season, save_dir)
            if path.exists() and not refresh:
                try:
                    with open(path) as fh:
                        got = json.load(fh)
                    if got.get("bat"):
                        out[str(season)] = got
                        if verbose:
                            print(f"[milbpark] {season}: cached "
                                  f"({len(got['bat'])} clubs)")
                        continue
                except (OSError, ValueError):
                    pass
            acc: Dict[str, dict] = {}
            cnt: Dict[str, dict] = {}
            for side, group in (("bat", "hitting"), ("pit", "pitching")):
                try:
                    rows = MiLB.fetch_milb_park_splits(season, group)
                except Exception as e:
                    print(f"[milbpark] {season} {group} FAILED: {e}")
                    continue
                tot: Dict[int, Dict[str, List[float]]] = {}
                for sp in rows:
                    tid = ((sp.get("team") or {}).get("id"))
                    code = ((sp.get("split") or {}).get("code"))
                    if tid is None or code not in ("h", "a"):
                        continue
                    c, n = MiLB._split_counts(sp.get("stat") or {}, side)
                    if c is None:
                        continue
                    rec = tot.setdefault(int(tid), {"h": [0.0] * (N_OUTCOMES + 1),
                                                    "a": [0.0] * (N_OUTCOMES + 1)})
                    for i in range(N_OUTCOMES):
                        rec[code][i] += c[i]
                    rec[code][N_OUTCOMES] += n
                fac: Dict[str, List[float]] = {}
                nn: Dict[str, List[float]] = {}
                for tid, rec in tot.items():
                    hn, an = rec["h"][N_OUTCOMES], rec["a"][N_OUTCOMES]
                    if hn < 500 or an < 500:
                        continue
                    f = []
                    for i in range(N_OUTCOMES):
                        hr_, ar_ = rec["h"][i] / hn, rec["a"][i] / an
                        # A club with zero of an outcome at home is a sample
                        # problem, not a park that forbids triples. Leave it at 1.
                        f.append((hr_ / ar_) if (hr_ > 0 and ar_ > 0) else 1.0)
                    fac[str(tid)] = f
                    nn[str(tid)] = [hn, an]
                acc[side] = fac
                cnt[side] = nn
                if verbose:
                    print(f"[milbpark] {season} {group:<9s} {len(fac)} clubs "
                          f"from {len(rows)} splits", flush=True)
            payload = {"season": season, "n": cnt, **acc}
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                json.dump(payload, fh)
            out[str(season)] = payload
        return out

    @staticmethod
    def load_milb_park(season: int, save_dir: Path = SAVE_DIR) -> dict:
        if season in _MILB_PARK:
            return _MILB_PARK[season]
        try:
            with open(MiLB.milb_park_path(season, save_dir)) as fh:
                _MILB_PARK[season] = json.load(fh)
        except (OSError, ValueError):
            _MILB_PARK[season] = {}
        return _MILB_PARK[season]

    @staticmethod
    def milb_park_factor(team_id: Optional[int], season: int, side: str = "bat",
                         window: Optional[int] = None,
                         save_dir: Path = SAVE_DIR) -> Optional[List[float]]:
        """A club's per-outcome park factor, averaged over `window` seasons.

        Averaged rather than taken from the target season alone for §8's reason:
        the noise falls as sqrt(n) while the true park effect survives, so the
        window is an arithmetic improvement and not a fitted one. Returns None
        when the club has no usable season, which means "do not adjust" — never a
        default factor of 1.0 dressed up as a measurement.
        """
        window = MILB_PARK_WINDOW if window is None else int(window)
        if team_id is None:
            return None
        acc = [0.0] * N_OUTCOMES
        got = 0
        for s in range(season - window + 1, season + 1):
            f = ((MiLB.load_milb_park(s, save_dir).get(side) or {})
                 .get(str(int(team_id))))
            if not f:
                continue
            for i in range(N_OUTCOMES):
                acc[i] += f[i]
            got += 1
        if not got:
            return None
        return [acc[i] / got for i in range(N_OUTCOMES)]

    @staticmethod
    def milb_park_report(season: Optional[int] = None, save_dir: Path = SAVE_DIR) -> None:
        """The spread and the persistence — the two things that decide whether a
        park factor is real or a season of noise (§8)."""
        season = CURRENT_SEASON if season is None else int(season)
        got = MiLB.load_milb_park(season, save_dir)
        if not got:
            raise SystemExit(f"mlb_sim: no Triple-A park factors for {season}. "
                             f"Run `python mlb_sim.py milbpark --refresh` first.")
        print(f"\nTriple-A PARK FACTORS — {season}, home rate over own road rate\n")
        for side in ("bat", "pit"):
            fac = got.get(side) or {}
            if not fac:
                continue
            print(f"  {side.upper()}  ({len(fac)} clubs)")
            print(f"    {'outcome':<9s}{'min':>8s}{'median':>9s}{'max':>8s}"
                  f"{'sd':>8s}")
            for i, nm in enumerate(OUTCOME_NAMES):
                v = sorted(f[i] for f in fac.values())
                if not v:
                    continue
                print(f"    {nm:<9s}{v[0]:>8.3f}{statistics.median(v):>9.3f}"
                      f"{v[-1]:>8.3f}{statistics.pstdev(v):>8.3f}")
            print()
        # persistence, which is what separates a factor from a season of noise
        prev = MiLB.load_milb_park(season - 1, save_dir)
        if prev.get("bat"):
            for side in ("bat", "pit"):
                a_, b_ = (prev.get(side) or {}), (got.get(side) or {})
                keys = sorted(set(a_) & set(b_))
                if len(keys) < 10:
                    continue
                print(f"  {side.upper()} year-over-year correlation "
                      f"{season-1} -> {season}  (n={len(keys)})")
                for i, nm in enumerate(OUTCOME_NAMES):
                    x = [a_[k][i] for k in keys]
                    y = [b_[k][i] for k in keys]
                    mx, my = statistics.mean(x), statistics.mean(y)
                    num = sum((p - mx) * (q - my) for p, q in zip(x, y))
                    den = (sum((p - mx) ** 2 for p in x)
                           * sum((q - my) ** 2 for q in y)) ** 0.5
                    if den > 0:
                        print(f"    {nm:<9s}{num/den:>+8.3f}")
                print()

    @staticmethod
    def milb_asof_path(season: int, as_of: str,
                       save_dir: Path = SAVE_DIR) -> Path:
        return Path(save_dir) / "asof" / MILB_ASOF_FMT.format(season=season,
                                                              as_of=as_of)

    @staticmethod
    def fetch_milb_asof_split(season: int, sport_id: int, group: str, as_of: str,
                              timeout: float = 120.0) -> List[dict]:
        """One level, one side, season-to-date through `as_of`.

        Same contract as `fetch_milb_split` — `playerPool=ALL` so the thin arms
        this exists for are not dropped as unqualified, and `totalSplits` checked
        rather than trusted so a silent truncation cannot ship.
        """
        return MiLB._stats_rows(
            {"stats": "byDateRange", "group": group, "sportId": sport_id,
             "season": season, "playerPool": "ALL", "limit": 5000,
             "startDate": f"{season}-01-01", "endDate": as_of},
            timeout, f"MiLB as-of {season} {as_of} sport {sport_id} {group}")

    @staticmethod
    def collect_milb_asof(cutoffs: Sequence[str], season: Optional[int] = None,
                          save_dir: Path = SAVE_DIR, force: bool = False,
                          verbose: bool = True) -> Dict[str, int]:
        """Cache a Triple-A snapshot per cutoff, in `collect_milb`'s shape.

        Stored under the same `{level: counts}` nesting the season cache uses so
        `_milb_counts` reads either one unchanged, and under the SOURCE's key names
        for the reason recorded on `_MILB_PIT_KEYS`: a cache that has already been
        interpreted cannot be re-interpreted when the interpretation moves.
        """
        season = CURRENT_SEASON if season is None else int(season)
        got: Dict[str, int] = {}
        level = MILB_LEVELS[MILB_ASOF_SPORT]
        for as_of in cutoffs:
            dest = MiLB.milb_asof_path(season, as_of, save_dir)
            if dest.exists() and not force:
                if verbose:
                    print(f"[milb-asof] {season} {as_of}: cached")
                continue
            acc: Dict[str, Dict[str, Dict[str, dict]]] = {"pit": {}, "bat": {}}
            ok = True
            for side, group, keys in (("pit", "pitching", _MILB_PIT_KEYS),
                                      ("bat", "hitting", _MILB_BAT_KEYS)):
                try:
                    rows = MiLB.fetch_milb_asof_split(season, MILB_ASOF_SPORT, group,
                                                 as_of)
                except Exception as e:
                    print(f"[milb-asof] {season} {as_of} {group} FAILED: {e}")
                    ok = False
                    continue
                MiLB._accumulate(acc, side, level, rows, keys)
            if not ok:
                # A half-written snapshot would read as "this player had no
                # Triple-A record", which is the one thing the file must never
                # say by accident. Skip the cutoff instead.
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w") as fh:
                json.dump({"season": season, "as_of": as_of, "levels": [level],
                           **acc}, fh)
            got[as_of] = len(acc["pit"]) + len(acc["bat"])
            if verbose:
                print(f"[milb-asof] {season} {as_of}: {len(acc['pit'])} pitchers, "
                      f"{len(acc['bat'])} hitters -> {dest.name}", flush=True)
        return got

    @staticmethod
    def milb_report(season: Optional[int] = None, save_dir: Path = SAVE_DIR) -> None:
        """How much minor league evidence exists for the arms the model is
        thinnest on — the check that this is worth wiring, before it is wired."""
        season = CURRENT_SEASON if season is None else int(season)
        milb = load_milb(season, save_dir)
        if not milb:
            raise SystemExit(f"mlb_sim: no MiLB cache for {season}. "
                             f"Run `python mlb_sim.py milb --refresh` first.")
        board = {_row_id(r): r for r in (load_board("pit", season, save_dir) or [])
                 if _row_id(r)}
        thin = sorted(((pid, _num(r, "TBF")) for pid, r in board.items()
                       if 0 < _num(r, "TBF") < 150), key=lambda x: x[1])
        covered = [(p, t) for p, t in thin if MiLB.milb_line(p, season, "pit", save_dir)]
        extra = []
        for pid, tbf in covered:
            m_tbf = sum(v.get("battersFaced", 0)
                        for v in MiLB.milb_line(pid, season, "pit", save_dir).values())
            extra.append((pid, tbf, m_tbf))
        print(f"\nMiLB coverage for THIN major league arms — {season}\n")
        print(f"  pitchers on the board under 150 TBF   {len(thin)}")
        print(f"  of those with a minor league line     {len(covered)} "
              f"({len(covered)/max(len(thin),1):.0%})")
        if extra:
            gain = [m / t for _, t, m in extra if t > 0]
            print(f"  median MiLB batters faced             "
                  f"{sorted(m for _, _, m in extra)[len(extra)//2]:.0f}")
            print(f"  median SAMPLE MULTIPLE from MiLB      "
                  f"{sorted(gain)[len(gain)//2]:.1f}x")
        print(f"\n  {'pitcher':<24s}{'MLB TBF':>9s}{'MiLB TBF':>10s}{'x':>6s}  levels")
        for pid, tbf, m_tbf in sorted(extra, key=lambda x: -x[2])[:12]:
            lv = MiLB.milb_line(pid, season, "pit", save_dir)
            nm = (board[pid].get("PlayerName") or str(pid))[:23]
            print(f"  {nm:<24s}{tbf:>9.0f}{m_tbf:>10.0f}"
                  f"{(m_tbf/tbf if tbf else 0):>6.1f}  "
                  f"{'+'.join(sorted(lv))}")

    @staticmethod
    def _side_counts(row: dict, side: str) -> Tuple[Optional[List[float]], float]:
        got = outcome_counts(row, side)
        return (list(got[0]) if got[0] else None), float(got[1] or 0.0)

    @staticmethod
    def _milb_counts(lv: dict, level: str = "AAA"
                     ) -> Tuple[Optional[List[float]], float]:
        """A player's line AT ONE LEVEL as the engine's nine outcomes.

        Built from the StatsAPI counts stored raw by `collect_milb`, mirroring
        `outcome_counts`: singles are hits minus the extra-base hits, and the
        balls in play are split by the feed's own ground/air out ratio rather than
        by a league constant.
        """
        return MiLB._counts_from_stat((lv or {}).get(level) or {})

    @staticmethod
    def milb_step_factor(side: str, lo: str, hi: str,
                         seasons: Sequence[int] = (2024, 2025, 2026),
                         save_dir: Path = SAVE_DIR,
                         n_min: Optional[float] = None
                         ) -> Tuple[List[float], int]:
        """One rung: `logit(rate at hi) - logit(rate at lo)` for same-season movers.

        Fitted exactly the way the AAA->MLB factor is — matched within-season, both
        directions pooled, weighted by the smaller of the two samples — so the
        rungs compose on the same scale.
        """
        n_min = MILB_PAIR_MIN if n_min is None else float(n_min)
        num = [0.0] * N_OUTCOMES
        den = [0.0] * N_OUTCOMES
        pairs = 0
        for season in seasons:
            milb = (load_milb(season, save_dir).get(side) or {})
            for lv in milb.values():
                cl, nl = MiLB._milb_counts(lv, lo)
                ch, nh = MiLB._milb_counts(lv, hi)
                if cl is None or ch is None or nl < n_min or nh < n_min:
                    continue
                pairs += 1
                w = min(nl, nh)
                for i in range(N_OUTCOMES):
                    pl, ph = cl[i] / nl, ch[i] / nh
                    if pl <= 0 or ph <= 0:
                        continue
                    num[i] += w * (_logit(ph) - _logit(pl))
                    den[i] += w
        return ([(num[i] / den[i]) if den[i] > 0 else 0.0
                 for i in range(N_OUTCOMES)], pairs)

    @staticmethod
    def milb_level_factors(side: str, aaa_to_mlb: Sequence[float],
                           seasons: Sequence[int] = (2024, 2025, 2026),
                           save_dir: Path = SAVE_DIR
                           ) -> Tuple[Dict[str, List[float]], Dict[str, int]]:
        """{level: factor onto MLB} by composing the rungs onto `aaa_to_mlb`."""
        out = {"AAA": list(aaa_to_mlb)}
        counts: Dict[str, int] = {}
        acc = list(aaa_to_mlb)
        for lo, hi in zip(MILB_CHAIN[1:], MILB_CHAIN[:-1]):
            step, n = MiLB.milb_step_factor(side, lo, hi, seasons, save_dir)
            acc = [acc[i] + step[i] for i in range(N_OUTCOMES)]
            out[lo] = list(acc)
            counts[lo] = n
        return out, counts

    @staticmethod
    def _milb_level_evidence(lv, factors, fallback):
        """`milb_evidence` returning COUNTS, for `build_rates`'s accumulator."""
        if not lv:
            return None, 0.0
        rates, n = MiLB.milb_evidence(lv, factors or {"AAA": list(fallback)})
        if rates is None:
            return None, 0.0
        return [r * n for r in rates], n

    @staticmethod
    def milb_evidence(lv: dict, factors: Dict[str, List[float]]
                      ) -> Tuple[Optional[List[float]], float]:
        """A player's WHOLE minor-league season as one MLB-equivalent rate + n.

        Every level he played at is translated onto the MLB scale and the counts
        are POOLED, rather than taking only his highest level. Once translated the
        levels are on one scale, so pooling is the right operation and throwing
        away the 300 batters he faced at Double-A because he also threw 18 innings
        at Triple-A is not.
        """
        tot = [0.0] * N_OUTCOMES
        n_tot = 0.0
        for level in MILB_CHAIN:
            fac = factors.get(level)
            if not fac:
                continue
            c, n = MiLB._milb_counts(lv, level)
            if c is None or n < MILB_MIN_LEVEL_N:
                continue
            rates = MiLB.translate_milb([c[i] / n for i in range(N_OUTCOMES)], fac)
            for i in range(N_OUTCOMES):
                tot[i] += rates[i] * n
            n_tot += n
        if n_tot <= 0:
            return None, 0.0
        return [tot[i] / n_tot for i in range(N_OUTCOMES)], n_tot

    @staticmethod
    def measure_milb_translation(seasons: Sequence[int] = (2024, 2025, 2026),
                                save_dir: Path = SAVE_DIR,
                                verbose: bool = True) -> dict:
        """Fit the AAA level factors and per-outcome credit, and cache them."""
        out: Dict[str, dict] = {"seasons": list(seasons), "factor": {},
                                "credit": {}, "pairs": {}, "fit_n": {}}
        for side in ("bat", "pit"):
            # --- FACTOR: matched within-season movers, both directions pooled
            num = [0.0] * N_OUTCOMES
            den = [0.0] * N_OUTCOMES
            pairs = 0
            for season in seasons:
                milb = (load_milb(season, save_dir).get(side) or {})
                for row in (load_board(side, season, save_dir) or []):
                    pid = _row_id(row)
                    if pid is None:
                        continue
                    mc, mn = MiLB._side_counts(row, side)
                    ac, an = MiLB._milb_counts(milb.get(str(pid)))
                    if mc is None or ac is None:
                        continue
                    if mn < MILB_PAIR_MIN or an < MILB_PAIR_MIN:
                        continue
                    pairs += 1
                    w = min(mn, an)
                    for i in range(N_OUTCOMES):
                        pm, pa_ = mc[i] / mn, ac[i] / an
                        if pm <= 0 or pa_ <= 0:
                            continue
                        num[i] += w * (_logit(pm) - _logit(pa_))
                        den[i] += w
            factor = [(num[i] / den[i]) if den[i] > 0 else 0.0
                      for i in range(N_OUTCOMES)]
            out["factor"][side] = factor
            out["pairs"][side] = pairs
            # The rest of the ladder, composed onto the Triple-A step.
            lvf, lvn = MiLB.milb_level_factors(side, factor, seasons, save_dir)
            out.setdefault("factor_by_level", {})[side] = lvf
            out.setdefault("level_pairs", {})[side] = lvn

            # --- CREDIT: season t AAA against season t+1 MLB, fitted out of sample
            stab = stabilize_for(side)
            lg = league_baseline(load_board(side, max(seasons), save_dir), side)
            rows = []
            for a_season in seasons:
                b_season = a_season + 1
                if b_season not in seasons:
                    continue
                milb = (load_milb(a_season, save_dir).get(side) or {})
                own: Dict[int, tuple] = {}
                for _r in (load_board(side, a_season, save_dir) or []):
                    _p = _row_id(_r)
                    if _p is not None:
                        own[_p] = MiLB._side_counts(_r, side)
                for row in (load_board(side, b_season, save_dir) or []):
                    pid = _row_id(row)
                    if pid is None:
                        continue
                    tc, tn = MiLB._side_counts(row, side)
                    ac, an = MiLB._milb_counts(milb.get(str(pid)))
                    if tc is None or ac is None or tn < MILB_TARGET_MIN:
                        continue
                    tr = MiLB.translate_milb([ac[i] / an for i in range(N_OUTCOMES)],
                                       factor)
                    # His OWN MLB line in the SAME season as the Triple-A one —
                    # needed by the "applied" specification below.
                    oc, on = own.get(pid, (None, 0.0))
                    orates = ([oc[i] / on for i in range(N_OUTCOMES)]
                              if oc and on > 0 else None)
                    rows.append(([tc[i] / tn for i in range(N_OUTCOMES)], tr, an,
                                 orates, on))

            # **TWO specifications, and the difference is not cosmetic.**
            # "twoway" scores AAA against LEAGUE — a weak opponent, and what the
            # credit was originally fitted under. "applied" scores it against
            # league AND his own MLB record, which is how `build_rates` uses it;
            # fitted the first way the credits come out 1.3-2.7x too high.
            # Measured: under "applied" the out-of-fold gain halves but stays
            # positive on all 18 outcome-sides. BOTH are stored, chosen by
            # `MILB_CREDIT_SPEC`, so the change is an A/B arm rather than a
            # silent re-fit. sim_state.md A.9c.
            grid = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0,
                    1.4, 2.0, 3.0]

            def _fit(spec: str) -> List[float]:
                got = [0.0] * N_OUTCOMES
                if not rows:
                    return got
                for i in range(N_OUTCOMES):
                    best, best_c = None, 0.0
                    for c in grid:
                        sse = 0.0
                        for tgt, tr, an, orates, on in rows:
                            w = (c * an) / (c * an + stab[i]) if c > 0 else 0.0
                            pred = w * tr[i] + (1.0 - w) * lg[i]
                            if spec == "applied" and orates is not None and on > 0:
                                w_o = on / (on + stab[i])
                                pred = w_o * orates[i] + (1.0 - w_o) * pred
                            sse += (pred - tgt[i]) ** 2
                        if best is None or sse < best:
                            best, best_c = sse, c
                    got[i] = best_c
                return got

            out["credit"][side] = _fit("twoway")
            out.setdefault("credit_applied", {})[side] = _fit("applied")
            out["fit_n"][side] = len(rows)
            if verbose:
                print(f"[aaa] {side}: {pairs} matched pairs, {len(rows)} fit rows")
        save_dir.mkdir(parents=True, exist_ok=True)
        with open(MILB_TRANSLATION_PATH, "w") as fh:
            json.dump(out, fh, indent=1)
        return out

    @staticmethod
    def translate_milb(rates: Sequence[float],
                      factor: Sequence[float]) -> List[float]:
        """A AAA rate vector expressed on the MLB scale."""
        return _normalize([_expit(_logit(rates[i]) + factor[i])
                           for i in range(N_OUTCOMES)])

    @staticmethod
    def aaa_translation_report(save_dir: Path = SAVE_DIR) -> None:
        got = load_milb_translation(save_dir)
        if not got:
            raise SystemExit("mlb_sim: no AAA translation on disk — run "
                             "`python mlb_sim.py aaa --refresh`")
        print(f"\nAAA -> MLB translation, fitted on {got['seasons']}\n")
        for side in ("bat", "pit"):
            f, c = got["factor"][side], got["credit"][side]
            print(f"  {side.upper()}  ({got['pairs'][side]} matched pairs, "
                  f"{got['fit_n'][side]} fit rows)")
            print(f"    {'outcome':<9s}{'log-odds shift':>15s}{'rate x':>9s}"
                  f"{'credit':>9s}{'AAA PA worth':>14s}")
            for i, nm in enumerate(OUTCOME_NAMES):
                mult = math.exp(f[i])
                print(f"    {nm:<9s}{f[i]:>+15.3f}{mult:>9.3f}{c[i]:>9.2f}"
                      f"{(f'{c[i]:.2f} MLB PA' if c[i] else 'not used'):>14s}")
            print()


# Target sample for the credit fit — the season t+1 line has to be reliable
# enough to be worth fitting against.
MILB_TARGET_MIN = 150
# **SHIPPED ON 2026-08-20, after the third A/B and two structural fixes.** The
# two versions that failed did so for implementation reasons — no MLB-sample
# gate, and it overwrote the playing-time prior's LEVEL instead of carrying a
# deviation from the peer mean. Gated and centred: level bias, correlation with
# the line and disagreement sd all better in BOTH seasons, moneyline NEUTRAL.
# **It ships for ACCURACY, not for edge** — the minor league line is public, so
# a neutral moneyline is the expected result. sim_state.md A.9c.
USE_MILB_PRIOR = True

# **The Triple-A line is only worth having where the MLB record is thin, and
# the feature had been applied to EVERYONE.** Out-of-sample gain over regressing
# to league, by how much MLB record the player already had:
#
#     prior MLB sample     hitters     pitchers
#     none                 +43.8%       +24.7%
#     1-49                  +2.5%       +11.5%
#     50-149                +0.5%        +4.8%
#     150+                  +0.2%        +1.2%
#
# 63% of hitter rows sat in that last bucket. A gate is also what the published
# systems do (Rotochamp: under 400 MLB PA). 0 disables it. sim_state.md A.9c.
MILB_MLB_PA_GATE = 150.0

# Which credit fit to use — see `measure_milb_translation`. "twoway" is the
# original (AAA vs league); "applied" is the same fit run under the model the
# credit is actually used in. Off by default: the applied credits are measured
# and better out of sample, but nothing here ships on a measurement of the
# rate layer alone (3d.1).
MILB_CREDIT_SPEC = "twoway"


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1.0 - 1e-4)
    return math.log(p / (1.0 - p))


def _expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# The rungs, best level first. A player is translated from EVERY level he
# played at, not just the top one — see `milb_evidence`.
#
# **Each rung is fitted on its OWN movers, then composed.** An MLB+AAA pair in
# one season is common (529 batters / 730 pitchers); MLB+AA is not — 26 and 52,
# about three players per outcome. But AA->AAA movers are plentiful, because
# moving up inside the minors is the normal career path and reaching the majors
# is not. Borrowing the Triple-A factor for a Double-A line — the obvious
# shortcut — reads Kade Anderson's AA line as a 32.0% MLB strikeout rate against
# a fitted 25.1%, and prices his debut at 69.5% against a market 51.7%.
MILB_CHAIN: Tuple[str, ...] = ("AAA", "AA", "A+", "A")

# Rungs BELOW this are ignored: the fit exists for them but a player whose only
# record is Low-A is not someone the engine can say anything useful about, and
# the translation error compounds multiplicatively along the chain.
MILB_MIN_LEVEL_N = 40.0


_AAA: Optional[dict] = None


def load_milb_translation(save_dir: Path = SAVE_DIR) -> dict:
    """The fitted translation, or {} — in which case the prior is not used.

    **No fallback constants on purpose.** A level factor nobody measured is
    the stand-in pattern of 5.6c, and it would be applied to every thin-sample
    arm on the board.
    """
    global _AAA
    if _AAA is None:
        try:
            with open(Path(save_dir) / MILB_TRANSLATION_PATH.name) as fh:
                _AAA = json.load(fh)
        except (OSError, ValueError):
            _AAA = {}
    return _AAA


def milb_prior(prior: Sequence[float], aaa_rates: Sequence[float],
              n_aaa: float, credit: Sequence[float],
              stabilize: Sequence[float],
              anchor: Optional[Sequence[float]] = None,
              center: Optional[Sequence[float]] = None) -> List[float]:
    """Move the prior toward what this player did at Triple-A.

    Per outcome, with the weight set by the CREDITED sample against the same
    stabilisation constant the player's own MLB line is judged by — so a
    hitter's AAA strikeout rate, which is worth a lot, moves the prior a long
    way, and a pitcher's AAA home-run rate, which is worth almost nothing,
    barely moves it at all. Same shape as `stuff_prior` and `contact_prior`.
    """
    # `anchor` is what the Triple-A line is blended AGAINST — the league, when
    # it displaces the playing-time proxy rather than refining it.
    #
    # **`center` is the fix for the choice between them (5.11.2).** Displacing
    # discards a real measured effect; composing double-counts the pessimism.
    # Both treat the Triple-A line as evidence about the player's LEVEL, and it
    # is not — it is evidence about where he sits AMONG PLAYERS LIKE HIM:
    #
    #     prior + w * (his translated line - what a player like him looks like)
    #
    # The mean deviation is zero by construction, so only the SPREAD is added.
    # Standard empirical Bayes, and why this cannot reproduce displacement's
    # -1.18% population bias.
    base = list(anchor) if anchor else list(prior)
    out = []
    for i in range(N_OUTCOMES):
        eff = credit[i] * n_aaa
        w = eff / (eff + stabilize[i]) if eff > 0 else 0.0
        if center is not None:
            out.append(base[i] + w * (aaa_rates[i] - center[i]))
        else:
            out.append(w * aaa_rates[i] + (1.0 - w) * base[i])
    return _normalize(out)


# ===========================================================================
# 10. BALL FLIGHT, FENCE GEOMETRY AND THE DISTANCE CALIBRATION
#
# Trajectory banks, per-park fence grids, and `calibrate_distance`, which fits
# `distance_scale` against real home-run outcomes.
#
# **The park x weather HOME-RUN MULTIPLIER built on top of this was REMOVED on
# 2026-08-15**, with `park_context`, `apply_park`, `hr_multiplier`,
# `regress_park` and `PARK_RELIABILITY`. It measured WORSE THAN LEAVING IT OUT
# on both quantities it could claim to help, and a real centring bug was found
# and fixed FIRST without rescuing it — which is why it is gone rather than
# gated. Do not reintroduce it without a measurement that BEATS leaving it out.
# sim_state.md A.10.
# ===========================================================================


# Shared with `homerunwidget` and `weatherman`, which anchor the same
# directory off `OddsAPI/` — so this one reaches UP out of `Sims/`.
DATA_DIR = _APP_ROOT / "model_data"
CALIB_PATH = DATA_DIR / "hr_distance_calibration.json"

# Neutral reference conditions. The multiplier is always a ratio against
# these, so they only have to be FIXED, not "average" in any deep sense.
NEUTRAL_TEMP_F = 70.0
NEUTRAL_HUMIDITY = 50.0
NEUTRAL_ALTITUDE_FT = 0.0
NEUTRAL_WIND_MPH = 0.0

# Only air balls can leave the yard. Everything outside this window is a
# ground ball or a pop-up and is skipped — it is ~75% of batted balls, and
# skipping it is what makes a per-hitter physics pass affordable.
LA_MIN, LA_MAX = 10.0, 50.0
EV_MIN = 85.0

_SIM = None


class BallFlight:
    """Ball flight, fence geometry and the cached trajectory banks."""

    @staticmethod
    def _sim():
        """The ball-flight simulator, imported lazily.

        `homerunwidget` pulls in scipy and pywavefront, so importing it at module
        load would put both on the GUI's import path for no reason.
        """
        global _SIM
        if _SIM is None:
            from homerunwidget import BallFlightSimulator
            _SIM = BallFlightSimulator()
        return _SIM

    @staticmethod
    def _cd_neutral() -> float:
        from homerunwidget import CD_NEUTRAL
        return CD_NEUTRAL

    @staticmethod
    def _bank_key(weather: Optional[dict], venue: Optional[str]) -> str:
        """Cache key for a bank. Rounded, because the bank does not meaningfully
        move on a half-degree of temperature or a degree of wind bearing, and a
        key that never repeats is a cache that never hits."""
        w = weather or {}
        parts = [
            venue or "_neutral",
            f"t{round(float(w.get('temp_f', NEUTRAL_TEMP_F)))}",
            f"h{round(float(w.get('humidity', NEUTRAL_HUMIDITY)) / 10) * 10}",
            f"w{round(float(w.get('wind_mph', NEUTRAL_WIND_MPH)))}",
            f"d{round(float(w.get('wind_dir_deg', 0.0)) / 10) * 10}",
            f"f{w.get('wind_frame', 'field')}",
        ]
        p = w.get("pressure_pa")
        if p:
            parts.append(f"p{round(float(p) / 100)}"
                         f"{'s' if w.get('pressure_is_station') else ''}")
        return "_".join(str(x).replace(" ", "-").replace("/", "-") for x in parts)

    @staticmethod
    def prebuild_banks(venues: Sequence[str], weather: Optional[dict] = None,
                       workers: Optional[int] = None) -> None:
        """Fill the bank cache for many parks at once, across processes.

        Embarrassingly parallel — each park is an independent couple of thousand
        ODE solves — and it is the entire cost of a calibration: ~15 minutes
        serially against about one across a real core count. Cached parks are
        skipped before the pool is created, so this is RESUMABLE.
        """

        todo = [v for v in venues
                if not (BANK_DIR / f"{BallFlight._bank_key(weather, v)}.pkl.gz").exists()]
        have = len(venues) - len(todo)
        if not todo:
            print(f"[banks] all {len(venues)} cached")
            return
        workers = workers or max(1, min(len(todo), (os.cpu_count() or 4) - 2))
        print(f"[banks] {have} cached, building {len(todo)} on {workers} workers")
        with multiprocessing.Pool(workers) as pool:
            for i, venue in enumerate(
                    pool.imap_unordered(_bank_worker,
                                        [(v, weather) for v in todo]), 1):
                print(f"[banks]   {i}/{len(todo)} {venue}", flush=True)

    @staticmethod
    def cached_trajectory_bank(weather: Optional[dict] = None,
                               venue: Optional[str] = None,
                               cd: Optional[float] = None) -> Dict[tuple, list]:
        """`trajectory_bank`, persisted to disk.

        Building a bank is ~2,300 ODE solves — about 30 seconds a park, and 17
        minutes for a full 30-park calibration. Nothing in a bank depends on the
        park's fences or on `distance_scale`, only on the launch conditions and
        the air, so a bank stays valid until the weather actually moves. Without
        this the module is fine for a one-off fit and useless on a live slate.
        """

        BANK_DIR.mkdir(parents=True, exist_ok=True)
        path = BANK_DIR / f"{BallFlight._bank_key(weather, venue)}.pkl.gz"
        if path.exists():
            try:
                with gzip.open(path, "rb") as fh:
                    return pickle.load(fh)
            except Exception:
                path.unlink(missing_ok=True)      # corrupt cache, rebuild
        with _quiet_solves():
            bank = BallFlight.trajectory_bank(weather, venue, cd)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wb") as fh:
            pickle.dump(bank, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)                          # atomic: no half-written bank
        return bank

    @staticmethod
    def trajectory_bank(weather: Optional[dict] = None,
                        venue: Optional[str] = None,
                        cd: Optional[float] = None) -> Dict[tuple, list]:
        """{(hla, la, ev): [(horizontal_ft, height_ft), ...]} for one weather.

        The expensive object, and the reason this module is usable at all. A
        trajectory depends on the launch conditions and the AIR — not on the park
        and not on the distance calibration — so ONE bank of ~2,300 solves serves
        every park, and re-fitting `distance_scale` costs nothing. The naive way
        (solve per park per scale) is ~176,000 solves. Altitude is the exception:
        it changes the air, so pass `venue` to bake it in.
        """
        weather = weather or {}
        sim = BallFlight._sim()
        cd = BallFlight._cd_neutral() if cd is None else cd

        temp = float(weather.get("temp_f", NEUTRAL_TEMP_F))
        hum = float(weather.get("humidity", NEUTRAL_HUMIDITY))
        wind = float(weather.get("wind_mph", NEUTRAL_WIND_MPH))
        wdir = float(weather.get("wind_dir_deg", 0.0))
        alt = float(weather.get("altitude_ft",
                                (_park(venue) or {}).get("altitude", 0.0)
                                if venue else NEUTRAL_ALTITUDE_FT))
        press = weather.get("pressure_pa")
        station = bool(weather.get("pressure_is_station", False))
        azimuth = (park_azimuth(venue)
                   if venue and weather.get("wind_frame") == "compass" else None)

        bank: Dict[tuple, list] = {}
        for hla in GRID_HLA:
            for la in GRID_LA:
                for ev in GRID_EV:
                    t = sim.calculate_trajectory(
                        ev, la, hla, wind, wdir, temp, hum, alt,
                        pressure_pa=press, cd_override=cd,
                        park_azimuth=azimuth, pressure_is_station=station)
                    bank[(hla, la, ev)] = [
                        (math.hypot(float(x), float(z)), float(y))
                        for x, y, z in zip(t["x"], t["y"], t["z"])]
        return bank

    @staticmethod
    def fence_grid_from_bank(bank: Dict[tuple, list], venue: Optional[str],
                             scale: float = 1.0
                             ) -> Dict[Tuple[int, int], float]:
        """Minimum clearing EV per cell, read off a prebuilt bank. No ODE solves."""
        grid: Dict[Tuple[int, int], float] = {}
        for hla in GRID_HLA:
            polar = hla_to_polar(hla)
            if venue:
                wall_d, wall_h = wall_at(venue, polar)
            else:
                wall_d, wall_h = NEUTRAL_WALL_DIST(polar), NEUTRAL_WALL_HEIGHT
            for la in GRID_LA:
                thresh = float("inf")
                for ev in GRID_EV:
                    if _clears_profile(bank[(hla, la, ev)], wall_d, wall_h, scale):
                        thresh = float(ev)
                        break
                grid[(hla, la)] = thresh
        return grid

    @staticmethod
    def _lookup(grid: Dict[Tuple[int, int], float], hla: float, la: float) -> float:
        h = min(GRID_HLA, key=lambda g: abs(g - hla))
        l = min(GRID_LA, key=lambda g: abs(g - la))
        return grid.get((h, l), float("inf"))

    @staticmethod
    def arm_factor(of_arm: Optional[float]) -> float:
        """Odds multiplier on a runner taking the extra base, from OF arm."""
        if not of_arm:
            return 1.0
        return math.exp(-ARM_ODDS_PER_MPH * (of_arm - ARM_MEAN_MPH))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def hla_to_polar(hla_deg: float) -> float:
    """Physics horizontal launch angle -> stadium polar angle.

    Physics: 0 = dead centre, +45 = right-field line, -45 = left-field line.
    Stadium polar (weatherman): 0 = RF line, 45 = centre, 90 = LF line.
    So polar = 45 - hla. Getting this backwards puts every pulled ball in the
    opposite corner of the park, which is the single easiest way to make this
    module look plausible and be wrong.
    """
    return 45.0 - hla_deg


def spray_to_hla(hc_x: float, hc_y: float) -> Optional[float]:
    """Savant spray pixel coords -> physics horizontal launch angle."""
    dx = hc_x - 125.42
    dy = 198.27 - hc_y
    if dy <= 0:
        return None
    return math.degrees(math.atan2(dx, dy))


def _park(venue: str) -> Optional[dict]:
    """Stadium record, by any of the park's names.

    **Resolves internally**, like `park_run_factor` and `weather_tilt` — the
    file's convention is that a park accessor takes whatever spelling the
    caller has. StatsAPI renames parks between seasons ("Rate Field",
    "Daikin Park", "UNIQLO Field at Dodger Stadium") and an exact-match
    `.get` on the raw name returned None, which reads downstream as altitude
    0 and no azimuth rather than as an error.
    """
    return weatherman.STADIUM_DATA.get(resolve_venue(venue or "") or venue)


def park_azimuth(venue: str) -> Optional[float]:
    """Home-plate -> centre-field compass bearing. Resolves internally.

    A miss here does not raise — it returns None, and `weather_tilt` then
    drops the wind term entirely while the ball-flight grid falls back to an
    unrotated bearing. CLAUDE.md records what that costs: every park behaving
    as though centre field pointed due north, 419-421 ft everywhere against a
    102 ft real spread.
    """
    rec = weatherman.PARK_ORIENTATION.get(resolve_venue(venue or "") or venue)
    if isinstance(rec, dict):
        return rec.get("azimuth")
    return rec


def wall_at(venue: str, polar_deg: float) -> Tuple[float, float]:
    wm = weatherman
    p = max(0.0, min(90.0, polar_deg))
    d = wm.get_stadium_wall_distance(venue, p)
    h = wm.get_stadium_wall_height(venue, p)
    if d is None or h is None:
        # Unknown park names used to return None and die ~100 frames later
        # inside the trajectory solve as a TypeError on a float/None compare.
        raise ValueError(
            f"mlb_sim: no wall geometry for venue {venue!r}. "
            f"Known parks: {', '.join(sorted(wm.STADIUM_DATA))}")
    return d, h


# ---------------------------------------------------------------------------
# The fence grid
# ---------------------------------------------------------------------------
# The ODE is far too slow to run per batted ball. Solve once per (park,
# weather) for the MINIMUM exit velocity that clears the fence at each (spray,
# launch angle) cell; every batted ball is then a table lookup.

GRID_HLA = tuple(range(-45, 46, 6))      # physics convention
GRID_LA = tuple(range(12, 45, 4))
GRID_EV = tuple(range(84, 123, 2))       # mph, ascending


BANK_DIR = DATA_DIR / "trajectory_banks"


class _quiet_solves:
    """Swallow stdout from the flight simulator while solving a bank.

    `calculate_trajectory` prints its high-altitude pressure diagnostic on EVERY
    call — ~2,300 identical lines per Coors bank, and across 22 workers sharing
    one stdout that is contention. The number itself is correct (verified: Coors
    at 0.825x sea-level density against ISA's ~0.83). stderr is deliberately left
    alone, so a real failure still surfaces.
    """

    def __enter__(self):
        self._old = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *exc):
        sys.stdout = self._old
        return False


def _bank_worker(args) -> str:
    """Build and persist one park's bank. MUST stay at module level.

    `multiprocessing` pickles the callable by qualified name, so a closure or
    a nested function cannot be a worker — the same constraint the offline
    tools in `homerunwidget.py` document for `_sim`/`_phys_with_cd`. It also
    must not touch pandas: the worker only sees a venue and a weather dict.
    """
    venue, weather = args
    with _quiet_solves():
        BallFlight.cached_trajectory_bank(weather, venue=venue)
    return venue


def _clears_profile(profile, wall_d: float, wall_h: float,
                    scale: float) -> bool:
    prev_r = prev_y = None
    for r0, y in profile:
        r = r0 * scale
        if prev_r is not None and prev_r <= wall_d <= r:
            span = r - prev_r
            f = (wall_d - prev_r) / span if span > 0 else 0.0
            return (prev_y + f * (y - prev_y)) > wall_h
        if y <= 0 and prev_r is not None:
            return False
        prev_r, prev_y = r, y
    return False


# A neutral reference park: symmetric, league-median dimensions. Used as the
# denominator of every multiplier so the numbers mean "relative to an average
# yard" rather than "relative to whichever park happened to be first".
NEUTRAL_WALL_HEIGHT = 10.4      # measured mean across the 30 parks (was 8.0)


def NEUTRAL_WALL_DIST(polar_deg: float) -> float:
    """League-mean MLB wall distance by polar angle.

    Fitted to the measured means across all 30 parks: 330 down the lines,
    400 to centre, **370 in the gaps**. The exponent matters more than it
    looks — a plain quadratic (`400 - 70x^2`) hits the lines and centre
    correctly but bulges to 382.5 in the gaps, 12.5 ft deeper than any real
    park, and the gaps are exactly where home runs go. That alone made this
    reference yard a 0.0294 HR/BB park against a real-park mean of 0.0414,
    inflating every park multiplier by 1.41x.
    """
    x = abs(polar_deg - 45.0) / 45.0     # 0 at centre, 1 at either line
    return 400.0 - 70.0 * (x ** 1.22)


def hr_rate(bbe: Sequence[dict], grid: Dict[Tuple[int, int], float]) -> float:
    """Fraction of a hitter's batted balls that clear, given a fence grid.

    Denominator is ALL his batted balls, not just the air balls, so the result
    is directly comparable to a home-run-per-batted-ball rate.
    """
    if not bbe:
        return 0.0
    hits = 0
    for b in bbe:
        ev, la, hla = b.get("ev"), b.get("la"), b.get("hla")
        if ev is None or la is None or hla is None:
            continue
        if not (LA_MIN <= la <= LA_MAX) or ev < EV_MIN:
            continue
        if ev >= BallFlight._lookup(grid, hla, la):
            hits += 1
    return hits / len(bbe)


# Fraction of balls in play converted per point of team OAA, per game.
#
# **Sized from the OAA definition, not fitted.** The league spread is ~0.88
# outs a game between the extremes at ~0.75 runs an out, so the true swing is
# about 0.5 runs/game; 0.00022 gave 0.75, ~50% hot. EffortMLB's own ~0.2 runs
# per START is a weaker, noisier instrument and is not in conflict. A.10.
OAA_TO_BIP_SHIFT = 0.00015

# Outfield arm suppresses the extra base. League mean 87.7 mph, sd 1.93; the
# effect is on the RUNNER's advance odds, so it is expressed as an odds
# multiplier per mph above or below average.
ARM_MEAN_MPH = 87.66
ARM_ODDS_PER_MPH = 0.045


def apply_defense(rates: Sequence[float], oaa: float) -> List[float]:
    """Shift balls in play toward outs for a good defence, and away for a bad
    one. Strikeouts and walks are untouched — no fielder is involved."""
    if not oaa:
        return list(rates)
    out = list(rates)
    hits = (S1B, S2B, S3B)
    outs = (GB_OUT, AIR_OUT)
    h = sum(out[i] for i in hits)
    o = sum(out[i] for i in outs)
    if h <= 0 or o <= 0:
        return out
    delta = min(max(oaa * OAA_TO_BIP_SHIFT, -h * 0.5), h * 0.5)
    for i in hits:
        out[i] -= delta * (out[i] / h)
    for i in outs:
        out[i] += delta * (out[i] / o)
    return out


# --- catcher framing -------------------------------------------------------
# **Framing is NOT the umpire, and the difference decides where it belongs.** A
# tight zone is shared by both teams and largely cancels for a side bet; a
# CATCHER belongs to one club, so his framing suppresses only the OPPONENT's
# offence and therefore prices totals, run lines and moneylines. Measured at
# 0.216 runs a game best-to-worst, ~42% of the OAA spread. Zero-sum across the
# league, so it CANNOT move league run scoring. sim_state.md A.10.
FRAMING_RUNS_PER_GAME_SD = 0.043      # 5.28 runs / 122 games

# How the run value is delivered: the share of an extra called strike taken on
# the K side, the rest on BB. It does not affect the RUN value, which is
# calibrated as a total, but it sets the K and BB props.
#
# **MEASURED — and 0.5 was wrong for a reason worth keeping.** It splits a
# MULTIPLIER, so it divides the two RELATIVE moves; the absolute move is
# near-symmetric, but walks are a quarter as common. **5.6c pointed at the wrong
# data**: Savant's `rv_11`..`rv_19` are run value by ZONE, not by COUNT. A.10.
FRAMING_K_SHARE = _MEASURED_RUN.get("framing_k_share", 0.5)

# Runs per unit of the framing tilt, MEASURED the way `RUNS_PER_TILT` is, on
# league-average clones through `simulate_game`'s own context path. Unscaled the
# K/BB tilt runs 1.564x too strong, so a good framing club would be credited
# with half again the runs it saves. A.10.
FRAMING_TILT_SCALE = 0.6394
# The shipped value, captured once. `ab_configure` ablates framing by setting
# `FRAMING_TILT_SCALE = 0.0`, and needs a way back that does NOT route through
# `_ab_shipped_defaults` — see the note there.
FRAMING_TILT_SHIPPED = 0.6394


def framing_multipliers(runs_per_game: float) -> Dict[int, float]:
    """Outcome multipliers for facing a club whose catchers frame this well.

    Positive `runs_per_game` means the catcher SAVES runs, so the opposing
    offence should score less: strikeouts up, walks down. Mass conservation is
    left to `apply_multipliers`, which renormalises.
    """
    if not runs_per_game:
        return {}
    # per-PA run value -> outcome shift, on the same scale the engine measures
    # every other tilt on.
    u = runs_per_game * FRAMING_TILT_SCALE
    return {K: 1.0 + u * FRAMING_K_SHARE,
            BB: 1.0 - u * (1.0 - FRAMING_K_SHARE)}


# Home-field advantage, as a symmetric tilt on offence.
#
# **The sim's structure supplies almost none of it.** Batting last plus the
# ghost runner give identical teams a 0.5014 home win rate against a real
# 0.5264, so without an explicit term the model was 2.5 points short on EVERY
# game — which is why its moneylines skewed to the underdog across the board.
# Calibrated 2026-08-15 so identical teams reproduce the real rate
# (`calibrate_hfa`). Applied to OFFENCE for simplicity: only the net effect on
# run scoring is identifiable from a win rate. Symmetric, so league scoring is
# unchanged. sim_state.md A.10.
HFA = 0.018

# Where the tilt comes from and goes to.
_HFA_UP = (S1B, S2B, S3B, HR)
_HFA_DOWN = (K, GB_OUT, AIR_OUT)


def offence_tilt(rates: Sequence[float], s: float) -> List[float]:
    """Move `s` of the on-base mass between outs and hits, conserving total.

    The shared primitive under home-field advantage and the game-level form
    draw. Positive `s` lifts hits at the expense of strikeouts and outs in
    play; negative does the reverse. Mass-conserving, so it never invents or
    destroys plate appearances.
    """
    if not s:
        return list(rates)
    out = list(rates)
    up = sum(out[i] for i in _HFA_UP)
    dn = sum(out[i] for i in _HFA_DOWN)
    if up <= 0 or dn <= 0:
        return out
    delta = up * s
    delta = max(min(delta, dn * 0.5), -up * 0.9)
    for i in _HFA_UP:
        out[i] += delta * (out[i] / up)
    for i in _HFA_DOWN:
        out[i] -= delta * (out[i] / dn)
    return out


def apply_hfa(rates: Sequence[float], home: bool,
              hfa: Optional[float] = None) -> List[float]:
    """Tilt one side's offence for home-field advantage, conserving mass."""
    h = HFA if hfa is None else hfa
    return offence_tilt(rates, h if home else -h) if h else list(rates)


# ---------------------------------------------------------------------------
# Game-level form — the per-team-game noise the engine was missing
# ---------------------------------------------------------------------------
# Season rates are FLAT; real baseball has a large per-team-game shared factor
# no forecast can see, and its absence is the bulk of the run-distribution
# deficit. **Shape all measured, not chosen**: TEAM-GAME rather than game (the
# two sides correlate -0.053), and OFFENCE-side and game-long rather than
# per-pitcher — the starter's own window is NEGATIVELY correlated beyond the
# game factor, so a per-starter draw is argued AGAINST by the data. A.10.
GAME_FORM_SD = 0.1134

# Runs are a CONVEX function of offensive rate, so a symmetric tilt raises the
# mean (Jensen); the draw is recentred by this much. Leaving it at 0 reintroduces
# the section-10 bug class: right in shape, wrong in level.
#
# **It scales with sd^2, so it MUST be refitted whenever `GAME_FORM_SD` moves** —
# raising the sd 5% and leaving this alone put the mean 0.13 runs high, which a
# test caught. The two also cannot be solved in sequence, because the shift
# lowers the run level the sd was fitted against. sim_state.md A.10.
GAME_FORM_MEAN_SHIFT = 0.0065


def draw_form(rng: random.Random, sd: Optional[float] = None) -> float:
    """One team-game's offensive form, centred so it does not move the mean."""
    s = GAME_FORM_SD if sd is None else sd
    return rng.gauss(-GAME_FORM_MEAN_SHIFT, s) if s else 0.0


# ---------------------------------------------------------------------------
# Weather — a DETERMINISTIC shift on the same axis as the form draw
# ---------------------------------------------------------------------------
# Fitted against ACTUAL runs, WITHIN park, on 1,840 games of 2026:
#
#   temperature       +0.0317 runs/degF   t 3.03-3.27
#   wind out to CF    +0.0618 runs/mph    t 3.10
#   wind SPEED alone  +0.0240 runs/mph    t 0.75   <- null, and that matters
#
# The last line is the check that this is physics and not a fit. **Deliberately
# NOT built on the trajectory-bank pipeline** — that is the machinery behind the
# park term that measured worse than nothing — and **centred on the PARK's own
# mean conditions**, because the coefficients came from a within-park fit. A.10.
WEATHER_TEMP_RUNS_PER_F = 0.0317
WEATHER_WIND_OUT_RUNS_PER_MPH = 0.0618

# Runs per unit of `offence_tilt`, applied to BOTH sides. MEASURED: league
# clones give game totals 8.0002 / 8.5732 / 8.8950 at tilt -0.03 / 0 / +0.03,
# so the local slope is 14.9 runs per unit.
RUNS_PER_TILT = 14.9

# --- TEAM QUALITY: the one thing a bottom-up engine cannot say ------------
# **Measured 2026-08-22**: in ordinary games the bottom-up build carries three
# quarters of club quality, but in the heavy-favourite bucket its loading FALLS
# to 0.142 while reality's RISES to 0.570 — **and it absorbs the market**, which
# collapses to t +1.06 when both are added.
#
# **Level-neutral by construction** (league differential sums to zero) and
# **subset-targeted by construction** (it scales with the club's own
# differential) — the two properties all three amplitude levers in 4e lacked.
# The gain is the RESIDUAL loading, not the whole. sim_state.md A.10, 4h.
TEAM_QUALITY_GAIN = 0.089
# Run differential over few games is mostly noise; shrink toward zero by games
# played. 30 is a third of a season and is not fitted — it is a guard, and the
# arm should be re-run at a couple of values before anything ships.
TEAM_QUALITY_SHRINK_G = 30.0


class TeamQuality:
    """The one thing a bottom-up engine cannot say: club quality."""

    @staticmethod
    def team_quality_tilt(rd_per_game: float) -> float:
        """Club run differential per game -> `offence_tilt` units."""
        if not TEAM_QUALITY_GAIN or not rd_per_game:
            return 0.0
        per_team = RUNS_PER_TILT / 2.0
        return TEAM_QUALITY_GAIN * rd_per_game / per_team

    @staticmethod
    def shrink_team_quality(run_diff: float, games: int) -> float:
        """Season-to-date run differential per game, shrunk toward league (zero)."""
        if games <= 0:
            return 0.0
        return (run_diff / games) * (games / (games + TEAM_QUALITY_SHRINK_G))


# Roof-closed games are a different regime: no wind at all, and the reported
# temperature is a thermostat rather than the weather.
ROOF_CLOSED_CONDITIONS = {"roof closed", "dome"}

# Ceiling on the weather tilt, in tilt units (~+-1.5 runs a game).
WEATHER_TILT_CLAMP = 0.10

# --- AIR DENSITY: temperature, pressure and humidity as ONE term -----------
# **MEASURED on 7,510 open-air games, within park.** Drag and Magnus are both
# proportional to density, so one density term beats three collinear ones:
# -0.1562 runs per 1% of density, pooled t -6.82, replicating at |t| > 3 in
# EVERY season. **Not temp + pressure separately, even though it fits better** —
# per sd the pressure effect is 2.7x too strong to be a density channel, so it
# proxies synoptic weather, and buying R2 with a coefficient the mechanism
# cannot support is the exact failure §10 records. A.10.
WEATHER_DENSITY_RUNS_PER_PCT = -0.1562
# Off until the closing-line A/B says otherwise, like every other term here.
USE_AIR_DENSITY = False

# Field-relative wind labels -> the component blowing OUT toward centre field.
# StatsAPI's label is ALREADY park-relative ("Out To CF"), not a compass
# bearing, so it needs no azimuth rotation. Do not confuse this with a feed
# bearing, which does (see CLAUDE.md on wind frames).
# **A crosswind is a MEASUREMENT of zero; "Varies" is the ABSENCE of one, and
# mapping both to 0.0 charged the second as if it were the first.** The term is
# `(out - the park's reference out)`, so at a park whose reference blows out,
# "the wind varies" reads as "the wind is blowing IN tonight" — then multiplied
# by that park's wind factor. On BAL @ ATH 2026-08-29 the label went "Out To CF"
# -> "Varies" 40 minutes before first pitch and the weather term swung -0.87
# runs, at Sutter, whose factor is 2.296, the highest of the thirty.
#
# `wind_out_component` already returns None for an unknown label and
# `weather_tilt` DROPS the wind term on None, which is the honest handling of a
# direction nobody measured. So the fix is to stop claiming these three are
# measurements. "l to r"/"r to l" stay at 0.0 — a crosswind really does put no
# air behind the ball, and that is data.
WIND_OUT_COMPONENT = {
    "out to cf": 1.0, "out to rf": 0.707, "out to lf": 0.707,
    "in from cf": -1.0, "in from rf": -0.707, "in from lf": -0.707,
    "l to r": 0.0, "r to l": 0.0,
    # "varies" / "none" / "calm" deliberately ABSENT -> None -> term dropped.
}

# Per-park WIND RECEPTIVITY — how much of a given wind actually reaches the
# ball. Fitted on batted-ball DISTANCE, and a huge lever: Wrigley 0.188 against
# Dodger Stadium 0.040, a 4.7x ratio around a league mean of 0.091.
#
# **Validated on RUNS before being used, because it was fitted on DISTANCE and
# the transfer is not automatic** — scaling by receptivity takes the wind term
# from t 3.10 to t 3.85, and fitting the flat slope separately by tier gives
# 1.65x in the predicted direction from data that never saw the distance fit.
# NOT the machinery that killed the park HR term. sim_state.md A.10.
RECEPTIVITY_PATH = DATA_DIR / "wind_receptivity.json"
PARK_WIND_FACTOR_CLAMP = (0.25, 2.50)

_PARK_WIND: Optional[Dict[str, float]] = None


def park_wind_factor(venue: Optional[str]) -> float:
    """This park's wind sensitivity relative to the league mean. 1.0 unknown."""
    global _PARK_WIND
    if _PARK_WIND is None:
        _PARK_WIND = {}
        try:
            with open(RECEPTIVITY_PATH) as fh:
                raw = json.load(fh)
            _glob = (raw.pop("_global", None) or {})
            vals = {k: v["wind_mult"] for k, v in raw.items()
                    if isinstance(v, dict) and v.get("wind_mult")}
            if vals:
                # **The normaliser is OPEN-AIR parks only.** A park's
                # `wind_mult` is fitted from how its batted balls answer the
                # recorded OUTDOOR wind, and under a shut roof they do not — so
                # the fit there measures ROOF USAGE. All five retractable parks
                # come back with a NEGATIVE response, which is impossible.
                #
                # **Divide by the scale the fit SHRANK TOWARD**, which the file
                # records and this function was throwing away: correlating the
                # implied shrink weight against n gives +0.98 for
                # `wind_mult_2pass` and -0.17 for the mean of the shrunk values.
                # The open-air fallback is the same argument by a second route
                # and the two agree to 0.4%. sim_state.md A.10.
                mean = float(_glob.get("wind_mult_2pass") or 0.0)
                if mean <= 0.0:
                    _roofs = weatherman.STADIUM_DATA
                    _open = [x for kk, x in vals.items()
                             if str((_roofs.get(resolve_venue(kk) or kk)
                                     or {}).get("roof") or "").lower() == "open"]
                    mean = (sum(_open) / len(_open) if len(_open) >= 10
                            else sum(vals.values()) / len(vals))
                lo, hi = PARK_WIND_FACTOR_CLAMP
                for k, v in vals.items():
                    _PARK_WIND[resolve_venue(k) or k] = max(lo, min(hi, v / mean))
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            _PARK_WIND = {}
    if not venue:
        return 1.0
    return _PARK_WIND.get(resolve_venue(venue) or venue, 1.0)


# ---------------------------------------------------------------------------
# Park RUN factor — empirical, and NOT the term removed in section 6
# ---------------------------------------------------------------------------
# §6 removed a park HOME-RUN multiplier extrapolated from fence geometry. **This
# is a different quantity**: the OBSERVED home/road run ratio, i.e. what
# actually happened there. Removing the physics term left NO park effect at all,
# which is fine on average and badly wrong at Sutter Health Park (1.513), where
# the sim was projecting ~1.5 runs under the market.
#
# **Validated OUT OF SAMPLE**, leave-one-game-out: corr +0.14 with the actual
# total against a whole-model +0.17, slope 4.63, t 6.10. `PARK_RUN_RELIABILITY`
# is SOLVED, not chosen, and **centred** — the home club's rates already carry
# this park for half its games. sim_state.md A.10.
PARK_RUN_PATH_FMT = "park_run_factors_{season}.json"
PARK_RUN_RELIABILITY = 0.699
PARK_HOME_GAME_SHARE = 0.5
PARK_RUN_CLAMP = (0.80, 1.40)

_PARK_RUN: Dict[tuple, Dict[str, float]] = {}     # keyed on (season, reliability)


def build_park_run_factors(season: Optional[int] = None, save_dir: Path = SAVE_DIR,
                           refresh: bool = False) -> Path:
    """Home/road runs-per-game factor per park, off that season's linescores.

    Computed in-repo rather than fetched, which is what makes a leak-free version
    possible at all — unlike Savant's boards, this can be built from a season
    that finished before the games being priced. **A park's raw runs per game is
    NOT a park factor**: it is mostly the two clubs who play there, and PNC read
    10.57 runs/game while being one of the most pitcher-friendly yards.
    """
    season = CURRENT_SEASON if season is None else int(season)
    path = Path(save_dir) / PARK_RUN_PATH_FMT.format(season=season)
    if path.exists() and not refresh:
        return path
    slate = season_slate(season, save_dir=save_dir)
    if not slate:
        raise RuntimeError(f"mlb_sim: no {season} slate to build park factors")
    home: Dict[str, List[float]] = {}
    road: Dict[str, List[float]] = {}
    venue_of: Dict[str, str] = {}
    for r in slate:
        tot = float(sum(r["home_innings"]) + sum(r["away_innings"]))
        v = resolve_venue(r["venue"]) or r["venue"]
        if not v:
            continue
        home.setdefault(v, []).append(tot)
        venue_of.setdefault(v, r["home"])
        # the same club's ROAD games are the control for its own quality
        road.setdefault(r["away"], []).append(tot)
    by_club_road = road
    out: Dict[str, dict] = {}
    for v, tots in home.items():
        club = venue_of.get(v)
        rd = [t for r in slate if r["away"] == club
              for t in (float(sum(r["home_innings"]) + sum(r["away_innings"])),)]
        if not club or len(tots) < 20 or len(rd) < 20:
            continue
        h_rpg = statistics.mean(tots)
        r_rpg = statistics.mean(rd)
        out[v] = {"club": club, "home_g": len(tots),
                  "home_rpg": round(h_rpg, 3), "road_g": len(rd),
                  "road_rpg": round(r_rpg, 3),
                  "raw": round(h_rpg / r_rpg, 4) if r_rpg else 1.0}
    del by_club_road
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"[park] {season}: {len(out)} parks -> {path}")
    return path


# **A shrinkage weight is only valid for the PREDICTOR it was solved on.** 0.699
# was solved leave-one-game-out WITHIN a season, but the leak-free backtest reads
# the PRIOR season's, so it made the park term 2-4x too strong in every §3d
# number. **The LIVE path was always fine**; only the backtest was wrong, and in
# the direction of making the model look worse.
#
# **ONE SEASON of park factor is ~50% sampling noise, so AVERAGE** — arithmetic,
# not a fit: the two-season mean carries 35% more signal at a sqrt(2) lower sd.
# **Shipped at 3, scored against the CLOSING TOTAL**, ~7x the instrument scoring
# against results is. **Known limitation:** a park that physically CHANGED is
# averaged across the change. sim_state.md A.10.
PARK_RUN_WINDOW = 3           # SHIPPED 2026-08-17. Savant publishes 3-year
                              # rolling for the same reason. Needs park factors
                              # back to `season - lag - 2`.

# Persistence of the windowed factor into the next season, measured. **NOT used
# to attenuate the reliability** — that was a double count, see
# `park_run_reliability`. Kept because it is the right way to compare WINDOWS to
# each other: a wider window persists better, which is the case for widening it.
PARK_RUN_PERSISTENCE_BY_WINDOW: Dict[int, float] = {
    1: 0.30,      # 2024->2025 (0.265), 2025->2026 (0.342)
    2: 0.43,      # (2023,24)->2025 (0.406), (2024,25)->2026 (0.463)
    3: 0.60,      # (2022-24)->2025 (0.611), (2023-25)->2026 (0.597)
    4: 0.61,      # splits between targets (0.667 / 0.553) — do not prefer it
}


def park_run_reliability() -> float:
    """How far to trust the park factor. **One value at every lag — the
    attenuation this function used to apply was a DOUBLE COUNT.**

    `PARK_RUN_RELIABILITY` had ALREADY been solved for this predictor, and a
    persistence slope IS a reliability (`cov(y1,y2)/var(y1)`), so multiplying them
    shrinks the noise out twice. **Caught by the closing total**, the only
    instrument that could see it: against realised totals the attenuated version
    looked BETTER on slope, because shrinking any over-dispersed predictor
    improves calibration while destroying correlation. **Never judge a shrink by
    its slope alone.** sim_state.md A.10.
    """
    return PARK_RUN_RELIABILITY


def park_run_window() -> int:
    """Seasons to average. **The window applies at every lag, including 0.**

    It used to apply only to a LAGGED factor, on the reasoning that at lag 0 the
    current season IS the answer. §8 already contained the refutation: a single
    season is mostly NOISE either way, so the window is arithmetic, not a trade
    against staleness. No new leak — it reaches BACK from `season`.

    **What it cost, and why nothing caught it**: `TEAM_CONTEXT_LAG` is 1 in
    `ab_configure` and 0 everywhere else, so every A/B used a 3-season window
    while the LIVE path used one. It is a re-ranking, not a level shift, so no
    aggregate could see it. **OPEN**: `PARK_RUN_RELIABILITY` was solved for a
    window-1 factor, so the live park term is now slightly UNDER-weighted — do
    not read it as calibrated until it is re-solved. sim_state.md A.10.
    """
    return max(1, PARK_RUN_WINDOW)


def park_run_factor(venue: Optional[str], season: Optional[int] = None,
                    save_dir: Path = SAVE_DIR) -> float:
    """Regressed home/road run factor for a park. 1.0 when unknown."""
    season = CURRENT_SEASON if season is None else int(season)
    # Keyed on SEASON **and the applied reliability**, so a lagged build does
    # not get served the contemporaneous numbers out of a stale memo. Keying on
    # season alone was enough while the reliability was a single constant; now
    # that it moves with the lag, it is not — and the failure would be silent.
    rel = park_run_reliability()
    win = park_run_window()
    key = (int(season), round(rel, 6), win)
    if key not in _PARK_RUN:
        _PARK_RUN[key] = {}
        # `season` is the NEWEST season in the window, so the window reaches
        # BACK from it — never forward, which would be the leak this whole
        # lagged path exists to avoid.
        acc: Dict[str, List[float]] = {}
        for yr in range(int(season) - win + 1, int(season) + 1):
            try:
                with open(save_dir /
                          PARK_RUN_PATH_FMT.format(season=yr)) as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                continue
            for k, v in raw.items():
                try:
                    # **Weighted by the games behind it.** A plain `mean` over
                    # the window counts a 12-game April sample as heavily as a
                    # finished 81-game season; the count is in the file and was
                    # never read. It bites hardest in April — Sutter Health Park
                    # has 81 games in 2025 against 61 in 2026 and the two
                    # disagree 1.1031 to 1.5132.
                    g = float(v.get("home_g") or 0.0)
                    acc.setdefault(resolve_venue(k) or k, []).append(
                        (float(v["raw"]), g if g > 0 else 1.0))
                except (KeyError, TypeError, ValueError):
                    continue
        lo, hi = PARK_RUN_CLAMP
        for name, vals in acc.items():
            # **A park present in ONE season of the window is not averaged with
            # nothing** — it keeps its own single-season factor, which is the
            # old behaviour rather than a hole. New and relocated parks land
            # here (Sutter Health, and Camden after the 2025 wall move), and
            # they are exactly the parks where a stale average would be wrong.
            wt = sum(g for _, g in vals)
            mean = (sum(x * g for x, g in vals) / wt if wt else
                    statistics.mean([x for x, _ in vals]))
            pf = 1.0 + (mean - 1.0) * rel
            _PARK_RUN[key][name] = max(lo, min(hi, pf))
    if not venue:
        return 1.0
    return _PARK_RUN[key].get(resolve_venue(venue) or venue, 1.0)


# ===========================================================================
# PARK DE-CONTAMINATION OF A PLAYER'S OWN RATES
# ===========================================================================
# **The rate layer estimates TALENT but is fed talent-plus-context**: a board row
# carries the park the player played in, on RAW counts, so the shipped chain
# shrinks that park away proportionally and then adds TONIGHT's at the game
# level. `park_run_tilt` divides the HOME side's exposure out; nothing corrects
# the VISITOR's hitters or EITHER pitcher, because an offence tilt cannot reach
# an arm.
#
# **Per OUTCOME, not per run** — Citizens Bank reads 1.181 on runs and 1.049 on
# home runs, so one run number injects a double-digit error into the column that
# matters most. sim_state.md A.10, 4e.

PARK_OUTCOME_WINDOW = PARK_RUN_WINDOW
USE_PARK_DECONTAM = True         # LIVE 2026-08-21. Level-neutral on the
                                 # slate (mean dTOTAL -0.066 runs, sd 0.185)
                                 # and +0.9/+1.2 pts on the two heavy
                                 # favourites measured. NOT yet scored on
                                 # the closing total across seasons.

_PARK_OUTCOME: Dict[tuple, Dict[str, List[float]]] = {}   # keyed on (season,)
_CLUB_PARK: Dict[int, Dict[str, str]] = {}                # season -> club->park


def park_outcome_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / f"park_outcome_factors_{season}.json"


def _park_outcome_table(season: int,
                        save_dir: Path = SAVE_DIR) -> Dict[str, List[float]]:
    """One season's per-outcome factors, memoised ON THE SEASON.

    `_FRAMING`, `_DEF` and `_PARK_WX_REF` were each memoised on a bare global
    and each served one season's numbers for every season asked for — three
    separate instances in this file. The key is the fix.
    """
    key = (season, str(save_dir))
    got = _PARK_OUTCOME.get(key)
    if got is None:
        p = park_outcome_path(season, save_dir)
        if not p.exists():
            got = {}
        else:
            with open(p) as fh:
                # **Keys RESOLVED, because the file stores raw StatsAPI venue
                # names and those drift between seasons** — Minute Maid ->
                # Daikin, Guaranteed Rate -> Rate Field, Dodger Stadium ->
                # "UNIQLO Field at Dodger Stadium". `measured_park_exposure`
                # looked up with an exact `.get(venue)` across a 3-season window,
                # so a rename silently collapsed it: Dodger Stadium found ONE
                # season of three, with no error. 126 player-shares affected.
                got = {(resolve_venue(k) or k): v["factor"]
                       for k, v in json.load(fh).items()}
        _PARK_OUTCOME[key] = got
    return got


class ParkFactors:
    """Per-outcome park factors, player exposure, and de-contamination."""

    @staticmethod
    def club_home_park(club: str, season: int,
                       save_dir: Path = SAVE_DIR) -> Optional[str]:
        """Which park a club calls home that season."""
        got = _CLUB_PARK.get(season)
        if got is None:
            p = Path(save_dir) / f"park_run_factors_{season}.json"
            got = {}
            if p.exists():
                with open(p) as fh:
                    for venue, row in json.load(fh).items():
                        c = row.get("club")
                        if c:
                            got[normalize_club(c)] = venue
            _CLUB_PARK[season] = got
        return got.get(normalize_club(club)) if club else None

    @staticmethod
    def build_park_outcome_factors(season: int, reliability: float = 0.70,
                                   save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
        """Per-OUTCOME park factors. `park_run_factor` is a RUN factor.

        Park affects home runs far more than strikeouts, so de-contaminating a
        nine-outcome vector with one run number injects error into every column
        it does not fit — Citizens Bank reads 1.181 on runs, 1.049 on homers.

        Home/road ratio on the SAME SET OF CLUBS both ways: every PA at park P
        against the rate those same clubs produced in all their OTHER games,
        which controls for club quality. Regressed toward 1.0 by games played.
        """
        slate = season_slate(season, save_dir=save_dir)
        venue, clubs = {}, {}
        for g in slate:
            pk = g.get("pk") or g.get("game_pk")
            if pk is None or not g.get("venue"):
                continue
            venue[pk] = g["venue"]
            clubs[pk] = (g.get("home"), g.get("away"))
        with gzip.open(Path(save_dir) / "pa" / "v2" / f"pa_{season}.json.gz") as fh:
            rows = json.load(fh)
        N = N_OUTCOMES
        at = collections.defaultdict(lambda: np.zeros(N))
        at_n: collections.Counter = collections.Counter()
        club_all = collections.defaultdict(lambda: np.zeros(N))
        club_n: collections.Counter = collections.Counter()
        pa_by_venue_club = collections.defaultdict(lambda: np.zeros(N))
        pav_n: collections.Counter = collections.Counter()
        for r in rows:
            v = venue.get(r["pk"])
            if v is None:
                continue
            o = r["o"]
            at[v][o] += 1
            at_n[v] += 1
            for c in clubs[r["pk"]]:
                if c is None:
                    continue
                club_all[c][o] += 1
                club_n[c] += 1
                pa_by_venue_club[(v, c)][o] += 1
                pav_n[(v, c)] += 1
        out: Dict[str, dict] = {}
        for v, cnt in at.items():
            if at_n[v] < 3000:
                continue
            here = cnt / at_n[v]
            elsew = np.zeros(N)
            elsen = 0
            for (vv, c), cc in pa_by_venue_club.items():
                if vv != v:
                    continue
                elsew += club_all[c] - cc
                elsen += club_n[c] - pav_n[(v, c)]
            if elsen < 3000:
                continue
            ref = elsew / elsen
            raw = np.where(ref > 0, here / np.maximum(ref, 1e-9), 1.0)
            games = at_n[v] / 76.0                       # ~76 PA a game
            w = games / (games + (1 - reliability) / reliability * 81.0)
            out[v] = {"raw": raw.tolist(), "factor": (1.0 + w * (raw - 1.0)).tolist(),
                      "pa": int(at_n[v]), "games": round(games, 1),
                      "w": round(float(w), 3)}
        return out

    @staticmethod
    def build_player_park_exposure(season: int,
                                   save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
        """Each player's ACTUAL park exposure, from his own plate appearances.

        The first version assumed `PARK_HOME_GAME_SHARE` at the board's `Team`
        tag, which fails exactly where it matters — ~9% of rows are `- - -`,
        traded mid-season. None of it is needed: `savedata/pa/v2/` carries every
        PA with its gamePk and the slate maps gamePk -> venue.

        **Stores the SHARES, not a baked exposure.** The shares are a fact about
        the schedule; the factors are an estimate with a window on them, and
        baking them together froze a single-season factor into a cache read over
        three — two numbers for one quantity.
        """
        slate = season_slate(season, save_dir=save_dir)
        venue = {}
        for g in slate:
            pk = g.get("pk") or g.get("game_pk")
            if pk is not None and g.get("venue"):
                venue[pk] = g["venue"]
        with gzip.open(Path(save_dir) / "pa" / "v2" / f"pa_{season}.json.gz") as fh:
            rows = json.load(fh)
        tally = {"bat": collections.defaultdict(collections.Counter),
                 "pit": collections.defaultdict(collections.Counter)}
        missing = 0
        for r in rows:
            v = venue.get(r["pk"])
            if v is None:
                missing += 1
                continue
            tally["bat"][r["bat"]][v] += 1
            tally["pit"][r["pit"]][v] += 1
        out: Dict[str, dict] = {}
        for side in ("bat", "pit"):
            per = {}
            for pid, parks in tally[side].items():
                tot = sum(parks.values())
                if tot < 25:                     # too few PAs to characterise
                    continue
                per[str(pid)] = {"pa": tot,
                                 "shares": {v: round(n / tot, 6)
                                            for v, n in parks.items()}}
            out[side] = per
        out["_meta"] = {"season": season, "pa_without_venue": missing,
                        "n_bat": len(out["bat"]), "n_pit": len(out["pit"])}
        return out

    @staticmethod
    def park_outcome_factor(club: Optional[str], season: int,
                            window: Optional[int] = None,
                            save_dir: Path = SAVE_DIR) -> List[float]:
        """A club's home-park factor per outcome, averaged over `window` seasons.

        Returns all-ones when the club is unknown — which is the RIGHT answer for
        the ~9% of board rows tagged `- - -`, a player who split the season
        between clubs and therefore has no single home park. That placeholder is
        the same one that corrupted `export_defense` (it normalises to "" and is a
        substring of every club), so it is handled by NAME here and never allowed
        to resolve to a park.
        """
        n = PARK_OUTCOME_WINDOW if window is None else window
        if not club or str(club).strip(" -") == "":
            return [1.0] * N_OUTCOMES
        acc = [0.0] * N_OUTCOMES
        hits = 0
        for s in range(season - n + 1, season + 1):
            venue = ParkFactors.club_home_park(club, s, save_dir)
            tab = _park_outcome_table(s, save_dir)
            f = tab.get(venue) if venue else None
            if not f:
                continue
            for i in range(N_OUTCOMES):
                acc[i] += f[i]
            hits += 1
        if not hits:
            return [1.0] * N_OUTCOMES
        return [a / hits for a in acc]

    @staticmethod
    def player_park_shares(pid: int, side: str, season: int,
                           save_dir: Path = SAVE_DIR) -> Optional[Dict[str, float]]:
        """Where this player's plate appearances were actually TAKEN, by park.

        Measured from `savedata/pa/v2/`, not assumed. The first version of this
        guessed `PARK_HOME_GAME_SHARE` of his PAs at the club named on his board
        row, which fails hardest exactly where it matters: ~9% of rows are tagged
        `- - -` for a player traded mid-season, and those are the players whose
        park mix is least like the assumption. It is also wrong for everyone else
        — players miss games, sit against same-handed starters, arrive in July,
        and the schedule is unbalanced.
        """
        key = (season, str(save_dir))
        got = _PARK_EXPO.get(key)
        if got is None:
            p = Path(save_dir) / f"player_park_exposure_{season}.json"
            got = json.load(open(p)) if p.exists() else {}
            _PARK_EXPO[key] = got
        return ((got.get(side) or {}).get(str(pid)) or {}).get("shares")

    @staticmethod
    def decontaminate_counts(counts: Sequence[float], pa: float,
                             pid: int, side: str, season: int,
                             save_dir: Path = SAVE_DIR) -> List[float]:
        """Strip a player's OWN park out of his counts, preserving PA.

        The multiplier his line carries is his MEASURED exposure — the parks he
        actually played in, weighted by PAs taken there. Dividing it out leaves
        the park-NEUTRAL line the stabilisers were measured to shrink. The vector
        is renormalised to the original PA so nothing downstream sees a changed
        sample size: the shrinkage weight must keep meaning what it meant.
        """
        f = measured_park_exposure(pid, side, season, save_dir)
        if all(abs(x - 1.0) < 1e-12 for x in f):
            return list(counts)
        out = []
        for i, c in enumerate(counts):
            out.append(c / f[i] if f[i] > 1e-6 else c)
        tot = sum(out)
        if tot <= 0:
            return list(counts)
        scale = (pa if pa > 0 else sum(counts)) / tot
        return [x * scale for x in out]

    @staticmethod
    def build_park_weather_ref_om(season: Optional[int] = None, lag: int = 0,
                                  save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
        """Each park's mean conditions AS OPEN-METEO SEES THEM -> a matched
        reference for the forecast arms. See `park_weather_reference`."""
        season = CURRENT_SEASON if season is None else int(season)
        fc = load_forecast_weather(season, lag, save_dir)
        acc: Dict[str, List[Tuple[float, float]]] = {}
        for row in season_slate(season, save_dir=save_dir):
            venue = resolve_venue(row.get("venue") or "")
            w = fc.get(int(row["pk"]))
            if not venue or not w:
                continue
            az = park_azimuth(venue)
            mph, deg = w.get("wind_mph"), w.get("wind_dir_deg")
            out = None
            if mph is not None and deg is not None and az is not None:
                field = (float(deg) - float(az)) % 360.0
                out = -float(mph) * math.cos(math.radians(field))
            if w.get("temp_f") is None or out is None:
                continue
            acc.setdefault(venue, []).append(
                (float(w["temp_f"]), out,
                 air_density(w.get("temp_f"), w.get("pressure_hpa"),
                             w.get("humidity_pct"))))
        ref = {v: {"temp_f": statistics.mean(t for t, _, _ in rows),
                   "out_component": statistics.mean(o for _, o, _ in rows),
                   # the park's own mean DENSITY, so the density term is centred on
                   # the population it is applied to (the recurring trap)
                   "density": (statistics.mean(d for _, _, d in rows if d is not None)
                               if any(d is not None for _, _, d in rows) else None),
                   "n": len(rows)}
               for v, rows in acc.items() if len(rows) >= 20}
        path = Path(save_dir) / f"park_weather_ref_om_{season}.json"
        with open(path, "w") as fh:
            json.dump(ref, fh, indent=1)
        print(f"[forecastwx] park reference for {len(ref)} parks -> {path}")
        return ref


# ---------------------------------------------------------------------------
# The two park BUILDERS — offline jobs, `python mlb_sim.py parkbuild`
# ---------------------------------------------------------------------------
# They lived as standalone scripts, so the readers below and the code producing
# what they read were in different files with no import between them — a change
# to `N_OUTCOMES`, the PA schema or the venue key would break the pair silently
# and only at read time. Heavy imports stay lazy so the GUI path is unchanged.


_PARK_EXPO: Dict[tuple, dict] = {}          # keyed on (season,) — never bare


def measured_park_exposure(pid: int, side: str, season: int,
                           save_dir: Path = SAVE_DIR) -> List[float]:
    """The park multiplier a player's own line already carries, per outcome.

    `sum over parks of (his share of PAs there) * factor[park]`. Falls back to
    neutral only when he has no measured PAs at all — a callup with fewer than
    25, or a season with no PA file.
    """
    shares = ParkFactors.player_park_shares(pid, side, season, save_dir)
    if not shares:
        return [1.0] * N_OUTCOMES
    n = PARK_OUTCOME_WINDOW
    expo = [0.0] * N_OUTCOMES
    for venue, w in shares.items():
        acc = [0.0] * N_OUTCOMES
        hits = 0
        rv = resolve_venue(venue) or venue
        for s in range(season - n + 1, season + 1):
            f = _park_outcome_table(s, save_dir).get(rv)
            if not f:
                continue
            for i in range(N_OUTCOMES):
                acc[i] += f[i]
            hits += 1
        for i in range(N_OUTCOMES):
            expo[i] += w * (acc[i] / hits if hits else 1.0)
    return expo


PARK_RUN_SEASON = 2026      # which season's park factors; lagged by
                            # TEAM_CONTEXT_LAG for a leak-free backtest


def park_run_tilt(venue: Optional[str], is_home: bool,
                  season: Optional[int] = None) -> float:
    """The park factor as an `offence_tilt`, CENTRED on that side's park mix.

    The home club plays ~half its games here and its rates already carry that,
    so its multiplier is divided by the mix; the visitor gets the full factor.
    """
    pf = park_run_factor(venue,
                         PARK_RUN_SEASON - TEAM_CONTEXT_LAG
                         if season is None else season)
    if pf == 1.0:
        return 0.0
    # The home club plays ~half its games here and its RATES already carry that,
    # so the multiplier is divided by the mix. `USE_PARK_DECONTAM` strips each
    # player's own park upstream, after which both sides take the full factor —
    # correcting one without the other double-counts, which is why they share a
    # flag.
    if USE_PARK_DECONTAM:
        m = pf
    else:
        m = (pf / (1.0 + PARK_HOME_GAME_SHARE * (pf - 1.0))) if is_home else pf
    return (REAL_MARKS["game_total_mean"] * (m - 1.0)) / RUNS_PER_TILT


_PARK_WX_REF: Dict[int, Dict[str, dict]] = {}


_PARK_WX_REF_OM: Dict[int, Dict[str, dict]] = {}


# Seasons already warned about a missing weather reference — once each.
_PARK_WX_WARNED: set = set()


def park_weather_reference(season: Optional[int] = None, save_dir: Path = SAVE_DIR
                           ) -> Dict[str, dict]:
    """{venue: {temp_f, out_component}} — each park's own typical conditions.

    **The reference has to be built from the SAME series it centres.** The
    shipped file is measured off StatsAPI observations, whose wind is a coarse
    eight-way LABEL; the forecast arms read Open-Meteo BEARINGS, and centring one
    on the other's mean leaves a standing +0.09 runs a game — a systematic lean
    to the over, and the "centre on the population you apply it to" trap again.
    Both lags use the DAY-0 series on purpose: a reference is climatology, not
    information, so day 0 centres the day-1 arm without leaking into it.
    """
    season = CURRENT_SEASON if season is None else int(season)
    # **KEYED ON SEASON.** These were bare globals, so the FIRST season loaded
    # was served for every later request — and because a missing file caches
    # `{}`, one call for a season with no reference file switched weather off
    # LEAGUE-WIDE for the rest of the process, silently. Exactly the defect
    # already fixed for `_FRAMING` and `_DEF`; it survived here because every
    # caller happens to use the default season.
    lag = weather_source_lag()
    cache = _PARK_WX_REF_OM if lag is not None else _PARK_WX_REF
    key = int(season)
    if key in cache:
        return cache[key]
    stem = "park_weather_ref_om" if lag is not None else "park_weather_ref"
    try:
        with open(save_dir / f"{stem}_{season}.json") as fh:
            cache[key] = json.load(fh)
        return cache[key]
    except (OSError, ValueError):
        pass

    # **A missing reference must NEVER degrade to {}.** `weather_tilt` reads it
    # as `ref.get("temp_f", temp)`, so an empty reference makes the term zero —
    # weather switches off ENTIRELY and SILENTLY, with plausible output (2025 got
    # a non-zero tilt on 0 of 1,500 games). Fall back to the NEAREST season that
    # exists. **The stems overlap and the naive glob is wrong**: `park_weather_
    # ref_*` also matches the `_om_` files, so the observed lookup "found"
    # Open-Meteo-only seasons and landed back on {}. Match the stem EXACTLY.
    have = []
    for f in Path(save_dir).glob(f"{stem}_*.json"):
        tail = f.stem[len(stem) + 1:]
        if tail.isdigit():
            have.append(int(tail))
    have = sorted(have)
    if not have:
        cache[key] = {}
        return cache[key]
    near = min(have, key=lambda y: (abs(y - key), -y))
    if key not in _PARK_WX_WARNED:
        _PARK_WX_WARNED.add(key)
        print(f"[weather] no {stem}_{season}.json — centring {season} on "
              f"{near}'s park climatology instead. The term is NOT off, but "
              f"it is centred on another season; build the reference to "
              f"remove this.")
    try:
        with open(save_dir / f"{stem}_{near}.json") as fh:
            cache[key] = json.load(fh)
    except (OSError, ValueError):
        cache[key] = {}
    return cache[key]


def air_density(temp_f: Optional[float], pressure_hpa: Optional[float],
                humidity_pct: Optional[float]) -> Optional[float]:
    """Air density in kg/m3 from station pressure, temperature and humidity.

    Drag and Magnus are both proportional to density, so this is the physically
    correct way to combine the three thermodynamic variables — one term instead
    of three collinear ones. Wind stays separate: it is a velocity.

    **Humid air is LESS dense than dry air**, because water vapour (18 g/mol) is
    lighter than the mix it displaces (~29 g/mol), so humidity HELPS offence —
    the opposite of the intuition that muggy air is heavy, and the obvious way to
    wire this backwards. Ideal gas with a Tetens correction, as in
    `homerunwidget`. `pressure_hpa` must be STATION pressure, never sea-level.
    """
    if temp_f is None or pressure_hpa is None:
        return None
    t_c = (float(temp_f) - 32.0) * 5.0 / 9.0
    t_k = t_c + 273.15
    p_pa = float(pressure_hpa) * 100.0
    rh = 0.0 if humidity_pct is None else max(0.0, min(100.0, float(humidity_pct)))
    # Tetens: saturation vapour pressure over water, in Pa
    p_sat = 610.78 * math.exp(17.27 * t_c / (t_c + 237.3))
    p_v = (rh / 100.0) * p_sat
    p_d = p_pa - p_v
    # R_dry 287.058, R_vapour 461.495 J/(kg K)
    return p_d / (287.058 * t_k) + p_v / (461.495 * t_k)


def wind_out_component(wind_mph: Optional[float], label: str
                       ) -> Optional[float]:
    """mph blowing out to centre; negative is in. None when unusable."""
    if wind_mph is None:
        return None
    f = WIND_OUT_COMPONENT.get((label or "").strip().lower())
    return None if f is None else wind_mph * f


def weather_tilt(weather: Optional[dict], venue: Optional[str] = None,
                 season: Optional[int] = None) -> float:
    """Tonight's conditions as an `offence_tilt`, per side.

    `weather` takes StatsAPI's own game-feed shape — `{"condition", "temp",
    "wind"}` with wind as "12 mph, Out To CF" — or the already-parsed
    `{"temp_f", "wind_mph", "wind_label"}`. Returns 0.0 when there is nothing
    usable, which is the honest default.
    """
    if not weather:
        return 0.0
    cond = str(weather.get("condition") or "").strip().lower()
    closed = cond in ROOF_CLOSED_CONDITIONS

    temp = weather.get("temp_f", weather.get("temp"))
    try:
        temp = float(temp) if temp not in (None, "") else None
    except (TypeError, ValueError):
        temp = None

    mph = weather.get("wind_mph")
    label = weather.get("wind_label")
    if mph is None and weather.get("wind"):
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*mph,\s*(.*)", str(weather["wind"]))
        if m:
            mph, label = float(m.group(1)), m.group(2)
    out = None if closed else wind_out_component(mph, label or "")
    # Compass path. A feed bearing names the direction the wind blows FROM,
    # clockwise from TRUE NORTH, so it must be rotated into the park frame
    # before it means anything — see CLAUDE.md on wind frames. StatsAPI's own
    # label is already field-relative and skips this entirely.
    if (out is None and not closed and mph is not None
            and weather.get("wind_dir_deg") is not None
            and str(weather.get("wind_frame", "")).lower() != "field"):
        az = park_azimuth(resolve_venue(venue or "") or "") if venue else None
        if az is not None:
            field = (float(weather["wind_dir_deg"]) - float(az)) % 360.0
            out = -mph * math.cos(math.radians(field))
    elif (out is None and not closed and mph is not None
            and weather.get("wind_dir_deg") is not None):
        out = -mph * math.cos(math.radians(float(weather["wind_dir_deg"])))

    # **The season was a hardcoded default of 2026 and no caller ever passed
    # one**, so every season was centred on 2026's climatology while
    # `park_weather_reference` carried an elaborate docstring about being
    # KEYED ON SEASON that nothing exercised. It now tracks the season being
    # priced, the same way `park_run_tilt` reads `PARK_RUN_SEASON` — and
    # NOT lagged, because weather is a same-day quantity.
    season = PARK_RUN_SEASON if season is None else int(season)
    ref = park_weather_reference(season).get(resolve_venue(venue or "") or "",
                                            {}) if venue else {}
    runs = 0.0
    # **Density when we have the inputs, temperature when we do not.** The
    # StatsAPI game feed carries no pressure or humidity, so the shipped
    # `observed` source cannot form a density and correctly falls back; the
    # Open-Meteo path carries both. Degrading is the point — the alternative is
    # a term that silently reads zero on the source that lacks the fields.
    # **A CLOSED ROOF gates TEMPERATURE too, not just wind (fixed 2026-08-29).**
    # `closed` used to reach only the wind term, so a fixed dome was charged the
    # OUTDOOR air: Tropicana on a 95F day took +0.731 runs for a game played in
    # a climate-controlled building. The two numbers are not even the same
    # quantity — `park_weather_reference` is built from OBSERVED game conditions,
    # which for a dome are the INDOOR ~72F, while the forecast supplies outside
    # air. Differencing them manufactures a tilt out of a unit mismatch.
    #
    # This is the Chase Field defect that `forecast_game_weather` already
    # records, and the fix there only covered RETRACTABLE parks (it returns None
    # for them). The FIXED domes still came through here with a real temperature.
    if not closed:
        dens = None
        if USE_AIR_DENSITY:
            dens = air_density(temp, weather.get("pressure_hpa"),
                               weather.get("humidity_pct"))
        ref_dens = ref.get("density")
        if dens is not None and ref_dens:
            runs += (WEATHER_DENSITY_RUNS_PER_PCT
                     * (dens - ref_dens) / ref_dens * 100.0)
        elif temp is not None:
            runs += WEATHER_TEMP_RUNS_PER_F * (temp - ref.get("temp_f", temp))
    if out is not None:
        # Scaled by how much wind this park actually feels.
        runs += (WEATHER_WIND_OUT_RUNS_PER_MPH * park_wind_factor(venue)
                 * (out - ref.get("out_component", out)))
    tilt = runs / RUNS_PER_TILT if RUNS_PER_TILT else 0.0
    # Clamped. The fit is LINEAR over the observed range and these are applied
    # up to ~3 sd out; over 1,845 real games the tilt runs -0.142..+0.133 with
    # sd 0.030, so this binds on ~0.3% of games and exists to stop a misparsed
    # wind string or a bad temperature producing a nonsense line.
    return max(-WEATHER_TILT_CLAMP, min(WEATHER_TILT_CLAMP, tilt))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

class DistanceCalibration:
    """Fitting `distance_scale` so predicted home runs match real ones."""

    @staticmethod
    def load_scale(default: float = 1.0) -> float:
        """The fitted distance scale. 1.0 until `calibrate_distance()` has run."""
        try:
            with open(CALIB_PATH) as fh:
                return float(json.load(fh)["distance_scale"])
        except (OSError, KeyError, ValueError, TypeError):
            return default

    @staticmethod
    def save_scale(scale: float, note: dict) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        with open(CALIB_PATH, "w") as fh:
            json.dump({"distance_scale": scale, **note}, fh, indent=2)

    @staticmethod
    def load_bbe_frame(season: Optional[int] = None, path: Optional[Path] = None):
        """Real batted balls with the columns this module needs, park-labelled."""
        season = CURRENT_SEASON if season is None else int(season)
        import pandas as pd
        TEAM_TO_PARK, STADIUM_DATA = weatherman.TEAM_TO_PARK, weatherman.STADIUM_DATA

        # The Savant CSVs are shared with `savant_bbe_fetch` / `homerunwidget`
        # and stay at the app root.
        path = path or (_APP_ROOT / f"savant_bbe_{season}.csv")
        df = pd.read_csv(path, usecols=[
            "events", "launch_speed", "launch_angle", "hc_x", "hc_y",
            "home_team", "batter"], low_memory=False)
        df = df.dropna(subset=["launch_speed", "launch_angle", "hc_x", "hc_y"])
        df["park"] = df["home_team"].map(TEAM_TO_PARK)
        df = df[df["park"].isin(STADIUM_DATA.keys())]

        dx = df["hc_x"] - 125.42
        dy = 198.27 - df["hc_y"]
        df = df[dy > 0]
        df["hla"] = np.degrees(np.arctan2(dx[dy > 0], dy[dy > 0]))
        df["is_hr"] = (df["events"] == "home_run").astype(int)
        return df

    @staticmethod
    def calibrate_distance(season: Optional[int] = None, sample: int = 60000,
                           seed: int = 7, workers: Optional[int] = None) -> dict:
        """Fit `distance_scale` so predicted home runs match REAL ones.

        The raw physics runs ~45 ft short, and a home run is a hard threshold
        against a fence, so that bias does NOT cancel in a ratio — left alone
        almost nothing clears and the multiplier becomes tail noise. Fitting the
        COUNT rather than per-ball accuracy is deliberate: what has to be right is
        where the fence sits in the distance distribution, not which individual
        ball went out.
        """
        season = CURRENT_SEASON if season is None else int(season)
        df = DistanceCalibration.load_bbe_frame(season)
        if sample and len(df) > sample:
            df = df.sample(sample, random_state=seed)
        air = df[(df["launch_angle"].between(LA_MIN, LA_MAX))
                 & (df["launch_speed"] >= EV_MIN)]
        actual = int(df["is_hr"].sum())
        parks = sorted(air["park"].unique())
        print(f"[calib] {len(df)} batted balls, {len(air)} air balls, "
              f"{actual} real home runs, {len(parks)} parks")

        # One bank per PARK, because altitude changes the air. Everything else
        # about the fit is a lookup against these.
        # Build every missing bank in parallel first, then read them back in.
        # Resumable: already-cached parks are skipped, so an interrupted run
        # picks up where it stopped rather than starting over.
        BallFlight.prebuild_banks(parks, {}, workers=workers)
        banks = {}
        for i, park in enumerate(parks, 1):
            banks[park] = BallFlight.cached_trajectory_bank({}, venue=park)
            print(f"[calib]   loaded {i}/{len(parks)} {park}", flush=True)

        by_park = {park: air[air["park"] == park] for park in parks}

        def predicted(scale: float) -> int:
            total = 0
            for park in parks:
                grid = BallFlight.fence_grid_from_bank(banks[park], park, scale)
                sub = by_park[park]
                for ev, la, hla in zip(sub["launch_speed"], sub["launch_angle"],
                                       sub["hla"]):
                    if ev >= BallFlight._lookup(grid, hla, la):
                        total += 1
            return total

        lo, hi = 0.90, 1.45
        seen: List[Tuple[float, int]] = []
        for _ in range(7):
            mid = 0.5 * (lo + hi)
            pred = predicted(mid)
            print(f"[calib] scale {mid:.4f} -> {pred} predicted vs {actual} actual")
            seen.append((mid, pred))
            if pred < actual:
                lo = mid
            else:
                hi = mid

        # Take the CLOSEST candidate, not the last one bisection happened to try.
        # The final midpoint is not the best estimate — here it landed on 1106
        # against a real 1055 while an earlier probe at 1041 was twice as close.
        scale, pred = min(seen, key=lambda sp: abs(sp[1] - actual))
        # Then interpolate between the two probes that bracket the target, which
        # this curve supports because it is steep and locally straight.
        below = [sp for sp in seen if sp[1] <= actual]
        above = [sp for sp in seen if sp[1] > actual]
        if below and above:
            lo_s, lo_p = max(below, key=lambda sp: sp[1])
            hi_s, hi_p = min(above, key=lambda sp: sp[1])
            if hi_p > lo_p:
                scale = lo_s + (actual - lo_p) / (hi_p - lo_p) * (hi_s - lo_s)
                pred = predicted(scale)
                print(f"[calib] interpolated {scale:.4f} -> {pred} vs {actual}")
        note = {"season": season, "sample": len(df), "actual_hr": actual,
                "predicted_hr": pred, "parks": len(parks)}
        DistanceCalibration.save_scale(scale, note)
        print(f"[calib] wrote distance_scale={scale:.4f} to {CALIB_PATH}")
        return {"distance_scale": scale, **note}


# ===========================================================================
# 11. PROJECTION — END TO END
# ===========================================================================


DEFAULT_SIMS = 20000


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def describe_wind(weather: Optional[dict], venue: Optional[str] = None) -> str:
    """A human, FIELD-RELATIVE description of a weather dict's wind.

    StatsAPI observations arrive field-relative with a `wind_label`; a FORECAST
    carries a compass bearing and no label, which is why the verbose line read
    "wind 4.604763008578949 mph None". The number was never wrong — `wind_frame`
    is tagged and the engine rotates it — but a bare `None` looks like a failure.
    Falls back to the raw bearing when the park is unknown, because guessing an
    orientation would put a real wind on the wrong axis.
    """
    if not weather:
        return ""
    label = weather.get("wind_label")
    if label:
        return str(label)
    deg = weather.get("wind_dir_deg")
    if deg is None:
        return ""
    if str(weather.get("wind_frame")) == "compass":
        try:
            field = weatherman.wind_to_field_frame(float(deg), venue)
            if field is not None:
                best = min(weatherman.MLB_WIND_LABELS.items(),
                           key=lambda kv: abs((float(field) - kv[1] + 180) % 360 - 180))
                return f"{best[0]} ({float(field):.0f}deg field)"
        except Exception:                                          # noqa: BLE001
            pass
    return f"from {float(deg):.0f}deg"


def project_game(home_abbr: str, away_abbr: str, venue: Optional[str] = None,
                 weather: Optional[dict] = None, n_sims: Optional[int] = None,
                 season: Optional[int] = None, seed: Optional[int] = 1,
                 hazard: Optional[List[float]] = None,
                 verbose: bool = False, with_pen: bool = True,
                 date: Optional[str] = None, live: bool = True,
                 game_number: Optional[int] = None) -> dict:
    """Simulate one game and return the priced board.

    **`venue` and `weather` DO alter the simulation.** What was removed on
    2026-08-15 is the park x weather HOME-RUN INTERACTION term (§10); the park run
    factor and the weather tilt both still ride the form axis and both are large —
    forcing Coors moves a measured total +1.93 runs, a 95F / 18 mph out-to-CF
    override +1.43. This docstring previously said they were "carried for
    reporting only", which is how trap 12 happens: `run_clv` passed neither and
    priced every live game at a neutral park.
    """
    n_sims = DEFAULT_SIMS if n_sims is None else int(n_sims)
    season = CURRENT_SEASON if season is None else int(season)
    home_abbr = normalize_club(home_abbr)
    away_abbr = normalize_club(away_abbr)
    bat_table, _ = build_rates("bat")
    pit_table, _ = build_rates("pit")
    hz = hazard or starter_hazard()

    # **Tonight's ACTUAL probables and posted lineups.** Without this the
    # sides fall back to `build_side`'s season-board choices, whose starter is
    # simply the club's highest-GS arm — i.e. every game is simulated as the
    # two aces, which is not the game being played.
    card = {}
    if live:
        try:
            card = probable_for(fetch_probables(date), away_abbr,
                                home_abbr, game_number) or {}
        except Exception as e:
            if verbose:
                print(f"  probables unavailable ({e}) — season-board sides")
    if card and not venue:
        venue = card.get("venue") or None
    # Tonight's conditions, if the caller did not supply them. StatsAPI's own
    # wind label is already FIELD-relative, so it needs no azimuth rotation.
    if live and weather is None and card.get("game_pk"):
        try:
            # **A SCHEDULED game has no observation, and that was silent.**
            # `game_weather` reads StatsAPI's game-time reading, which does not
            # exist until the game does, so every forward projection priced at
            # `weather_tilt = 0.0` — a neutral park on a 95F day. The forecast
            # is the information set a projection legitimately has, AND its
            # numeric bearing beats StatsAPI's 8-way label — see
            # `live_game_weather`, which orders the two and keeps the roof.
            weather = live_game_weather(
                card["game_pk"], date,
                venue or resolve_venue(card.get("venue") or ""),
                card.get("start"))
            if weather and verbose:
                print(f"  weather: {weather.get('condition')}, "
                      f"{weather.get('temp_f', 0):.0f}F, wind "
                      f"{weather.get('wind_mph', 0):.1f} mph "
                      f"{describe_wind(weather, venue)}"
                      f"   -> {weather_tilt(weather, venue) * RUNS_PER_TILT:+.2f} "
                      f"runs")
        except Exception as e:
            if verbose:
                print(f"  weather unavailable ({e})")

    home, home_used = build_side_live(
        home_abbr, bat_table, pit_table, season=season, hazard=hz,
        sp_id=card.get("home_sp"), lineup_ids=card.get("home_lineup"),
        catcher_id=card.get("home_catcher"),
        use_itp_pen=with_pen)
    away, away_used = build_side_live(
        away_abbr, bat_table, pit_table, season=season, hazard=hz,
        sp_id=card.get("away_sp"), lineup_ids=card.get("away_lineup"),
        catcher_id=card.get("away_catcher"),
        use_itp_pen=with_pen)
    if verbose:
        for tag, sd, u, _sk in ((home_abbr, home, home_used, "home"),
                                (away_abbr, away, away_used, "away")):
            rep = u.get("pen_report") or {}
            note = (f"  resting {rep['rested']}" if rep.get("rested") else "")
            sp_tag = "" if u["sp"] else "  [BOARD FALLBACK]"
            # **Three states, not two.** This read `"posted" if u["lineup"]`,
            # which only asks whether a nine was found — so Rotowire's beat-
            # writer PROJECTION printed as "posted". `probable_for` already tags
            # `lineup_source`, and the whole point of that tag is that a
            # projection is never folded in silently.
            lu_tag = (("posted" if card.get(f"{_sk}_lineup_source") == "posted"
                       else "PROJECTED") if u["lineup"] else "board FALLBACK")
            print(f"  {tag}: SP {sd.starter.name}{sp_tag}"
                  f"   lineup {lu_tag}   pen {u['pen']}{note}")

    # **The rate correction, on the LIVE path too.** This used to omit `ml=`,
    # so `project` ran the incumbent whatever `RATE_MODEL` said while `clv` and
    # `backtest` ran the variant. Inert at `RATE_MODEL = "baseline"`, which is
    # exactly why it would have survived until the residual shipped. Trap 12:
    # the live path is not covered by the A/B harness, so an optional argument
    # defaulting to None is a silent divergence there and nowhere else.
    # `as_of` is "" — LIVE; tonight's game legitimately has today's board.
    gdate = date or datetime.date.today().isoformat()
    results = simulate_many(
        home, away, n=n_sims, seed=seed, weather=weather, venue=venue,
        ml=game_adjuster(season, "", {
            "venue": venue or card.get("venue") or "", "date": gdate,
            "temp_f": (weather or {}).get("temp_f"),
            "wind_mph": (weather or {}).get("wind_mph"),
            "wind_label": (weather or {}).get("wind_label") or "",
            "home_sp": card.get("home_sp") or -1,
            "away_sp": card.get("away_sp") or -1,
        }, home, away))
    return {"home": home, "away": away, "results": results,
            "venue": venue, "weather": weather}


# Lines chosen to sit where books actually hang them.
BATTER_BOARD = (
    ("batter_hits", 0.5), ("batter_hits", 1.5),
    ("batter_total_bases", 1.5), ("batter_home_runs", 0.5),
    ("batter_rbis", 0.5), ("batter_runs_scored", 0.5),
    ("batter_strikeouts", 0.5), ("batter_walks", 0.5),
    ("batter_stolen_bases", 0.5),
)
PITCHER_BOARD = (
    ("pitcher_strikeouts", 4.5), ("pitcher_strikeouts", 5.5),
    ("pitcher_strikeouts", 6.5), ("pitcher_outs", 15.5),
    ("pitcher_outs", 17.5), ("pitcher_hits_allowed", 4.5),
    ("pitcher_walks", 1.5),
)


class Projection:
    """Turning a projection into a priced board."""

    @staticmethod
    def price_board(proj: dict) -> List[dict]:
        """Every mapped market for every player in the game, fairly priced."""
        res = proj["results"]
        rows = []
        for side in ("away", "home"):
            team = proj[side]
            for b in team.lineup:
                for market, line in BATTER_BOARD:
                    if market not in BATTER_MARKETS:
                        continue
                    rows.append({"side": side, **summarize_prop(
                        res, b.name, market, line)})
            for market, line in PITCHER_BOARD:
                rows.append({"side": side, **summarize_prop(
                    res, team.starter.name, market, line)})
        return rows

    @staticmethod
    def _fmt(v):
        return f"{v:+d}" if isinstance(v, int) else "  --"


# ===========================================================================
# 12. COMMAND LINE
# ===========================================================================


def league_side(tag: str) -> TeamSide:
    """A flat league-average side: nine league hitters, a league starter on the
    real hook curve, and EIGHT league relievers.

    **There were five byte-identical copies of this**, which is how two subtly
    different synthetic sides come to exist without anyone deciding. Deliberately
    NOT `_demo_side`, which tilts by `quality` and carries six arms.

    **What it cannot do, stated here rather than rediscovered**: every arm is
    identical and none carries deployment traits, so this side structurally
    cannot express anything margin- or leverage-conditional. A probe of the pen's
    score-awareness built on it measures zero and reads as a clean null (trap 5,
    three instances). Use real sides for that.
    """
    return TeamSide(
        [Batter(f"{tag}b{i}", list(LEAGUE_BASELINE)) for i in range(9)],
        Pitcher(f"{tag}SP", list(LEAGUE_BASELINE), is_starter=True,
                hazard=starter_hazard()),
        [Pitcher(f"{tag}RP{i}", list(LEAGUE_BASELINE)) for i in range(8)])


def _demo_side(name: str, quality: float = 1.0) -> TeamSide:
    """A synthetic side for the smoke test. `quality` tilts the lineup's
    contact/power against league."""
    lineup = []
    for i in range(9):
        r = list(LEAGUE_BASELINE)
        r[HR] *= quality
        r[S1B] *= quality
        r[K] /= quality
        lineup.append(Batter(f"{name}-bat{i+1}", _normalize(r)))
    sp = Pitcher(f"{name}-SP", list(LEAGUE_BASELINE), is_starter=True,
                 hazard=starter_hazard())
    pen = [Pitcher(f"{name}-RP{i+1}", list(LEAGUE_BASELINE)) for i in range(6)]
    return TeamSide(lineup=lineup, starter=sp, bullpen=pen)


class Reports:
    """The human-readable reports behind `rates`, `project` and `smoke`."""

    @staticmethod
    def smoke_test() -> None:
        home = _demo_side("HOME", quality=1.10)
        away = _demo_side("AWAY", quality=0.95)
        n = 5000
        res = simulate_many(home, away, n=n, seed=7)

        rh = sum(r.runs_home for r in res) / n
        ra = sum(r.runs_away for r in res) / n
        print(f"{n} sims — mean runs: home {rh:.2f}, away {ra:.2f}")
        print(f"home win% {sum(1 for r in res if r.runs_home > r.runs_away)/n:.3f}")

        for mkt, line in (("batter_hits", 0.5), ("batter_total_bases", 1.5),
                          ("batter_home_runs", 0.5)):
            s = summarize_prop(res, "HOME-bat3", mkt, line)
            print(f"  HOME-bat3 {mkt:24s} {line}  mean {s['mean']:.2f}  "
                  f"P(over) {s['p_over']:.3f}  fair {s['fair_over']:+d}")

        s = summarize_prop(res, "AWAY-SP", "pitcher_strikeouts", 5.5)
        print(f"  AWAY-SP  pitcher_strikeouts     5.5  mean {s['mean']:.2f}  "
              f"P(over) {s['p_over']:.3f}  fair {s['fair_over']:+d}")
        s = summarize_prop(res, "AWAY-SP", "pitcher_outs", 15.5)
        print(f"  AWAY-SP  pitcher_outs          15.5  mean {s['mean']:.2f}  "
              f"P(over) {s['p_over']:.3f}  fair {s['fair_over']:+d}")

    @staticmethod
    def rates_report() -> None:
        for side, label in (("bat", "BATTING"), ("pit", "PITCHING")):
            seasons = available_seasons(side)
            table, league = build_rates(side)
            print(f"\n=== {label}  seasons {seasons}  players {len(table)} ===")
            print("league baseline per PA:")
            print("   " + "  ".join(f"{n} {league[i]:.4f}"
                                    for i, n in enumerate(OUTCOME_NAMES)))
            print(f"   sum {sum(league):.6f}")

            ranked = sorted(table.items(), key=lambda kv: -kv[1]["pa"])[:3]
            for _pid, rec in ranked:
                r = rec["rates"]
                print(f"  {rec['name']:<24s} PA {rec['pa']:6.0f}  "
                      f"K {r[K]:.3f} BB {r[BB]:.3f} HR {r[HR]:.3f} "
                      f"1B {r[S1B]:.3f} GB {r[GB_OUT]:.3f} AIR {r[AIR_OUT]:.3f}")

    @staticmethod
    def project_cli(argv=None) -> None:
        ap = argparse.ArgumentParser(prog='mlb_sim.py project')
        ap.add_argument("home")
        ap.add_argument("away")
        ap.add_argument("--venue", default=None)
        ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
        ap.add_argument("--temp", type=float, default=None)
        ap.add_argument("--wind", type=float, default=None)
        ap.add_argument("--wind-dir", type=float, default=None,
                        help="compass bearing the wind blows FROM")
        ap.add_argument("--wind-label", default=None,
                        help='field-relative label instead of a bearing, '
                             'StatsAPI style: "Out To CF", "In From LF", ...')
        args = ap.parse_args(argv)

        weather = None
        if args.temp is not None or args.wind is not None:
            weather = {"temp_f": args.temp if args.temp is not None else 70.0,
                       "wind_mph": args.wind or 0.0}
            if args.wind_label:
                weather["wind_label"] = args.wind_label
            else:
                weather["wind_dir_deg"] = args.wind_dir or 0.0
                weather["wind_frame"] = "compass"

        print(f"\n{args.away} @ {args.home}"
              + (f"  —  {args.venue}" if args.venue else "")
              + (f"  —  {weather['temp_f']:.0f}F, wind {weather['wind_mph']:.0f} mph "
                 + (f"{weather['wind_label']}" if weather.get("wind_label")
                    else f"from {weather.get('wind_dir_deg', 0):.0f}deg")
                 if weather else ""))
        print(f"  {args.sims} simulations\n")

        proj = project_game(args.home, args.away, args.venue, weather,
                            args.sims, verbose=True)
        res = proj["results"]
        n = len(res)
        rh = sum(r.runs_home for r in res) / n
        ra = sum(r.runs_away for r in res) / n
        wins = sum(1 for r in res if r.runs_home > r.runs_away) / n
        # **The MEAN and the MEDIAN are different numbers and only one is
        # comparable to a book's line.** Game runs are right-skewed, so the line
        # a book hangs is the MEDIAN, 0.40-0.47 BELOW the mean. Printing only
        # the mean invites reading the skew as a half-run disagreement — a trap
        # recorded three times in sim_state.md and walked into again on
        # 2026-08-20. **Not the sample median** either: a game total is a whole
        # number, so what is comparable is the HALF-POINT line whose over and
        # under sit closest to even money, the same quantity `market_total`
        # reads out of the book.
        totals = [r.runs_home + r.runs_away for r in res]
        t_mean = (ra + rh)
        lines = [x + 0.5 for x in range(0, 30)]
        fair_line = min(lines, key=lambda L: abs(price_over(totals, L) - 0.5))
        p_over = price_over(totals, fair_line)
        print(f"\n  projected score   {args.away} {ra:.2f} — {rh:.2f} {args.home}")
        print(f"  total             {t_mean:.2f} MEAN   |   fair line "
              f"{fair_line:.1f}  (P over {p_over:.3f})"
              f"   <- compare the LINE to a book's, never the mean")
        print("                     a book's number is the even-money point, and "
              "runs are right-skewed, so it sits ~0.4 under the mean")
        print(f"  home win prob     {wins:.1%}   fair {Projection._fmt(to_american(wins))}")

        print(f"\n  {'player':<24s} {'market':<22s} {'line':>5s} "
              f"{'mean':>6s} {'P(o)':>6s} {'over':>6s} {'under':>6s}")
        for row in Projection.price_board(proj):
            if row["mean"] < 0.02:
                continue
            print(f"  {row['player']:<24s} {row['market']:<22s} "
                  f"{row['line']:>5.1f} {row['mean']:>6.2f} "
                  f"{row['p_over']:>6.3f} {Projection._fmt(row['fair_over']):>6s} "
                  f"{Projection._fmt(row['fair_under']):>6s}")


class Cli:
    """The `python mlb_sim.py <command>` surface — one method per command.

    Each method takes the FULL argv (so every body still parses
    `argv[1:]` exactly as it did inline) and is dispatched through
    `Cli.COMMANDS`. `main()` stays a module-level function because that
    is what `python mlb_sim.py` and the suites reach for.
    """

    @staticmethod
    def cmd_rates(argv) -> None:
        """ingest report off the boards"""
        Reports.rates_report()

    @staticmethod
    def cmd_calibrate(argv) -> None:
        """fit the HR distance scale"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py calibrate")
        ap.add_argument("--workers", type=int, default=None,
                        help="parallel bank builders (default: cores - 2)")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--sample", type=int, default=60000)
        a = ap.parse_args(argv[1:])
        DistanceCalibration.calibrate_distance(a.season, a.sample, workers=a.workers)

    @staticmethod
    def cmd_project(argv) -> None:
        Reports.project_cli(argv[1:])

    @staticmethod
    def cmd_clv(argv) -> None:
        """score against market movement"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py clv")
        ap.add_argument("--sport", default="baseball")
        ap.add_argument("--sims", type=int, default=8000)
        ap.add_argument("--limit", type=int, default=None,
                        help="only the first N games on the board")
        ap.add_argument("--no-live", action="store_true",
                        help="ignore tonight's probables/lineups (season "
                             "boards only — biases totals high)")
        ap.add_argument("--edge", type=float, default=0.02,
                        help="edge floor for the filtered bucket")
        ap.add_argument("--ml", default=None,
                        help="price with the ML rate layer: a node list or "
                             "'all' (see mlb_ml section 5). Uses the "
                             "walk-forward fold for the season.")
        ap.add_argument("--alpha", type=float, default=0.25,
                        help="--ml only: residual strength, logit space")
        ap.add_argument("--date", default=None,
                        help="slate date, YYYY-MM-DD (default today). The "
                             "BOARD and the PROBABLES both use it — "
                             "OddsPortal's default listing carries yesterday's "
                             "finished games and they must not be priced "
                             "against today's lineups")
        a = ap.parse_args(argv[1:])
        if a.ml:
            _d = a.date or datetime.date.today().isoformat()
            _season = int(_d[:4])
            fold = ML_FOLD_FOR_SEASON.get(_season)
            if not fold:
                raise SystemExit(
                    f"mlb_sim: no walk-forward ML fold for {_season}; a season "
                    f"may only be priced by a model that predates it.")
            RATE_MODEL = "blend"
            ML_HIER_NODES = a.ml
            ML_BLEND_ALPHA = a.alpha
            ML_MODEL_FOLD = fold
            globals().update(RATE_MODEL=RATE_MODEL, ML_HIER_NODES=ML_HIER_NODES,
                             ML_BLEND_ALPHA=ML_BLEND_ALPHA,
                             ML_MODEL_FOLD=ML_MODEL_FOLD)
            print(f"[clv] ML rate layer ON — nodes {a.ml!r}, alpha {a.alpha}, "
                  f"fold {fold} (trained {AbHarness.ml_fold_span(fold)})")
        picks, summary = run_clv(a.sport, a.sims, limit=a.limit,
                                 live_lineups=not a.no_live, date=a.date)
        if not picks:
            raise SystemExit("no picks — nothing on the board resolved")
        b = summary.get("bias") or {}
        if b.get("n"):
            print()
            print(f"TOTALS CALIBRATION  ({b['n']} games)")
            print(f"  model mean {b['mean_model']:.2f}   "
                  f"market mean {b['mean_market']:.2f}   "
                  f"disagreement {b['mean_diff']:+.2f} runs "
                  f"(median {b['median_diff']:+.2f}, "
                  f"model over on {b['over_share']:.0%})")
            # **model - market is a DISAGREEMENT and attributing it to the model
            # is what cost the 2026-08-29 session** — see `league_fair_total`.
            # Split it before reacting to it.
            if b.get("league_fair") is not None:
                print(f"  league fair line {b['league_fair']:.2f}   ->   "
                      f"MODEL {b['model_vs_league']:+.2f}   "
                      f"market {b['market_vs_league']:+.2f}")
                if abs(b["model_vs_league"]) > 0.25:
                    print("  ** The MODEL is off by more than a quarter run "
                          "against the league's own fair line. That part is "
                          "yours; fix it before reading the edge buckets. **")
                elif abs(b["mean_diff"]) > 0.25:
                    print("  ** The disagreement is mostly the MARKET's "
                          "position on this slate, not model bias. Do not go "
                          "hunting for runs the model has not lost. **")
            elif abs(b["mean_diff"]) > 0.25:
                print(f"  ** No league fair line cached for this season, so "
                      f"this {b['mean_diff']:+.2f} cannot be split into model "
                      f"error and market position. Treat it as a "
                      f"disagreement, not a bias. **")
        summary = Clv.summarize_clv(picks, a.edge)
        fade = summary.get("fade")
        if fade is not None:
            print()
            print(f"FADE CORRELATION  {fade:+.3f}")
            if abs(fade) > 0.4:
                print("  ** The model is mostly FADING the market, not "
                      "disagreeing with it game by game.\n"
                      "     The edge board below is a readout of compressed "
                      "outputs, not of market error. **")
        print()
        print(f"{'bucket':<18s} {'n':>4s} {'mean CLV':>10s} {'CLV>0':>8s}")
        for name, row in (("all picks", summary["all"]),
                          (f"edge >= {a.edge:.0%}", summary["edge"])):
            if row["n"]:
                print(f"{name:<18s} {row['n']:>4d} {row['clv']:>+9.3%} "
                      f"{row['hit']:>8.1%}")
        print()
        for mkt, row in sorted(summary["by_market"].items()):
            if row["n"]:
                print(f"  {mkt:<16s} {row['n']:>4d} {row['clv']:>+9.3%} "
                      f"{row['hit']:>8.1%}")
        print()
        print("NOTE: rates are season-to-date, so scoring games already played "
              "carries\n      look-ahead bias. Treat this as a plumbing check, "
              "not an edge estimate.")

    @staticmethod
    def cmd_calibrate_form(argv) -> None:
        """fit the game-level form draw"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py calibrate-form")
        ap.add_argument("--sims", type=int, default=8000)
        ap.add_argument("--target", type=float, default=0.0147,
                        help="clone mode only: per-inning covariance the draw "
                             "should ADD; the real total is 0.0192 and matchup "
                             "spread already supplies ~0.0045")
        ap.add_argument("--slate", action="store_true",
                        help="fit on the REAL slate against the real "
                             "covariance, instead of on clones against a "
                             "target with the matchup share assumed out")
        ap.add_argument("--reps", type=int, default=20,
                        help="slate mode: sims per real game")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--workers", type=int, default=None,
                        help="slate mode: processes (default cores - 2)")
        a = ap.parse_args(argv[1:])
        r = (SlateCalibration.calibrate_form_on_slate(a.season, reps=a.reps,
                                     workers=a.workers) if a.slate
             else Validation.calibrate_form(a.target, a.sims))
        print("\n  paste into mlb_sim.py:")
        print(f"    GAME_FORM_SD = {r['GAME_FORM_SD']:.4f}")
        print(f"    GAME_FORM_MEAN_SHIFT = {r['GAME_FORM_MEAN_SHIFT']:.4f}")

    @staticmethod
    def cmd_marks(argv) -> None:
        """re-measure the reference marks"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py marks")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        m = Validation.measure_real_marks(a.season, refresh=a.refresh)
        print(f"reference marks, {a.season}  ({m.get('_games')} games, "
              f"{m.get('_range')})")
        for k, v in m.items():
            if k.startswith("_"):
                continue
            if isinstance(v, list):
                print(f"  {k:32s} " + " ".join(f"{x:.3f}" for x in v))
                continue
            frozen = REAL_MARKS.get(k)
            drift = (f"  frozen {frozen:.4f}"
                     if isinstance(frozen, (int, float)) else "")
            print(f"  {k:32s} {v:9.4f}{drift}")

    @staticmethod
    def cmd_dispersion(argv) -> None:
        """run DISPERSION on clone sides"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py dispersion")
        ap.add_argument("--sims", type=int, default=6000)
        ap.add_argument("--season", type=int, default=2026)
        a = ap.parse_args(argv[1:])
        r = Validation.validate_dispersion(a.sims, season=a.season)
        real = r["real"]
        print(f"innings 1-8, {r['team_games']} sim team-games "
              f"(league-average CLONES — no matchup spread by construction)")
        print(f"  {'':10s} {'sim':>10s} {'real':>10s}")
        for k in ("var", "indep", "cov", "pair_cov"):
            rv = real.get(k)
            print(f"  {k:10s} {r[k]:10.4f} "
                  f"{rv:10.4f}" if rv is not None else
                  f"  {k:10s} {r[k]:10.4f}          -")
        print(f"\n  covariance share of variance: sim {r['cov_share']:+.1%}"
              f"   real {real['cov']/real['var']:+.1%}"
              if real.get("var") else "")
        print("\n  by lag (flat => shared per-game factor; "
              "decaying => momentum):")
        for lag, c in sorted(r["by_lag"].items()):
            print(f"    lag {lag}  {c:+.5f}")
        print("\n  by window   (real: starter 1-5 +0.0135, "
              "bullpen 6-8 +0.0316, spanning +0.0204)")
        for k, v in r["window"].items():
            print(f"    {k:12s} {v:+.5f}" if v is not None else
                  f"    {k:12s}      -")

    @staticmethod
    def cmd_calibrate_fatigue(argv) -> None:
        """fit the opening penalty"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py calibrate-fatigue")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--reps", type=int, default=12)
        ap.add_argument("--workers", type=int, default=None)
        a = ap.parse_args(argv[1:])
        SlateCalibration.calibrate_fatigue(a.season, reps=a.reps, workers=a.workers)

    @staticmethod
    def cmd_asof(argv) -> None:
        """cache AS-OF boards, leak-free"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py asof")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--every", type=int, default=7,
                        help="cutoff spacing in days (7 = weekly, ~26 fetches "
                             "per side per season)")
        ap.add_argument("--start", default=None)
        ap.add_argument("--end", default=None)
        ap.add_argument("--force", action="store_true")
        a = ap.parse_args(argv[1:])
        d0 = datetime.date.fromisoformat(a.start or f"{a.season}-04-07")
        d1 = datetime.date.fromisoformat(
            a.end or min(datetime.date.today(),
                         datetime.date(a.season, 10, 1)).isoformat())
        dates, cur = [], d0
        while cur <= d1:
            dates.append(cur.isoformat())
            cur += datetime.timedelta(days=a.every)
        print(f"as-of boards: {len(dates)} cutoffs, {dates[0]}..{dates[-1]} "
              f"(needs headless Firefox; /api/leaders 403s a plain request)")
        got = Boards.fetch_boards_asof(dates, a.season, force=a.force)
        print(f"\nfetched {len(got)}; cached under {ASOF_DIR}")

    @staticmethod
    def cmd_backtest(argv) -> None:
        """replay a season on frozen rates"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py backtest")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--reps", type=int, default=60)
        ap.add_argument("--seed", type=int, default=17)
        ap.add_argument("--limit", type=int, default=None)
        ap.add_argument("--workers", type=int, default=None)
        a = ap.parse_args(argv[1:])
        bt = backtest(a.season, reps=a.reps, seed=a.seed, limit=a.limit,
                      workers=a.workers)
        sc = score_backtest(bt)
        print(f"\nBACKTEST {a.season} — rates frozen STRICTLY BEFORE each "
              f"game date ({len(bt['cutoffs'])} cutoffs)")
        print(f"  {sc['n']} games, {a.reps} sims each\n")
        print(f"  total   model {sc['model_mean_total']:.3f}   "
              f"actual {sc['actual_mean_total']:.3f}   "
              f"bias {sc['total_bias']:+.3f}")
        print(f"          corr {sc['total_corr']:+.4f}   "
              f"RMSE {sc['total_rmse']:.3f}")
        # Scored against BASEBALL above and against a BOOK below: runs are
        # right-skewed, so the total where P(over)=0.5 sits below the mean and
        # comparing it with an actual mean invents a bias of exactly the skew.
        print(f"  line    implied {sc['model_implied_line']:.3f}   "
              f"skew {sc['skew']:+.3f}   "
              f"(vs an actual MEAN this reads {sc['line_bias']:+.3f} — "
              f"a book's total is a median, baseball's is not)")
        print(f"  home    model {sc['model_home_win']:.4f}   "
              f"actual {sc['actual_home_win']:.4f}   "
              f"bias {sc['ml_bias']:+.4f}   corr {sc['ml_corr']:+.4f}")
        print("\n  Still season-final and NOT frozen: Savant OAA and framing "
              "(their\n  leaderboards ignore date parameters), the "
              "insidethepen pen, and the\n  fitted constants (GAME_FORM_SD, "
              "FRAMING_TILT_SCALE, PARK_RUN_RELIABILITY,\n  the playing-time "
              "prior's shape).")

    @staticmethod
    def cmd_closing(argv) -> None:
        """model vs the DE-VIGGED close"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py closing")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--reps", type=int, default=60)
        ap.add_argument("--seed", type=int, default=17)
        ap.add_argument("--edge", type=float, default=0.03)
        ap.add_argument("--price", choices=("avg", "max"), default="avg")
        ap.add_argument("--workers", type=int, default=None)
        a = ap.parse_args(argv[1:])
        bt = backtest(a.season, reps=a.reps, seed=a.seed, workers=a.workers)
        sc = score_backtest(bt)
        r = ClosingScore.clv_vs_closing(bt, a.season, edge=a.edge, price=a.price)
        print(f"\nMODEL vs the DE-VIGGED CLOSING LINE — {a.season}")
        print(f"  {r['matched']} of {r['n_games']} backtested games matched to "
              f"a closing moneyline ({a.price} of book)"
              + (f"; {r['mismatched']} rejected on a SCORE mismatch"
                 if r["mismatched"] else "; every match verified by score"))
        # The bias line comes FIRST, deliberately: a standing side tilt shows up
        # as edge on every game and would be read as a signal.
        print(f"  standing ML bias {sc['ml_bias']:+.4f} "
              f"(model {sc['model_home_win']:.4f} vs actual "
              f"{sc['actual_home_win']:.4f}) — read this before the edge")
        print(f"  Monte Carlo se on p_home at {r['reps']} sims: "
              f"{r['mc_se']:.4f}"
              + ("  ** larger than the edge threshold: the filter is mostly "
                 "selecting SIM NOISE, which dilutes ROI toward zero and "
                 "flattens the buckets. Raise --reps. **"
                 if r["mc_se"] > a.edge else "  (below the threshold)"))

        def show(label, s):
            if not s.get("n"):
                print(f"  {label:22s} no picks")
                return
            print(f"  {label:22s} n {s['n']:5d}   hit {s['hit']:.4f}   "
                  f"mkt fair {s['mkt_fair']:.4f}   "
                  f"breakeven {s['mkt_implied']:.4f}   "
                  f"model said {s['model_implied']:.4f}   "
                  f"ROI {s['roi']:+.4f} +/- {s['roi_se']:.4f}  "
                  f"(t {s['t']:+.2f})")

        print()
        show("ALL games", r["all"])
        show(f"edge > {a.edge:.0%}", r["filtered"])
        print("\n  by disagreement with the close — a real edge GROWS with it;"
              "\n  a flat profile with one good bucket is what noise looks like")
        for b in r["buckets"]:
            print(f"    {b['lo']:.0%}-{b['hi']:.0%}  n {b['n']:5d}   "
                  f"hit {b['hit']:.4f}   mkt fair {b['mkt_fair']:.4f}   "
                  f"model {b['model_implied']:.4f}   "
                  f"ROI {b['roi']:+.4f} +/- {b['roi_se']:.4f}")

    @staticmethod
    def cmd_forecastwx(argv) -> None:
        """PERIOD-CORRECT weather (5b.2)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py forecastwx")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--lag", type=int, default=WEATHER_FORECAST_LAG_DAYS,
                        help="days before first pitch the forecast was issued "
                             "(1 matches when the opening price is hung)")
        a = ap.parse_args(argv[1:])
        for season in (a.season or [2025, 2026]):
            fetch_forecast_weather(season, lag_days=a.lag)
            # the matched reference is always built off DAY 0 — climatology,
            # not information — so it centres the day-1 arm without leaking
            if load_forecast_weather(season, 0):
                ParkFactors.build_park_weather_ref_om(season, 0)

    @staticmethod
    def cmd_clvopen(argv) -> None:
        """model vs the OPENING line (CLV)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py clvopen")
        ap.add_argument("--season", action="append", type=int, default=None,
                        help="repeatable; default is both 2025 and 2026")
        ap.add_argument("--reps", type=int, default=2000)
        ap.add_argument("--arm", action="append", default=None,
                        help="repeatable; each arm is scored through the same "
                             "harness and compared. Default base. The 3d.12 "
                             "look-ahead ablation is "
                             "--arm base --arm nowx --arm nolineup --arm nolook")
        ap.add_argument("--min-books", type=int, default=MIN_BOOKS_FOR_CLV_OPEN)
        ap.add_argument("--fresh", action="store_true",
                        help="re-run the backtest instead of using the cache")
        ap.add_argument("--workers", type=int, default=None)
        a = ap.parse_args(argv[1:])
        seasons = a.season or [2025, 2026]
        arms = a.arm or ["base"]
        # {arm: (pooled moneyline rows, pooled totals rows)}
        by_arm: Dict[str, Tuple[List[dict], List[dict]]] = {
            k: ([], []) for k in arms}
        for arm_name in arms:
          pooled_ml, pooled_tot = by_arm[arm_name]
          for season in seasons:
            bt = ab_run_arm(season, arm_name, a.reps, fresh=a.fresh,
                            workers=a.workers)
            r = clv_vs_opening(bt, season, min_books=a.min_books)
            ml, tot = r["moneyline"], r["totals"]
            pooled_ml += ml
            pooled_tot += tot
            print(f"\nMODEL vs the OPENING line — {season}  (arm {arm_name!r}, "
                  f"{r['matched']} of {r['n_games']} games matched"
                  + (f", {r['mismatched']} rejected on a SCORE mismatch"
                     if r["mismatched"] else ", every match verified by score")
                  + (f", {r['no_event']} with no per-event odds"
                     if r["no_event"] else "") + ")")
            if r["open_lag_days"] is not None:
                print(f"  the opening price is hung a median "
                      f"{r['open_lag_days']:.2f} days before first pitch; "
                      f"{(r['open_after_cutoff'] or 0):.1%} of openers came "
                      f"AFTER our board cutoff")
                print("  (an opener hung AFTER our cutoff had access to "
                      "everything the model saw and\n   a day more besides, so "
                      "that share is the HARDER comparison, not the easier "
                      "one)")

            def show(label, s, unit="p"):
                if not s.get("n"):
                    print(f"    {label:26s} no picks")
                    return
                u = "runs" if unit == "runs" else ""
                print(f"    {label:26s} n {s['n']:5d}   "
                      f"CLV {s['clv']:+.5f} {u:4s} +/- {s['se']:.5f}  "
                      f"(t {s['t']:+.2f})   moved our way {s['hit']:.4f}")

            print("\n  MONEYLINE — CLV in de-vigged probability")
            show("all picks", OpeningClv._clv_summary(ml))
            print("\n  TOTALS — CLV in probability at the opening line, "
                  "and in RUNS")
            show("all picks", OpeningClv._clv_summary([t for t in tot if t["priced"]]))
            show("line move", OpeningClv._clv_summary(tot, "clv_runs"), unit="runs")

          if len(seasons) > 1 and pooled_ml:
            print(f"\n  {arm_name.upper()} POOLED  n {len(pooled_ml)} "
                  f"moneyline / {len(pooled_tot)} totals")

            def show2(label, s, unit="p"):
                if not s.get("n"):
                    return
                u = "runs" if unit == "runs" else ""
                print(f"    {label:26s} n {s['n']:5d}   "
                      f"CLV {s['clv']:+.5f} {u:4s} +/- {s['se']:.5f}  "
                      f"(t {s['t']:+.2f})   moved our way {s['hit']:.4f}")
            show2("MONEYLINE", OpeningClv._clv_summary(pooled_ml))
            show2("TOTALS", OpeningClv._clv_summary([t for t in pooled_tot if t["priced"]]))
            show2("TOTALS line move",
                  OpeningClv._clv_summary(pooled_tot, "clv_runs"), unit="runs")

            # What the de-vig is worth, shown rather than claimed. The raw row
            # is what this test would have reported without it.
            vr = OpeningClv.vig_report(pooled_ml)
            if vr.get("n"):
                print(f"\n  the DE-VIG, demonstrated on the same {vr['n']} "
                      f"moneyline picks:")
                print(f"    overround   open {vr['open_overround']:.4f}  ->  "
                      f"close {vr['close_overround']:.4f}   "
                      f"(it tightens by "
                      f"{vr['open_overround'] - vr['close_overround']:+.4f})")
                print(f"    CLV on RAW implied probabilities "
                      f"{vr['raw']['clv']:+.5f}  (t {vr['raw']['t']:+.2f})"
                      f"  <- what this would have reported")
                print(f"    CLV de-vigged                    "
                      f"{vr['devigged']['clv']:+.5f}  "
                      f"(t {vr['devigged']['t']:+.2f})  <- the honest number")
            print("\n  by disagreement with the OPEN — a real edge GROWS with "
                  "it. A flat profile\n  with one good bucket is noise, "
                  "however good that bucket looks (3d.1).")
            print(f"    {'moneyline':14s} {'n':>6s} {'CLV':>10s} {'se':>9s} "
                  f"{'t':>7s} {'our way':>9s}")
            for b in OpeningClv.clv_open_buckets(pooled_ml):
                if not b["n"]:
                    continue
                print(f"    {b['lo']:.0%}-{b['hi']:.0%}".ljust(18)
                      + f"{b['n']:6d} {b['clv']:+10.5f} {b['se']:9.5f} "
                      f"{b['t']:+7.2f} {b['hit']:9.4f}")

        # --- the ablation table: every arm through the SAME harness --------
        if len(by_arm) > 1:
            print("\n\n  THE LOOK-AHEAD ABLATION — same games, same seeds, "
                  "same scorer.\n  `base` holds the posted lineup and the "
                  "OBSERVED game-time weather; the opening\n  price had "
                  "neither. What survives in `nolook` is the part of the CLV "
                  "a\n  pre-lineup, pre-weather projection actually earned.")
            print(f"\n    {'arm':10s} {'ML CLV':>10s} {'t':>7s} "
                  f"{'TOT CLV':>10s} {'t':>7s} {'line runs':>10s} {'t':>7s}")
            ref = None
            for k, (mrows, trows) in by_arm.items():
                a1 = OpeningClv._clv_summary(mrows)
                a2 = OpeningClv._clv_summary([t for t in trows if t["priced"]])
                a3 = OpeningClv._clv_summary(trows, "clv_runs")
                if not a1.get("n"):
                    continue
                print(f"    {k:10s} {a1['clv']:+10.5f} {a1['t']:+7.2f} "
                      f"{a2['clv']:+10.5f} {a2['t']:+7.2f} "
                      f"{a3['clv']:+10.5f} {a3['t']:+7.2f}")
                if ref is None:
                    ref = (a1["clv"], a2["clv"], a3["clv"])
            if ref and "nolook" in by_arm:
                nl = by_arm["nolook"][0]
                nt = [t for t in by_arm["nolook"][1] if t["priced"]]
                s1 = OpeningClv._clv_summary(nl)["clv"] / ref[0] if ref[0] else float("nan")
                s2 = OpeningClv._clv_summary(nt)["clv"] / ref[1] if ref[1] else float("nan")
                print(f"\n    share of the CLV that SURVIVES the ablation: "
                      f"moneyline {s1:.1%}, totals {s2:.1%}")

            # Two arms that agree to the last digit did not run (section 8).
            base_rows = by_arm.get(arms[0], ([], []))[0]
            bk = {(r["pk"], r["date"]): r["model_home"] for r in base_rows}
            for k, (mrows, _t) in by_arm.items():
                if k == arms[0] or not mrows:
                    continue
                shared = [r for r in mrows if (r["pk"], r["date"]) in bk]
                if shared and all(
                        r["model_home"] == bk[(r["pk"], r["date"])]
                        for r in shared):
                    print(f"    ** {k} is IDENTICAL to {arms[0]} on every "
                          f"game. The ablation did not run. **")

        print("\n  CLV needs no game result, so its error bar is the one "
              "quoted — but it is NOT\n  an edge on its own: a market can "
              "move toward a model and still be right.")

    @staticmethod
    def cmd_stuff(argv) -> None:
        """pitch-model REPEATABILITY (3d.8)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py stuff")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--no-score", action="store_true",
                        help="reliability only; skip the (slow) A/B against "
                             "the incumbent's predictions")
        a = ap.parse_args(argv[1:])
        seasons = a.season or [2025, 2026]
        names = ("K", "BB", "HBP", "GB_OUT", "AIR_OUT", "1B", "2B", "3B", "HR")
        for season in seasons:
            r = Stuff.measure_stuff_reliability(season)
            print(f"\nSTUFF REPEATABILITY — {season}: {r['n']} pitcher-cutoff "
                  f"pairs" + (f", model fit on {r.get('fit_seasons')}"
                              if r.get("fit_seasons") else ""))
            if r["n"] < 30:
                print("  not enough data — are the as-of boards cached?")
                continue
            eff = stuff_stabilize(STABILIZE_PA_PIT, r["rho2"])
            print(f"  {'outcome':9s} {'corr(own)':>10s} {'corr(stuff)':>12s} "
                  f"{'rho2':>8s} {'M':>8s} {'M_eff':>8s}")
            for i in range(N_OUTCOMES):
                print(f"  {names[i]:9s} {r['corr_own'][i] or 0.0:+10.3f} "
                      f"{r['corr_delta'][i] or 0.0:+12.3f} "
                      f"{r['rho2'][i]:8.3f} {STABILIZE_PA_PIT[i]:8.0f} "
                      f"{eff[i]:8.0f}")
            print("  corr is against what he did AFTER the cutoff, so neither "
                  "predictor\n  shares a sampling error with the target. "
                  "SHIPPED rho2 is the MINIMUM\n  across seasons, not the "
                  "mean — over-trusting is this file's failure mode.")
            if a.no_score:
                continue
            s = Stuff.score_stuff_prior(season)
            if s["n"] < 30:
                continue
            print(f"\n  predicting the REST of his season, n {s['n']} "
                  f"(the incumbent is the SHIPPED shrunk blend, not league)")
            keys = ("league", "own", "incumbent", "stuff")
            print(f"  {'outcome':9s}" + "".join(f"{k:>12s}" for k in keys))
            for i in range(N_OUTCOMES):
                print(f"  {names[i]:9s}" +
                      "".join(f"{s[k]['rmse'][i]:12.5f}" for k in keys))
            print(f"  {'RV rmse':9s}" +
                  "".join(f"{s[k]['rv_rmse']:12.5f}" for k in keys))
            print(f"  {'RV corr':9s}" +
                  "".join(f"{s[k]['rv_corr'] or 0.0:+12.4f}" for k in keys))

    @staticmethod
    def cmd_bmielke(argv) -> None:
        """BMIELKE as a hitter prior — gate coverage, then the prediction A/B (17e)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py bmielke")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--no-score", action="store_true",
                        help="coverage only; skip the (slow) A/B against the "
                             "incumbent's predictions")
        a = ap.parse_args(argv[1:])
        seasons = a.season or [2025, 2026]
        names = ("K", "BB", "HBP", "GB_OUT", "AIR_OUT", "1B", "2B", "3B", "HR")
        for season in seasons:
            board = load_board("bat", season) or []
            pids = [p for r in board if (p := _row_id(r)) is not None]
            lv = bmielke_levels(pids, season)
            n_read = sum(1 for p in pids if bmielke_asof(p, season))
            # **Print the gate that RAN, not the metric's crossover.** They
            # differ deliberately (see `BMIELKE_GATE_BBE`) and a banner naming
            # the wrong one is the §2d defect exactly: a printed number that
            # does not describe what executed.
            print(f"\nBMIELKE — {season}: {len(pids)} board rows, "
                  f"{n_read} with a reading, {len(lv)} INSIDE the gate "
                  f"(<= {BMIELKE_GATE_BBE} balls in play; the metric's own "
                  f"crossover against xwOBAcon is {BMIELKE_MAX_BBE})")
            if not lv:
                print("  nothing gated — is savedata/bmielke populated? "
                      "Bmielke.fetch_bmielke_season()")
                continue
            vals = sorted(v for v, _ in lv.values())
            bbe = sorted(n for _, n in lv.values())
            print(f"  level  p05 {vals[len(vals)//20]:.4f}  "
                  f"median {vals[len(vals)//2]:.4f}  "
                  f"p95 {vals[-max(len(vals)//20, 1)]:.4f}   (1.0 = the "
                  f"gated population's average, by construction)")
            print(f"  balls in play  min {bbe[0]}  median {bbe[len(bbe)//2]}  "
                  f"max {bbe[-1]}")
            if a.no_score:
                continue
            sc = Bm.score_bmielke_prior(season)
            if sc["n"] < 30:
                print("  not enough as-of pairs to score")
                continue
            print(f"\n  predicting the REST of his season, n {sc['n']}, "
                  f"{sc['n_moved']} of them actually moved by the prior")
            keys = ("league", "own", "incumbent", "bmielke")
            for tag in ("all", "gated"):
                if tag not in sc:
                    continue
                print(f"\n  --- {tag.upper()} (n {sc[tag]['n']}) --- "
                      f"incumbent is the SHIPPED shrunk blend, not league")
                print(f"  {'outcome':9s}" + "".join(f"{k:>12s}" for k in keys))
                for i in range(N_OUTCOMES):
                    print(f"  {names[i]:9s}" +
                          "".join(f"{sc[tag][k]['rmse'][i]:12.5f}"
                                  for k in keys))
                print(f"  {'RV rmse':9s}" +
                      "".join(f"{sc[tag][k]['rv_rmse']:12.5f}" for k in keys))
                print(f"  {'RV corr':9s}" +
                      "".join(f"{sc[tag][k]['rv_corr'] or 0.0:+12.4f}"
                              for k in keys))
            print("\n  READ THE GATED BLOCK. The `all` block dilutes the term "
                  "with hitters\n  it declined to touch, and a diluted null "
                  "is indistinguishable from a real one.")

    @staticmethod
    def cmd_bmaudit(argv) -> None:
        """which hitters BMIELKE boosts, and whether the SWING backs it (17e)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py bmaudit")
        ap.add_argument("--season", type=int, default=CURRENT_SEASON)
        ap.add_argument("--top", type=int, default=20)
        ap.add_argument("--min-share", type=float, default=0.40,
                        help="flag a boost whose swing share falls below this")
        a = ap.parse_args(argv[1:])
        season = a.season
        board = load_board("bat", season) or []
        name, pa = {}, {}
        for r in board:
            pid = _row_id(r)
            if pid is None:
                continue
            name[pid] = r.get("PlayerName") or str(pid)
            pa[pid] = outcome_counts(r, "bat")[1]
        lv = bmielke_levels(list(pa), season)
        if not lv:
            print("no gated hitters — is savedata/bmielke populated?")
            return
        global USE_BMIELKE_PRIOR
        was = USE_BMIELKE_PRIOR
        try:
            USE_BMIELKE_PRIOR = False
            off, _ = build_rates("bat", [season])
            USE_BMIELKE_PRIOR = True
            on, _ = build_rates("bat", [season])
        finally:
            USE_BMIELKE_PRIOR = was
        rows = []
        for pid, (level, bbe) in lv.items():
            if pid not in off or pid not in on:
                continue
            sup = Bm.bmielke_support(pid, season)
            if not sup:
                continue
            d = (rate_run_value(on[pid]["rates"])
                 - rate_run_value(off[pid]["rates"]))
            rows.append((name[pid], pa.get(pid, 0), bbe, d, level, sup))
        rows.sort(key=lambda r: -abs(r[3]))
        print(f"\nBMIELKE AUDIT — {season}: {len(rows)} gated hitters, "
              f"largest {a.top} moves\n"
              f"  swing share = how much of the reading is BAT SPEED / ATTACK "
              f"ANGLE / WHIFF\n  rather than the hitter's OWN xwOBAcon and "
              f"hardest-hit ball (see Bm.bmielke_support)\n")
        print(f"  {'hitter':22s}{'PA':>5}{'BBE':>5}{'runs/PA':>9}{'level':>7}"
              f"{'swing%':>8}{'fast%':>7}{'EV98':>7}  flag")
        for n_, p, b, d, level, sup in rows[:a.top]:
            flag = "" if sup["swing_share"] >= a.min_share else "OWN-CONTACT"
            print(f"  {n_[:21]:22s}{p:5.0f}{b:5d}{d:+9.5f}{level:7.3f}"
                  f"{sup['swing_share']*100:7.0f}%{sup['fastsw']*100:6.0f}%"
                  f"{sup['evmax']:7.1f}  {flag}")
        weak = [r for r in rows if r[5]["swing_share"] < a.min_share
                and abs(r[3]) > 0.010]
        print(f"\n  {len(weak)} of {len(rows)} hitters move more than 0.010 "
              f"runs/PA on a reading\n  the swing does NOT mostly back. Those "
              f"are the ones to distrust: the\n  rate layer is already "
              f"shrinking the same batted balls.")

    @staticmethod
    def cmd_diff(argv) -> None:
        """score on the RUN DIFFERENTIAL (4f)"""
        ap = argparse.ArgumentParser(
            prog="mlb_sim.py diff",
            description="Score an arm on the RUN DIFFERENTIAL against the "
                        "Asian-handicap ladder and the actual results. The "
                        "closing TOTAL cannot see a defect that moves the two "
                        "clubs in opposite directions; this can.")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--arm", action="append", default=None)
        ap.add_argument("--reps", type=int, default=2000)
        ap.add_argument("--fresh", action="store_true")
        ap.add_argument("--workers", type=int, default=None)
        ap.add_argument("--check", action="store_true",
                        help="print the ladder decode proof and stop")
        a = ap.parse_args(argv[1:])
        seasons = a.season or [2025, 2026]
        if a.check:
            for season in seasons:
                r = ladder_report(season)
                print(f"\nladder {season}: {r['games']} games   "
                      f"monotonicity {r['monotone_violations']}/"
                      f"{r['monotone_pairs']}   moneyline bracketed "
                      f"{r['bracket_checked'] - r['bracket_violations']}/"
                      f"{r['bracket_checked']}")
                print(f"  rung coverage P(D>=m): {r['rung_coverage']}")
            return
        for arm_name in (a.arm or ["base"]):
            pooled = []
            for season in seasons:
                bt = ab_run_arm(season, arm_name, a.reps, fresh=a.fresh,
                                workers=a.workers)
                rows = Differential.differential_rows(bt, season)
                Differential.print_differential(Differential.score_differential(rows),
                                   f"{arm_name} {season}")
                pooled += rows
            if len(seasons) > 1:
                Differential.print_differential(Differential.score_differential(pooled),
                                   f"{arm_name} POOLED")

    @staticmethod
    def cmd_ab(argv) -> None:
        """A/B a change vs the CLOSE (3d)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py ab")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--reps", type=int, default=2000,
                        help="sims per game; at 40 the Monte Carlo se on "
                             "p_home is 0.079 and swamps what is measured")
        ap.add_argument("--workers", type=int, default=None)
        ap.add_argument("--fresh", action="store_true",
                        help="discard cached arms — REQUIRED after any "
                             "rate-layer change, or a new arm is compared "
                             "against one built by the old code")
        ap.add_argument("--score-only", action="store_true",
                        help="re-score cached arms without simulating")
        ap.add_argument("--arm", action="append", default=None,
                        help="repeatable; restrict to these arms. Without it "
                             "every arm in AB_ARMS is built, which means one "
                             "uncached arm costs a full run to look at an "
                             "unrelated question.")
        a = ap.parse_args(argv[1:])
        seasons = a.season or [2026, 2025]
        arms = list(a.arm) if a.arm else list(AB_ARMS)
        bad = [x for x in arms if x not in AB_ARMS]
        if bad:
            raise SystemExit(f"mlb_sim: unknown arm(s) {bad}; "
                             f"have {list(AB_ARMS)}")
        print(f"A/B {arms} x {seasons} at {a.reps} sims/game, "
              f"leak-free (team context lagged, framing ablated)")
        print(f"  progress also appends to {PROGRESS_LOG} — tail -f it")
        by_season: Dict[int, Dict[str, dict]] = {}
        for season in seasons:
            got: Dict[str, dict] = {}
            # HISTORICAL arms first, and only when actually on disk: they price
            # a CODE change that no flag can toggle. Skipped silently when
            # absent, because a missing artifact is not an error.
            for name, why in AB_REFERENCE.items():
                p = AB_DIR / f"bt{season}_{name}_{a.reps}.json"
                if p.exists():
                    with open(p) as fh:
                        got[name] = json.load(fh)
                    print(f"  {season} {name:20s} reference (never re-run) — "
                          f"{why.split('.')[0]}")
            for name in arms:
                if a.score_only:
                    p = AB_DIR / f"bt{season}_{name}_{a.reps}.json"
                    if not p.exists():
                        raise SystemExit(f"mlb_sim: {p} not cached")
                    with open(p) as fh:
                        got[name] = json.load(fh)
                else:
                    got[name] = ab_run_arm(season, name, a.reps, a.fresh,
                                           workers=a.workers)
            by_season[season] = got
        ab_score(by_season)

    @staticmethod
    def cmd_eventodds(argv) -> None:
        """OPENING odds + TOTALS per event"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py eventodds")
        ap.add_argument("--season", action="append", type=int, default=None,
                        help="repeatable; runs them in order in ONE process, "
                             "because a queued wrapper is a process that can "
                             "be killed out from under the queue")
        ap.add_argument("--limit", type=int, default=None,
                        help="stop after N events (for a smoke run)")
        ap.add_argument("--workers", type=int, default=8)
        ap.add_argument("--timeout", type=float, default=25.0)
        a = ap.parse_args(argv[1:])
        print("per-event OPENING + CLOSING odds: moneyline, totals, run line, "
              "and the first-five-innings scopes.\n"
              "  ~17s/event per worker; resumable by event id, so an "
              "interrupted run costs nothing.\n"
              "  needs a non-US egress IP — a US one returns zero outcomes "
              "while the event page still resolves.")
        seasons = a.season or [2026]
        got = {}
        for season in seasons:
            got = EventOdds.fetch_event_odds(season, limit=a.limit, workers=a.workers,
                                   timeout=a.timeout)
        # what did we actually get? A count of games is not a count of markets.
        # Reported for the LAST season fetched; each season prints its own
        # cached-vs-expected line as it finishes.
        n_tot = n_f5 = n_ml = 0
        for e in got.values():
            if EventOdds.event_totals(e, 1):
                n_tot += 1
            if EventOdds.event_totals(e, 2):
                n_f5 += 1
            if any(l.get("bt") == 3 and l.get("sc", 1) == 1
                   for l in e.get("lines", [])):
                n_ml += 1
        print(f"\n  of {len(got)} cached events: {n_ml} with a moneyline, "
              f"{n_tot} with whole-game totals, {n_f5} with F5 totals")

    @staticmethod
    def cmd_recency(argv) -> None:
        """within-season recency (3d.9)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py recency")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--side", action="append", choices=("bat", "pit"),
                        default=None)
        a = ap.parse_args(argv[1:])
        hl = (150.0, 250.0, 500.0, 1000.0)
        for side in (a.side or ["pit", "bat"]):
            for season in (a.season or [2025, 2026]):
                r = Stuff.measure_recency(side, season, hl)
                print(f"\nWITHIN-SEASON RECENCY — {side} {season}: "
                      f"n {r['n']} player-cutoff pairs, effective sample "
                      f"{r['eff_share']:.0%} of raw at hl={hl[0]:.0f}")
                if r["n"] < 30:
                    print("  not enough data — are the as-of boards cached?")
                    continue
                keys = r["names"]
                print("  " + " " * 12 + "".join(f"{k:>10s}" for k in keys))
                for lab in ("raw", "shrunk"):
                    print(f"  {lab:6s} rmse" +
                          "".join(f"{r[lab][k]['rmse']:10.5f}" for k in keys))
                    print(f"  {'':6s} corr" +
                          "".join(f"{r[lab][k]['corr'] or 0.0:+10.4f}"
                                  for k in keys))
                print("  paired on `shrunk`, + = recency better, CLUSTERED BY "
                      "PLAYER —\n  one player contributes a row per cutoff and "
                      "those rows are not independent:")
                for k in keys[1:]:
                    p = r["paired"][k]
                    print(f"    {k:8s} {p['players']:4d} players  "
                          f"{p['mean']:+.6f} +/- {p['se']:.6f}  "
                          f"(t {p['t']:+.2f})")

    @staticmethod
    def cmd_stints(argv) -> None:
        """relief-appearance shape"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py stints")
        ap.add_argument("--games", type=int, default=400)
        ap.add_argument("--refresh", action="store_true",
                        help="re-scrape the play-by-play (1,850 games)")
        a = ap.parse_args(argv[1:])
        if a.refresh:
            RelieverUsage.collect_reliever_stints(refresh=True)
        r = validate_stint_shape(a.games)
        print(f"relief-appearance shape — {r['games']} simulated games "
              f"against {r['real']['n']} real appearances\n")
        print(f"  {'':22s} {'sim':>8s} {'real':>8s}")
        for k, lbl in (("apps_per_team_game", "appearances/team-game"),
                       ("bf", "batters faced"),
                       ("outs", "outs recorded"),
                       ("innings", "innings touched"),
                       ("mid_entry", "entered mid-inning"),
                       ("multi_inning", "2+ innings touched")):
            print(f"  {lbl:22s} {r['sim'][k]:8.3f} {r['real'][k]:8.3f}")
        print(f"\n  {'innings':10s}" + "".join(f"{i:>8d}" for i in range(1, 5)))
        for k in ("sim", "real"):
            print(f"  {k:10s}" + "".join(
                f"{r[k]['by_innings'].get(i, 0.0):8.3f}" for i in range(1, 5)))

    @staticmethod
    def cmd_aaa(argv) -> None:
        """AAA->MLB translation, fitted"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py aaa")
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        if a.refresh:
            MiLB.measure_milb_translation()
        MiLB.aaa_translation_report()

    @staticmethod
    def cmd_parkbuild(argv) -> None:
        """per-outcome park factors + exposure"""
        ap = argparse.ArgumentParser(
            prog="mlb_sim.py parkbuild",
            description="Per-outcome park factors and each player's measured "
                        "park exposure. Both read savedata/pa/v2/ and the "
                        "season slate; USE_PARK_DECONTAM needs both.")
        ap.add_argument("seasons", nargs="*", type=int, default=None)
        ap.add_argument("--reliability", type=float, default=0.70)
        a = ap.parse_args(argv[1:])
        for season in (a.seasons or [2023, 2024, 2025, 2026]):
            fac = ParkFactors.build_park_outcome_factors(season, a.reliability)
            fp = park_outcome_path(season)
            with open(fp, "w") as fh:
                json.dump(fac, fh, indent=1)
            exp = ParkFactors.build_player_park_exposure(season)
            xp = Path(SAVE_DIR) / f"player_park_exposure_{season}.json"
            with open(xp, "w") as fh:
                json.dump(exp, fh)
            mt = exp["_meta"]
            print(f"{season}: {len(fac)} parks -> {fp.name};  "
                  f"bat {mt['n_bat']:5d} pit {mt['n_pit']:5d} "
                  f"(PA w/o venue {mt['pa_without_venue']}) -> {xp.name}")
            if fac:
                hot = sorted(fac, key=lambda k: -fac[k]["factor"][HR])[:2]
                cold = sorted(fac, key=lambda k: fac[k]["factor"][HR])[:2]
                for v in hot + cold:
                    print(f"    {v:26s} HR {fac[v]['factor'][HR]:.3f}  "
                          f"3B {fac[v]['factor'][S3B]:.3f}")

    @staticmethod
    def cmd_milb(argv) -> None:
        """minor league lines + AAA arsenal (9c)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py milb")
        ap.add_argument("seasons", nargs="*", type=int, default=None)
        ap.add_argument("--refresh", action="store_true")
        ap.add_argument("--no-statcast", action="store_true",
                        help="skip the Triple-A Statcast arsenal pass")
        ap.add_argument("--statcast-only", action="store_true",
                        help="only the arsenal pass, reusing cached lines")
        a = ap.parse_args(argv[1:])
        seasons = a.seasons or [2026]
        if not a.statcast_only:
            MiLB.collect_milb(seasons, refresh=a.refresh)
        # Same players, same season, same cache file — see
        # `collect_milb_statcast`. Hawk-Eye is Triple-A and FSL only; a
        # Double-A arm is untouched by this and stays on the level ladder.
        if not a.no_statcast:
            MiLB.collect_milb_statcast(seasons, refresh=a.refresh or a.statcast_only)
        MiLB.milb_report(max(seasons))

    @staticmethod
    def cmd_framing(argv) -> None:
        """rebuild framing from PITCH level (9d)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py framing")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--upto", default=None,
                        help="only games on/before this date (as-of)")
        ap.add_argument("--no-pitcher", action="store_true")
        ap.add_argument("--no-umpire", action="store_true")
        ap.add_argument("--repeatability", action="store_true",
                        help="SPLIT-HALF: does the pitcher/umpire adjustment "
                             "give a better catcher estimate?")
        ap.add_argument("--split", default=None, help="split-half date")
        ap.add_argument("--validate", action="store_true",
                        help="score against MLBAnalytics/team_framing_<season>.csv")
        ap.add_argument("--out", default=None)
        a = ap.parse_args(argv[1:])
        Framing.measure_framing(a.season, a.upto, with_pitcher=not a.no_pitcher,
                        with_umpire=not a.no_umpire, out_path=a.out)
        if a.validate:
            Framing.framing_validate_report(a.season, a.out)
        if a.repeatability:
            Framing.framing_repeatability_report(a.season, a.split)

    @staticmethod
    def cmd_milbpark(argv) -> None:
        """AAA PARK factors, per outcome"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py milbpark")
        ap.add_argument("seasons", nargs="*", type=int, default=None)
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        seasons = a.seasons or [2024, 2025, 2026]
        MiLB.collect_milb_park(seasons, refresh=a.refresh)
        MiLB.milb_park_report(max(seasons))

    @staticmethod
    def cmd_milbasof(argv) -> None:
        """AS-OF AAA snapshots (5.11.1)"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py milbasof")
        ap.add_argument("seasons", nargs="*", type=int, default=None)
        ap.add_argument("--force", action="store_true")
        a = ap.parse_args(argv[1:])
        # The cutoff grid is the BOARDS', never an independent one: two grids
        # would let the Triple-A line be fresher than the major league board
        # beside it, which is the leak this is here to close.
        for season in (a.seasons or [2026]):
            cuts = available_asof_cutoffs(season)
            if not cuts:
                print(f"[milb-asof] {season}: no as-of boards cached — "
                      f"run `python mlb_sim.py asof --season {season}` first")
                continue
            print(f"[milb-asof] {season}: {len(cuts)} cutoffs, "
                  f"{cuts[0]}..{cuts[-1]}")
            MiLB.collect_milb_asof(cuts, season, force=a.force)

    @staticmethod
    def cmd_baserunning(argv) -> None:
        """measured advancement rates"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py baserunning")
        ap.add_argument("--season", type=int, default=BASERUN_SEASON)
        ap.add_argument("--refresh", action="store_true",
                        help="re-scrape the play-by-play (a full season)")
        ap.add_argument("--games", type=int, default=0,
                        help="only the last N games (a smoke test, not a fit)")
        ap.add_argument("--workers", type=int, default=12)
        a = ap.parse_args(argv[1:])
        if a.refresh:
            collect_baserunning(a.season, workers=a.workers, refresh=True,
                                n_games=a.games)
        BaseRunningPbp.baserunning_report(a.season, workers=a.workers)

    @staticmethod
    def cmd_re24(argv) -> None:
        """run expectancy vs measured"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py re24")
        ap.add_argument("--season", type=int, default=BASERUN_SEASON)
        ap.add_argument("--games", type=int, default=6000)
        a = ap.parse_args(argv[1:])
        BaseRunningPbp.re24_report(a.games, season=a.season)

    @staticmethod
    def cmd_boards(argv) -> None:
        """fetch FULL-SEASON boards"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py boards")
        ap.add_argument("seasons", nargs="+", type=int)
        ap.add_argument("--side", action="append", choices=("bat", "pit"),
                        default=None)
        ap.add_argument("--force", action="store_true")
        a = ap.parse_args(argv[1:])
        sides = tuple(a.side) if a.side else ("bat", "pit")
        print(f"full-season boards: {sides} x {a.seasons} "
              f"(needs headless Firefox)")
        got = Boards.fetch_season_boards(a.seasons, sides, force=a.force)
        print(f"\nfetched {len(got)}; cached under {SAVE_DIR}")

    @staticmethod
    def cmd_slate(argv) -> None:
        """the REAL slate, scored on itself"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py slate")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--reps", type=int, default=15,
                        help="simulations per real game")
        ap.add_argument("--limit", type=int, default=None)
        ap.add_argument("--refresh", action="store_true",
                        help="re-pull the schedule instead of using the cache")
        ap.add_argument("--no-weather", action="store_true")
        ap.add_argument("--no-venue", action="store_true")
        ap.add_argument("--no-real-sp", action="store_true",
                        help="board's highest-GS arm instead of the probable")
        ap.add_argument("--no-real-lineups", action="store_true")
        ap.add_argument("--workers", type=int, default=None,
                        help="processes (default cores - 2); the answer does "
                             "not depend on this")
        a = ap.parse_args(argv[1:])
        if a.refresh:
            n = len(season_slate(a.season, refresh=True))
            print(f"slate refreshed: {n} completed games")
        r = validate_slate_vs_reality(
            a.season, reps=a.reps, limit=a.limit,
            use_weather=not a.no_weather, use_venue=not a.no_venue,
            use_real_sp=not a.no_real_sp,
            use_real_lineups=not a.no_real_lineups, workers=a.workers)
        u, sim, real = r["used"], r["sim"], r["real"]
        print(f"real slate {a.season}: {u['games']} games x {a.reps} sims"
              f"   ({sim['team_games']} sim team-games, innings 1-8,"
              f" {r['workers']} workers)")
        print(f"  real starter {u['sp']}/{2*u['games']}   "
              f"posted lineup {u['lineup']}/{2*u['games']}   "
              f"weather {u['weather']}/{u['games']}   "
              f"park {u['venue']}/{u['games']}")
        print(f"\n  {'':10s} {'sim':>10s} {'real':>10s}")
        for k in ("mean", "sd", "var", "indep", "cov", "pair_cov"):
            print(f"  {k:10s} {sim[k]:10.4f} {real[k]:10.4f}")
        print("\n  per-inning mean   (real inning 1 is the HIGHEST — 5.4)")
        print(f"    {'inning':8s}" + "".join(f"{i:>8d}" for i in range(1, 9)))
        print(f"    {'sim':8s}" + "".join(f"{x:8.3f}" for x in sim["by_inning"]))
        print(f"    {'real':8s}" + "".join(f"{x:8.3f}" for x in real["by_inning"]))
        print(f"    {'diff':8s}" + "".join(
            f"{s - t:+8.3f}" for s, t in zip(sim["by_inning"], real["by_inning"])))
        print("\n  by window   (real: starter 1-5, bullpen 6-8, spanning)")
        for k in ("starter_1_5", "bullpen_6_8", "spanning"):
            s, t = sim["window"].get(k), real["window"].get(k)
            print(f"    {k:12s} {s:+.5f}   real {t:+.5f}"
                  if s is not None and t is not None else f"    {k:12s} -")
        gt = r["game_total"]
        print(f"\n  game total   sim {gt['sim_mean']:.3f}   "
              f"real {gt['real_mean']:.3f}   RMSE {gt['rmse']:.3f}")
        print(f"    model sd {gt['model_sd']:.3f} "
              f"(MC {gt['mc_sd']:.3f}, {gt['mc_share']:.0%} of it) "
              f"-> {gt['model_sd_adj']:.3f} noise-removed")
        print(f"    corr {gt['corr']:+.4f}   disattenuated "
              f"{gt['corr_adj']:+.4f}"
              if gt.get("corr_adj") is not None else
              f"    corr {gt['corr']:+.4f}")
        if gt["mc_share"] > 0.25:
            print("    ** Monte Carlo noise dominates the model spread at "
                  f"reps={a.reps}. Neither correlation is readable; "
                  "raise --reps. **")

    @staticmethod
    def cmd_pbp(argv) -> None:
        """one-time play-by-play backfill; every consumer reads it"""
        ap = argparse.ArgumentParser(prog="mlb_sim.py pbp")
        ap.add_argument("seasons", nargs="*", type=int, default=[2026])
        ap.add_argument("--workers", type=int, default=12)
        ap.add_argument("--check", action="store_true",
                        help="report what is missing, fetch nothing")
        a = ap.parse_args(argv[1:])
        for season in (a.seasons or [2026]):
            pks = RelieverUsage.season_game_pks(season)
            if not pks:
                print(f"[pbp] {season}: no completed games on disk — run "
                      f"`slate --refresh` first")
                continue
            r = PlayByPlay.backfill_play_by_play(pks, workers=a.workers,
                                                 check=a.check)
            if a.check:
                print(f"[pbp] {season}: {r['asked']} games, {r['had']} cached, "
                      f"{len(r['missing'])} missing")
                continue
            print(f"[pbp] {season}: {r['asked']} games — {r['had']} already "
                  f"cached, {r['fetched']} fetched, {r['failed']} failed")
            if r["failed"]:
                print("      failed games stay missing and will be retried "
                      "on the next run; nothing partial is cached.")

    @staticmethod
    def cmd_smoke(argv) -> None:
        Reports.smoke_test()

    # -- dispatch ----------------------------------------------------------
    COMMANDS = {
        'rates': cmd_rates,
        'calibrate': cmd_calibrate,
        'project': cmd_project,
        'clv': cmd_clv,
        'calibrate-form': cmd_calibrate_form,
        'marks': cmd_marks,
        'dispersion': cmd_dispersion,
        'calibrate-fatigue': cmd_calibrate_fatigue,
        'asof': cmd_asof,
        'backtest': cmd_backtest,
        'closing': cmd_closing,
        'forecastwx': cmd_forecastwx,
        'clvopen': cmd_clvopen,
        'stuff': cmd_stuff,
        'bmielke': cmd_bmielke,
        'bmaudit': cmd_bmaudit,
        'diff': cmd_diff,
        'ab': cmd_ab,
        'eventodds': cmd_eventodds,
        'recency': cmd_recency,
        'stints': cmd_stints,
        'aaa': cmd_aaa,
        'parkbuild': cmd_parkbuild,
        'milb': cmd_milb,
        'framing': cmd_framing,
        'milbpark': cmd_milbpark,
        'milbasof': cmd_milbasof,
        'baserunning': cmd_baserunning,
        're24': cmd_re24,
        'boards': cmd_boards,
        'slate': cmd_slate,
        'pbp': cmd_pbp,
        '': cmd_smoke,
        'smoke': cmd_smoke,
    }


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    handler = Cli.COMMANDS.get(cmd)
    if handler is None:
        print(__doc__)
        raise SystemExit(f"mlb_sim: unknown command {cmd!r}")
    handler(argv)



# ===========================================================================
# 13. CLV HARNESS — SCORING THE MODEL AGAINST MARKET MOVEMENT
# ===========================================================================
# Closing line value, not ROI: over any sample a bettor can realistically
# collect, ROI is dominated by variance.
#
# **READ THIS BEFORE BELIEVING A BACKTEST NUMBER.** `build_rates` reads
# SEASON-TO-DATE boards, so scoring an old game with them uses information that
# did not exist when the line opened. `record` snapshots today's projections
# against today's prices and is the real harness; `replay` scores past games
# with current rates, is optimistically biased, says so wherever it prints, and
# is a plumbing smoke test rather than an edge estimate.

# Renamed from "clv" 2026-08-16: the directory is MLB-specific and this
# repo has NHL/tennis/CS2 models that will want their own.
CLV_DIR = SAVE_DIR / "MLBclv"
MIN_BOOKS_FOR_CLV = 2      # a line quoted by one book is not a market


def _team_index() -> Dict[str, dict]:
    """{normalised club name: {abbr, venue}} from the cached StatsAPI roster.

    OddsPortal names clubs in full ("Cincinnati Reds"); the boards use
    abbreviations, and the two abbreviation sets disagree on exactly seven
    clubs (AZ/ARI, CWS/CHW, KC/KCR, SD/SDP, SF/SFG, TB/TBR, WSH/WSN), which
    is what `_FG_ALIAS` reconciles.
    """
    path = SHARED_DIR / "mlb_roster_2026.json"
    with open(path) as fh:
        teams = json.load(fh)["teams"]
    out = {}
    for t in teams:
        abbr = _FG_ALIAS.get(t.get("abbreviation"), t.get("abbreviation"))
        out[_norm_club(t["name"])] = {
            "abbr": abbr, "venue": (t.get("venue") or {}).get("name") or ""}
    return out


# StatsAPI abbreviation -> FanGraphs board abbreviation.
_FG_ALIAS = {"AZ": "ARI", "CWS": "CHW", "KC": "KCR", "SD": "SDP",
             "SF": "SFG", "TB": "TBR", "WSH": "WSN"}


# Spellings people actually type, beyond `_FG_ALIAS`. The board is the odd one
# out on seven clubs (TB/TBR, SD/SDP, ...), so anything typed on a command line
# or arriving from another feed has to be normalised INTO board spelling before
# it reaches `team_roster`. Oakland is the other trap: the club is ATH now, but
# OAK is what most sources and most muscle memory still say.
_CLUB_ALTS = {"OAK": "ATH", "AZ": "ARI", "ARZ": "ARI", "CWS": "CHW",
              "CHA": "CHW", "CHN": "CHC", "KC": "KCR", "SD": "SDP",
              "SF": "SFG", "TB": "TBR", "TBD": "TBR", "WSH": "WSN",
              "WAS": "WSN", "NYA": "NYY", "NYN": "NYM", "LAN": "LAD",
              "SLN": "STL", "SDN": "SDP", "SFN": "SFG"}


def normalize_club(abbr: str) -> str:
    """Any common club abbreviation -> the FanGraphs board's spelling.

    `TB` is not `TBR` on the board and seven clubs are like that, so a plain
    string from a CLI or another feed fails with "not enough TB rows" rather
    than anything that points at the cause.
    """
    a = (abbr or "").strip().upper()
    return _FG_ALIAS.get(a, _CLUB_ALTS.get(a, a))


def _norm_club(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


# Sponsor renames that a substring match cannot bridge. Kept in step with
# `EffortMLB.VENUE_ALIASES` — deliberately DUPLICATED rather than imported,
# because importing EffortMLB drags in Qt and this module must stay headless.
# If a park is renamed, both need the entry.
VENUE_ALIASES = {
    "daikin park": "Minute Maid Park",                  # renamed 2025
    "rate field": "Guaranteed Rate Field",              # renamed 2025
    "uniqlo field at dodger stadium": "Dodger Stadium",
    "loandepot park": "LoanDepot Park",                 # case only
    "oriole park at camden yards": "Camden Yards",
}


def resolve_venue(name: str) -> Optional[str]:
    """A StatsAPI venue name -> the key weatherman's STADIUM_DATA uses.

    Five of thirty do not match literally, all through sponsor renames. An
    unresolved venue is NOT harmless here: `wall_at` returns None and the park
    term dies mid-lineup, so the game silently loses its park/weather
    adjustment while every other number still looks fine.
    """
    if not name:
        return None
    STADIUM_DATA = weatherman.STADIUM_DATA
    if name in STADIUM_DATA:
        return name
    low = name.lower()
    alias = VENUE_ALIASES.get(low)
    if alias and alias in STADIUM_DATA:
        return alias
    for k in STADIUM_DATA:                       # substring, either direction
        kl = k.lower()
        if kl == low or kl in low or low in kl:
            return k
    return None


# ---------------------------------------------------------------------------
# Pricing maths
# ---------------------------------------------------------------------------

def implied(dec: Optional[float]) -> Optional[float]:
    """Decimal odds -> raw implied probability (still carrying the vig)."""
    if not dec or dec <= 1.0:
        return None
    return 1.0 / dec


class Pricing:
    """Pricing maths, and model prices for the whole-game markets."""

    @staticmethod
    def devig(decs: Sequence[Optional[float]]) -> List[Optional[float]]:
        """Strip the overround from one market's prices, proportionally.

        Proportional (multiplicative) de-vigging is used deliberately over
        something like Shin: it needs no extra parameter, and on the near-even
        two-way markets we score (totals, run lines) the difference between
        methods is far smaller than the book-to-book spread we are averaging over
        anyway. On a heavy favourite it would matter, which is why the moneyline
        result is reported separately rather than pooled with the totals.
        """
        raw = [implied(d) for d in decs]
        live = [p for p in raw if p]
        if len(live) < 2:
            return [None] * len(decs)
        tot = sum(live)
        return [(p / tot if p else None) for p in raw]

    @staticmethod
    def p_total_over(results: Sequence[GameResult], line: float) -> float:
        return price_over(game_totals(results), line)

    @staticmethod
    def _half_inning_hist(results: Sequence[GameResult], side: str) -> Dict[str, int]:
        """{runs in a half-inning: count} pooled over a set of simulated games."""
        out: Dict[str, int] = {}
        for r in results:
            for v in (r.half_runs_home if side == "home" else r.half_runs_away):
                k = str(v)
                out[k] = out.get(k, 0) + 1
        return out

    @staticmethod
    def club_quality_asof(season: int, save_dir: Path = SAVE_DIR,
                          cutoffs: Optional[Sequence[str]] = None
                          ) -> Dict[tuple, float]:
        """{(date, club): shrunk run differential per game} from PRIOR games only.

        **Two ways to be wrong here, and the first one bit.** *Leakage*: the entry
        is keyed by DATE but the loop runs per GAME, so on a doubleheader the
        second game's write overwrote the shared key and priced game one with its
        own outcome partly baked in (~2% of team-games). `seen` freezes the entry
        at a club's FIRST game of a date. *Freshness*: strictly-prior is legal but
        not automatically a fair A/B — every other input is frozen at the weekly
        cutoff, so pass `cutoffs` to freeze this one too. Live, `cutoffs=None` is
        right; you really do know yesterday's score.
        """
        out: Dict[tuple, float] = {}
        acc: Dict[str, List[float]] = {}
        seen: set = set()
        cuts = sorted(cutoffs) if cutoffs else None
        # {(cutoff, club): value} when freezing, so every game inside a cutoff
        # window reads the same number the boards were built from
        frozen: Dict[tuple, float] = {}
        for g in sorted(season_slate(season, save_dir=save_dir),
                        key=lambda r: r["date"]):
            hr = sum(g.get("home_innings") or [])
            ar = sum(g.get("away_innings") or [])
            cut = None
            if cuts:
                cut = None
                for c in cuts:
                    if c < g["date"]:
                        cut = c
                    else:
                        break
            for club, diff in ((g["home"], hr - ar), (g["away"], ar - hr)):
                got = acc.get(club) or [0.0, 0.0]
                key = (g["date"], club)
                if key not in seen:
                    seen.add(key)
                    if cuts:
                        fk = (cut, club)
                        if fk not in frozen:
                            frozen[fk] = TeamQuality.shrink_team_quality(got[0], int(got[1]))
                        out[key] = frozen[fk]
                    else:
                        out[key] = TeamQuality.shrink_team_quality(got[0], int(got[1]))
                acc[club] = [got[0] + diff, got[1] + 1]
        return out

    @staticmethod
    def _staff_split(results: Sequence[GameResult], home: "TeamSide",
                     away: "TeamSide") -> Dict[str, float]:
        """Mean runs / BF / outs charged to the STARTER vs the RELIEVERS, per side.

        `PitcherLine.r` charges a run to whoever was ON THE MOUND when it crossed,
        so an inherited runner is charged to the reliever rather than to the man
        who put him on. **That is the opposite of box-score convention and it
        biases exactly this split** — it flatters the starter and blames the pen.
        Anything read off `rp_r` is therefore an UPPER bound on relief runs and
        `sp_r` a lower bound on the starter's; only compare it to a real series
        built the same way (on-mound attribution), never to box-score ER.
        """
        out: Dict[str, float] = {}
        n = len(results) or 1
        for tag, side in (("h", home), ("a", away)):
            nm = side.starter.name
            # **`res.pitchers` is keyed by NAME across BOTH clubs**, so "everyone
            # who is not the starter" sweeps in the opposing staff. Read the
            # relief total off THIS side's bullpen by name instead; a first cut
            # that took the complement reported 6.4 relief runs a game against a
            # real 1.7 and would have made any relief comparison meaningless.
            pen = {p.name for p in side.bullpen}
            sp_r = sp_bf = sp_o = rp_r = rp_bf = 0.0
            for res in results:
                for who, line in res.pitchers.items():
                    if who == nm:
                        sp_r += line.r; sp_bf += line.bf; sp_o += line.outs
                    elif who in pen:
                        rp_r += line.r; rp_bf += line.bf
            out[f"sp_r_{tag}"] = sp_r / n
            out[f"sp_bf_{tag}"] = sp_bf / n
            out[f"sp_outs_{tag}"] = sp_o / n
            out[f"rp_r_{tag}"] = rp_r / n
            out[f"rp_bf_{tag}"] = rp_bf / n
        return out

    @staticmethod
    def p_home_covers(results: Sequence[GameResult], handicap: float) -> float:
        """P(home + handicap > away). OddsPortal signs the handicap from the HOME
        side, so `-1.5` is the home side laying a run and a half."""
        margins = [(r.runs_home + handicap) - r.runs_away for r in results]
        live = [m for m in margins if m != 0]
        if not live:
            return 0.5
        return sum(1 for m in live if m > 0) / len(live)


# ---------------------------------------------------------------------------
# Model prices for whole-game markets
# ---------------------------------------------------------------------------

def game_totals(results: Sequence[GameResult]) -> List[float]:
    return [float(r.runs_home + r.runs_away) for r in results]


def implied_line(results: Sequence[GameResult], lo: float = 3.5,
                 hi: float = 16.5) -> float:
    """The total at which the model would price the game pick'em.

    A book's total is the number where P(over) = 0.5. The simulated MEDIAN is
    the same idea but quantised to whole runs, since totals are integers — a
    model that thinks the fair number is 8.3 and one that thinks 8.9 both
    report a median of 8. Interpolating across the half-point ladder recovers
    the resolution the median throws away, and that resolution is most of the
    signal when market lines sit half a run apart.
    """
    return fair_line(game_totals(results), lo, hi)


def fair_line(vals: Sequence[float], lo: float = 3.5,
              hi: float = 16.5) -> float:
    """The pick'em total for ANY set of game totals — simulated or REAL.

    Split out of `implied_line` so the league's own realised totals can be put
    through the identical estimator. That comparison needs one definition, not
    two: a reimplementation that drifts by a tenth would move the reference the
    model is scored against, and the reference is the whole point (§`LEAGUE_FAIR`).
    """
    ladder = [lo + 0.5 * i for i in range(int((hi - lo) / 0.5) + 1)]
    prev_l, prev_p = ladder[0], price_over(vals, ladder[0])
    for L in ladder[1:]:
        p = price_over(vals, L)
        if p <= 0.5 <= prev_p and prev_p != p:
            return prev_l + (prev_p - 0.5) / (prev_p - p) * (L - prev_l)
        prev_l, prev_p = L, p
    return prev_l


# ---------------------------------------------------------------------------
# THE LEAGUE'S OWN FAIR LINE — the missing third leg of a totals comparison
# ---------------------------------------------------------------------------
# **`model - market` is a DISAGREEMENT, not a model error, and reading it as one
# cost a whole diagnostic session (2026-08-29).** A live slate came in +0.45 over
# the market, tripping `TotalsBias`' own guard; weather, park, the projected
# lineups, the bullpen, the starter shrink target and the MiLB gate were each
# measured and none owned it. They could not: against the LEAGUE's own fair line
# the model was +0.11 and the MARKET was -0.36. There were only eleven
# hundredths of model error to find and four tenths were being hunted.
#
# Three numbers describe league scoring and they sit a full run apart, so the
# reference has to be the SAME object a book hangs — the interpolated pick'em
# line, not the mean and not the discrete median:
#
#     2026:  mean 8.954   discrete median 8.00   FAIR LINE 8.329
#
# It is stable enough to be a reference: 8.326 / 8.321 / 8.329 across 2024-26.
_LEAGUE_FAIR: Dict[int, Optional[float]] = {}


def league_fair_total(season: Optional[int] = None,
                      save_dir: Path = SAVE_DIR) -> Optional[float]:
    """The league's realised pick'em total for `season`, or None with no slate.

    Returns None rather than a guess when the season has no cached results —
    a fabricated reference is worse than no reference, because every
    attribution downstream would be quoted against it.

    **CACHE-ONLY, deliberately.** `season_slate` FETCHES a missing season, and
    this is called from a banner: asking for the reference pulled a 1.4 MB
    schedule off StatsAPI for a season nobody was pricing. A reference lookup
    must never be a download. Populate a season with `season_slate` first.
    """
    season = CURRENT_SEASON if season is None else int(season)
    if season in _LEAGUE_FAIR:
        return _LEAGUE_FAIR[season]
    if not (Path(save_dir) / f"season_slate_{season}.json").exists():
        _LEAGUE_FAIR[season] = None
        return None
    try:
        games = season_slate(season, save_dir=save_dir)
    except Exception:                                          # noqa: BLE001
        games = []
    vals = [float(sum(g["home_innings"]) + sum(g["away_innings"]))
            for g in (games or [])
            if g.get("home_innings") and g.get("away_innings")]
    # A handful of April games is not a league reference.
    _LEAGUE_FAIR[season] = fair_line(vals) if len(vals) >= 200 else None
    return _LEAGUE_FAIR[season]


def p_home_win(results: Sequence[GameResult]) -> float:
    """Excludes the (vanishingly rare) unresolved tie, same as a book would."""
    dec = [r for r in results if r.runs_home != r.runs_away]
    if not dec:
        return 0.5
    return sum(1 for r in dec if r.runs_home > r.runs_away) / len(dec)


def _joint_runs(results: Sequence[GameResult]) -> Dict[str, int]:
    """{"home,away": count} over a set of simulated games."""
    out: Dict[str, int] = {}
    for r in results:
        k = f"{r.runs_home},{r.runs_away}"
        out[k] = out.get(k, 0) + 1
    return out


def joint_margins(game: dict) -> collections.Counter:
    """{margin: count} for one backtest game row.

    RAISES on a row with no `joint`. An arm cached before the field existed
    parses perfectly and would report a distribution of nothing at all, which
    is trap 9 — the silent-corruption shape that an aggregate cannot see.
    """
    j = game.get("joint")
    if not j:
        raise KeyError(
            f"mlb_sim: backtest row {game.get('pk')} has no 'joint' run "
            f"histogram. Re-run the arm with --fresh; an arm cached before "
            f"this field existed cannot answer a margin question.")
    out: collections.Counter = collections.Counter()
    for k, c in j.items():
        h, a = k.split(",")
        out[int(h) - int(a)] += c
    return out


# ---------------------------------------------------------------------------
# Scoring one game
# ---------------------------------------------------------------------------

@dataclass
class ClvPick:
    """One priced disagreement between the model and the opening line."""
    game: str
    market: str
    scope: str
    line: Optional[float]
    side: str                  # the outcome label we would have backed
    model_p: float
    open_p: float              # de-vigged opening probability of that side
    close_p: float             # de-vigged closing probability of the same side
    n_books: int
    open_dec: Optional[float] = None
    close_dec: Optional[float] = None

    @property
    def edge(self) -> float:
        """What the model claimed at the open."""
        return self.model_p - self.open_p

    @property
    def clv(self) -> float:
        """How far the market moved TOWARD us by the close.

        Positive means the closing line agreed with the model more than the
        opening line did. This is the whole measurement — it needs no game
        result, which is why it converges in a season rather than a decade.
        """
        return self.close_p - self.open_p


class Clv:
    """Scoring one game against the market, and running a slate."""

    @staticmethod
    def _pair_probs(line) -> Optional[tuple]:
        """(labels, open_probs, close_probs, open_decs, close_decs) for a 2-way
        line, de-vigged on both sides. None when either side is unpriced."""
        outs = line.outcomes
        if len(outs) != 2:
            return None
        close_dec = [o.avg_odds for o in outs]
        open_dec = [o.opening_avg for o in outs]
        if not all(close_dec) or not all(open_dec):
            return None
        op = Pricing.devig(open_dec)
        cp = Pricing.devig(close_dec)
        if not all(op) or not all(cp):
            return None
        return ([o.name for o in outs], op, cp, open_dec, close_dec)

    @staticmethod
    def clv_picks_for_game(results: Sequence[GameResult], eo,
                           label: str = "") -> List[ClvPick]:
        """Every market where the model disagreed with the OPENING line.

        The model is priced AT THE MARKET'S OWN LINE — the sim carries a full
        distribution, so it can answer any total or handicap, and comparing our
        8.5 against the book's 8.0 would measure nothing but the line difference.
        """
        picks: List[ClvPick] = []
        game = label or f"{eo.away} @ {eo.home}"

        def add(line, model_p_of_first: float):
            pr = Clv._pair_probs(line)
            if not pr or line.n_books < MIN_BOOKS_FOR_CLV:
                return
            labels, op, cp, od, cd = pr
            # Back whichever side the model thinks is underpriced at the open.
            i = 0 if model_p_of_first > op[0] else 1
            model_p = model_p_of_first if i == 0 else 1.0 - model_p_of_first
            picks.append(ClvPick(
                game=game, market=line.market, scope=line.scope,
                line=line.handicap, side=labels[i], model_p=model_p,
                open_p=op[i], close_p=cp[i], n_books=line.n_books,
                open_dec=od[i], close_dec=cd[i]))

        tot = eo.main_line("over-under")
        if tot and tot.handicap is not None:
            add(tot, Pricing.p_total_over(results, tot.handicap))

        ml = eo.main_line("home-away")
        if ml:
            add(ml, p_home_win(results))

        rl = eo.main_line("asian-handicap")
        if rl and rl.handicap is not None:
            add(rl, Pricing.p_home_covers(results, rl.handicap))

        return picks

    @staticmethod
    def summarize_bias(rows: Sequence[TotalsBias],
                       season: Optional[int] = None) -> dict:
        """Model, market, and THE LEAGUE — the disagreement split into its parts.

        `mean_diff` is model MINUS MARKET and is a disagreement, not an error.
        `model_vs_league` is the model's own error and is the one to act on;
        `market_vs_league` is where the book has this slate relative to a
        normal night. They sum to `mean_diff`. See `league_fair_total`.
        """
        if not rows:
            return {"n": 0}
        d = [r.diff for r in rows]
        d_sorted = sorted(d)
        mean_model = sum(r.model_total for r in rows) / len(rows)
        mean_market = sum(r.market_total for r in rows) / len(rows)
        out = {
            "n": len(d),
            "mean_model": mean_model,
            "mean_market": mean_market,
            "mean_diff": sum(d) / len(d),
            "median_diff": d_sorted[len(d) // 2],
            "over_share": sum(1 for x in d if x > 0) / len(d),
        }
        lg = league_fair_total(season)
        out["league_fair"] = lg
        out["model_vs_league"] = (mean_model - lg) if lg is not None else None
        out["market_vs_league"] = (mean_market - lg) if lg is not None else None
        return out

    @staticmethod
    def fade_correlation(picks: Sequence[ClvPick]) -> Optional[float]:
        """Is the model finding edges, or just fading whatever the market says?

        Correlates each pick's claimed edge against how far the market's price
        sits from the middle. Game-specific insight shows ~0; a COMPRESSED model
        shows strongly negative, because it reverts everything to the mean and so
        takes the under on every high total, the over on every low one, and every
        underdog on the moneyline.

        **Check this before reading an edge board.** One real slate: totals
        -0.650, moneyline -0.887 — the edge list was a readout of the model's own
        narrow spread. An "edge" that big and that correlated is a defect.
        """
        xs = [p.open_p - 0.5 for p in picks]
        ys = [p.edge for p in picks]
        n = len(xs)
        if n < 4:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
        sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
        if not sx or not sy:
            return None
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n / (sx * sy)

    @staticmethod
    def summarize_clv(picks: Sequence[ClvPick],
                      edge_floor: float = 0.02) -> dict:
        """Pooled CLV overall and among the picks the model was most confident in.

        The edge-filtered number is the one that matters: if the model has signal,
        the picks where it disagreed MOST with the open should be the ones the
        market moved furthest toward. A model with no signal shows ~0 in both, and
        — importantly — no gap between them.
        """
        def agg(rows):
            if not rows:
                return {"n": 0, "clv": None, "hit": None}
            cl = [p.clv for p in rows]
            return {"n": len(rows),
                    "clv": sum(cl) / len(cl),
                    "hit": sum(1 for c in cl if c > 0) / len(cl)}

        out = {"all": agg(list(picks)),
               "edge": agg([p for p in picks if p.edge >= edge_floor]),
               "fade": Clv.fade_correlation(picks)}
        by_market = {}
        for p in picks:
            by_market.setdefault(p.market, []).append(p)
        out["by_market"] = {k: agg(v) for k, v in by_market.items()}
        return out

    @staticmethod
    def _op_client(proxy: Optional[str] = None):
        from OddsPortalClient import OddsPortalClient
        return OddsPortalClient(proxy=proxy)

    @staticmethod
    def slate_games(sport: str = "baseball", proxy: Optional[str] = None,
                    date: Optional[str] = None) -> List[dict]:
        """Today's board from OddsPortal, joined to our club abbreviations.

        Rows whose clubs do not resolve are dropped with a count rather than
        silently skipped — an unresolved club is usually a name-map drift, and a
        harness that quietly scores 12 of 15 games looks identical to one that
        scored all 15.
        """
        c = Clv._op_client(proxy)
        idx = _team_index()
        out, unresolved = [], []
        for r in _op_listing(c, sport):
            if date:
                ts = r.get("date-start-timestamp")
                if not ts:
                    continue
                if datetime.datetime.fromtimestamp(int(ts)).date().isoformat() != date:
                    continue
            h = idx.get(_norm_club(r.get("home-name")))
            a = idx.get(_norm_club(r.get("away-name")))
            if not h or not a:
                unresolved.append(f"{r.get('away-name')} @ {r.get('home-name')}")
                continue
            out.append({
                "url": r.get("url"), "home": h["abbr"], "away": a["abbr"],
                "venue": resolve_venue(h["venue"]) or "",
                "start_ts": r.get("date-start-timestamp"),
                "label": f"{r.get('away-name')} @ {r.get('home-name')}",
            })
        if unresolved:
            print(f"[clv] {len(unresolved)} game(s) unresolved: "
                  f"{', '.join(unresolved[:3])}")
        return out


@dataclass
class TotalsBias:
    """Model total vs the market's total, per game.

    **Compare MEDIANS, never the mean.** A book hanging 8.0 at even money states
    P(over 8.0) = 0.5 — a MEDIAN. Game runs are strongly right-skewed and in this
    engine the gap is +0.75 runs (simulated mean 8.75, median 8.00), so comparing
    our mean to their line reads as a standing bias that does not exist. It cost a
    full diagnostic pass: model median 8.00, Bovada median 8.00, MLB's actual MEAN
    8.96 — all consistent. The CLV picks were never affected; they price
    `p_total_over` at the market's own number.

    **And `diff` is a DISAGREEMENT, not a model error.** Getting the descriptor
    right (above) makes the comparison like-for-like; it does not say which side
    is wrong. That needs the league's own fair line as a third leg —
    `summarize_bias` carries it, `league_fair_total` explains what it cost to
    learn. On 2026-08-29 this read +0.45 and the model was +0.11.
    """
    game: str
    model_total: float          # MEDIAN simulated total
    market_total: float
    model_mean: Optional[float] = None   # kept for reference only

    @property
    def diff(self) -> float:
        return self.model_total - self.market_total


# ---------------------------------------------------------------------------
# Running a slate
# ---------------------------------------------------------------------------

# **`/matches/baseball/` is the wrong page for an MLB slate** — it is the
# sport-wide listing, and at 02:00 local only ONE of the current day's rows was
# major-league. The LEAGUE page carries the real slate. **But it is a FIXTURES
# page, and a finished game leaves it**, so `--date` accepted any past date and
# resolved none, reporting "nothing on the board". Both pages are MERGED rather
# than switched on the date, because a slate in progress is genuinely mixed and
# either page alone is a partial board that looks complete. sim_state.md A.13.


def _op_page_rows(client, path: str) -> List[dict]:
    """Listing rows off one OddsPortal league page."""
    blob = client._next_payload(client._get(path).text)
    rows = []
    for mm in client._LISTING_ROW_RE.finditer(blob):
        obj = client._json_object_at(blob, mm.start())
        if obj and obj.get("url"):
            rows.append(obj)
    return rows


def _op_listing(client, sport: str, finished: bool = True) -> List[dict]:
    """Listing rows for a sport, preferring its LEAGUE pages.

    `finished` also reads the RESULTS page, which is the only place a
    concluded game appears. Deduped on `url`, which is the row identity — a
    game can legitimately be on both pages while a slate is in progress.
    """
    paths = [p for p in (OddsPortal.LEAGUE_PATH.get(sport),
                         OddsPortal.RESULTS_PATH.get(sport) if finished else None)
             if p]
    if not paths:
        return client._listing_rows(sport)
    out, seen = [], set()
    for path in paths:
        try:
            for r in _op_page_rows(client, path):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    out.append(r)
        except Exception as e:                                 # noqa: BLE001
            print(f"[clv] league page {path} failed ({e})")
    if out:
        return out
    print(f"[clv] no league rows; falling back to /matches/{sport}/")
    return client._listing_rows(sport)


def run_clv(sport: str = "baseball", n_sims: int = 8000,
            proxy: Optional[str] = None, locations: Optional[dict] = None,
            limit: Optional[int] = None,
            live_lineups: bool = True, verbose: bool = True,
            date: Optional[str] = None) -> tuple:
    """Project every game on the board and score it against the market.

    Returns (picks, summary). Fetches each game's markets once and simulates
    once; the same run prices the total, the moneyline and the run line, so
    the three are internally consistent rather than three separate models.
    """
    from OddsPortalClient import OddsPortalClient
    # **The board and the probables MUST be the same day, and they were not.**
    # `slate_games` with no date returned whatever OddsPortal was showing (at
    # 02:00, games already PLAYED) while `fetch_probables` defaulted to today,
    # so games were priced against probables that did not exist and silently
    # fell back to the board's best-nine-by-PA. One date, passed to both.
    date = date or datetime.date.today().isoformat()
    games = Clv.slate_games(sport, proxy, date=date)
    if limit:
        games = games[:limit]
    if not games:
        if verbose:
            print(f"[clv] nothing on the {date} board")
        return [], {}

    bat_table, _ = build_rates("bat")
    pit_table, _ = build_rates("pit")
    hz = starter_hazard()
    c = Clv._op_client(proxy)

    probables: Dict[tuple, dict] = {}
    if live_lineups:
        try:
            # doubleheader-aware: keys carry the game number, so look up
            # through `probable_for` rather than indexing (away, home)
            probables = fetch_probables(date)
        except Exception as e:
            print(f"[clv] probables unavailable ({e}) — falling back to "
                  f"season-board starters, which biases totals high")
    subs = {"sp": 0, "milb_sp": 0, "posted": 0, "projected": 0, "games": 0}

    picks: List[ClvPick] = []
    bias: List[TotalsBias] = []
    for g in games:
        try:
            if locations:
                eo = OddsPortalClient.get_event_odds_multi(
                    g["url"], locations, markets=(3, 2, 5))
            else:
                eo = c.get_event_odds(g["url"], markets=(3, 2, 5))
        except Exception as e:
            if verbose:
                print(f"[clv] odds failed {g['label']}: {e}")
            continue
        try:
            # doubleheader-aware; the CLV board has no game number, so this
            # takes game 1 rather than whichever parsed last
            pr = probable_for(probables, g["away"], g["home"]) or {}
            home, uh = build_side_live(
                g["home"], bat_table, pit_table, sp_id=pr.get("home_sp"),
                lineup_ids=pr.get("home_lineup"),
                catcher_id=pr.get("home_catcher"), hazard=hz)
            away, ua = build_side_live(
                g["away"], bat_table, pit_table, sp_id=pr.get("away_sp"),
                lineup_ids=pr.get("away_lineup"),
                catcher_id=pr.get("away_catcher"), hazard=hz)
            subs["games"] += 1
            subs["sp"] += int(uh["sp"]) + int(ua["sp"])
            # A debut priced off the minors IS tonight's real starter, but it
            # is a different quality of evidence and the banner says so.
            subs["milb_sp"] += sum(1 for _u in (uh, ua)
                                   if _u.get("sp_source") == "milb")
            # **A projection is not a posted lineup and the summary must not
            # say it is.** `used["lineup"]` only reports that a nine was USED,
            # and `run_clv` hands `build_side_live` the projected nine as
            # readily as the posted one — so counting it under "posted" is the
            # silent substitution `probable_for` grew `lineup_source` to
            # prevent. Same defect the slate banner already fixed.
            for _u, _side in ((uh, "home"), (ua, "away")):
                if not _u["lineup"]:
                    continue
                src = (pr.get(f"{_side}_lineup_source") or "").lower()
                subs["posted" if src == "posted" else "projected"] += 1
        except Exception as e:
            if verbose:
                print(f"[clv] sides failed {g['label']}: {e}")
            continue
        # **Venue and weather were NOT being passed, and that is not
        # cosmetic.** Every game on the live board was priced at a NEUTRAL park,
        # so Coors and Petco got the same run environment — it showed as the
        # model sitting 1.31 runs under the market on CLE @ COL.
        venue = resolve_venue(pr.get("venue") or "") or (g.get("venue") or None)
        # Forecast FIRST — its numeric bearing beats StatsAPI's 8-way label,
        # which missed by ~70 degrees at Sutter and cost 1.24 runs. A shut roof
        # still comes from the observation. See `live_game_weather`.
        wx = live_game_weather(pr.get("game_pk"), date, venue, pr.get("start"))
        res = simulate_many(
            home, away, n=n_sims, seed=17, weather=wx, venue=venue,
            ml=game_adjuster(int(date[:4]), "", {
                "venue": pr.get("venue") or "", "date": date,
                "temp_f": (wx or {}).get("temp_f"),
                "wind_mph": (wx or {}).get("wind_mph"),
                "wind_label": (wx or {}).get("wind_label") or "",
                "home_sp": pr.get("home_sp") or -1,
                "away_sp": pr.get("away_sp") or -1,
            }, home, away))
        got = Clv.clv_picks_for_game(res, eo, g["label"])
        picks.extend(got)
        tot = eo.main_line("over-under")
        gt = game_totals(res)
        model_mean = sum(gt) / len(gt)
        model_tot = implied_line(res)          # the book's own convention
        if tot and tot.handicap is not None:
            bias.append(TotalsBias(g["label"], model_tot, tot.handicap,
                                   model_mean))
        if verbose:
            print(f"  {g['label']:<38s} sim {model_tot:5.2f} "
                  f"| mkt {tot.handicap if tot else '--':>4} | {len(got)} picks")
    if verbose and subs["games"]:
        print(f"[clv] real starters used on {subs['sp']}/{2*subs['games']} "
              f"sides"
              + (f" ({subs['milb_sp']} of them built from the MINORS — "
                 f"no board row)" if subs['milb_sp'] else "")
              + f", lineups {subs['posted']} posted / "
                f"{subs['projected']} PROJECTED of {2*subs['games']}")
    return picks, {"clv": Clv.summarize_clv(picks),
                   "bias": Clv.summarize_bias(bias, int(date[:4])),
                   "subs": subs}



# ---------------------------------------------------------------------------
# Tonight's actual probables and posted lineups
# ---------------------------------------------------------------------------
# `build_side` hands every club its highest-GS arm in every game — fine offline,
# wrong on a slate: a two-ace matchup and a bullpen game get the same starter,
# so the model cannot see the pitching matchup at all. On one measured slate it
# left totals +0.45 runs high.



class LiveSlate:
    """Tonight's probables, posted and projected lineups."""

    @staticmethod
    def _lineup_catcher(players: Optional[Sequence[dict]]) -> Optional[int]:
        """The posted catcher's MLBAM id, or None when the lineup has no C.

        A posted nine can legitimately lack a catcher — a DH-only card, or a
        partial lineup — so this returns None rather than guessing, and the caller
        falls back to the club figure.
        """
        for pl in (players or []):
            if ((pl.get("primaryPosition") or {}).get("abbreviation") or "") == "C":
                try:
                    return int(pl["id"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    @staticmethod
    @staticmethod
    def rotowire_url(date: Optional[str] = None) -> Optional[str]:
        """The Rotowire lineups URL that serves `date`, or None if it cannot.

        **Rotowire takes a RELATIVE selector, and the ISO form fails SILENTLY**:
        `?date=tomorrow` returns tomorrow's 16 lineups, `?date=2026-08-25` returns
        200 and TODAY's page with 11. So the offset is translated to the keyword
        here, only today and tomorrow are expressible, and anything else returns
        None so the caller refuses rather than pricing off the wrong day.
        """
        if not date:
            return Rotowire.LINEUPS_URL
        try:
            want = datetime.date.fromisoformat(date)
        except ValueError:
            return None
        delta = (want - datetime.date.today()).days
        if delta == 0:
            return Rotowire.LINEUPS_URL
        if delta == 1:
            return f"{Rotowire.LINEUPS_URL}?date=tomorrow"
        return None

    @staticmethod
    def rotowire_lineup_date(timeout: float = 20.0,
                             url: Optional[str] = None) -> Optional[str]:
        """The date the Rotowire lineups page is actually showing, ISO, or None.

        **Which slate the page describes is a fact about what came back, not what
        was asked for.** Club pairings on consecutive days of a SERIES are
        identical, so the matchup set cannot disambiguate, and a projection
        applied to the wrong day is a whole lineup of wrong data with nothing to
        notice it by. `url` must be the SAME url the lineups are scraped from, or
        this verifies one page and trusts another.
        """
        try:
            r = requests.get(url or Rotowire.LINEUPS_URL, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            mm = _ROTO_DATE.search(r.text)
            if not mm:
                return None
            return (f"{mm.group(3)}-{_MONTHS.index(mm.group(1)) + 1:02d}"
                    f"-{int(mm.group(2)):02d}")
        except Exception:                                          # noqa: BLE001
            return None

    @staticmethod
    def projected_lineups(date: Optional[str] = None,
                          timeout: float = 20.0) -> Dict[str, List[tuple]]:
        """{club abbr: [(name, pos, bats), ...]} in batting order, or {}.

        `date` is REQUESTED via `rotowire_url` and then CHECKED against what the
        page prints. Both halves are needed: requesting without checking trusts a
        parameter that fails silently, and checking without requesting means
        tomorrow is never available at all. Club codes are normalised onto the
        board's spelling on WRITE, not on query — the seven-club disagreement is
        the silent-key-miss trap of §7.2.
        """
        url = LiveSlate.rotowire_url(date)
        if url is None:
            print(f"mlb_sim: Rotowire only serves today and tomorrow — "
                  f"no projected lineups for {date}")
            return {}
        if date:
            shown = LiveSlate.rotowire_lineup_date(timeout, url)
            if shown and shown != date:
                print(f"mlb_sim: Rotowire is showing {shown}, not {date} — "
                      f"no projected lineups for this slate")
                return {}
        try:
            from GUIMLBlineups import fetch_daily_lineups
            matchups = fetch_daily_lineups(url) or []
        except Exception as e:                                    # noqa: BLE001
            print(f"mlb_sim: projected lineups unavailable ({e})")
            return {}
        out: Dict[str, List[tuple]] = {}
        for mu in matchups:
            for abbr, players in (mu.get("Team_Lineups") or {}).items():
                good = [p for p in players if p and p[0]]
                if abbr and len(good) >= 9:
                    out[normalize_club(_FG_ALIAS.get(abbr, abbr))] = good[:9]
        return out

    @staticmethod
    def _name_key(name: str) -> Tuple[str, str]:
        """(surname, first token) with accents stripped and suffixes dropped.

        The suffix is dropped from position 1 ONWARD only: 'V. Guerrero' against
        'Vladimir Guerrero Jr.' matches nothing if the last token is taken blind,
        and 'V.' is itself a Roman numeral, so stripping it from position 0 would
        leave no first name at all.
        """
        txt = unicodedata.normalize("NFKD", name or "")
        txt = "".join(c for c in txt if not unicodedata.combining(c))
        parts = [w.strip(".,'") for w in txt.replace("-", " ").split() if w.strip(".,'")]
        while len(parts) > 1 and parts[-1].lower().strip(".") in (
                "jr", "sr", "ii", "iii", "iv", "v"):
            parts.pop()
        if not parts:
            return "", ""
        return parts[-1].lower(), parts[0].lower()

    @staticmethod
    def resolve_projected_lineup(abbr: str, players: Sequence[tuple],
                                 season: Optional[int] = None,
                                 save_dir: Path = SAVE_DIR,
                                 min_resolved: int = 7) -> List[int]:
        """Rotowire display names -> MLBAM ids, in batting order.

        Rotowire abbreviates most first names ('C. DeLauter'), so this matches on
        SURNAME plus first INITIAL inside that club's board rows, then narrows a
        tie by bat side, position, playing time. A name ambiguous after all three
        returns `UNRESOLVED_BATTER` rather than a guess — `_game_side` turns that
        into a replacement-level hitter, the honest answer for a man the board has
        never seen. Returns [] below `min_resolved`, where the projection is worse
        than the board's own best nine.
        """
        season = CURRENT_SEASON if season is None else int(season)
        pool = team_roster("bat", season, save_dir).get(abbr) or []
        wide = load_board("bat", season, save_dir) or []
        idx: Dict[Tuple[str, str], List[dict]] = {}
        for row in pool:
            idx.setdefault(LiveSlate._name_key(row.get("PlayerName") or ""), []).append(row)

        def hits(rows, last, first):
            out = []
            for r in rows:
                l, f = LiveSlate._name_key(r.get("PlayerName") or "")
                if l != last:
                    continue
                if f[:1] == first[:1] if len(first) <= 1 else f == first:
                    out.append(r)
            return out

        ids: List[int] = []
        for name, pos, bats in players[:9]:
            last, first = LiveSlate._name_key(name)
            initial = len(first) <= 1 or "." in str(name).split()[0]
            cands = hits(pool, last, first if initial else first)
            if not cands:
                # A call-up or a deadline pickup whose board row still says his old
                # club. Only usable when the name is unique LEAGUE-wide — 'Luis
                # Garcia' is not, and stays unresolved.
                league = hits(wide, last, first)
                cands = league if len(league) == 1 else []
            if len(cands) > 1:
                for key, want in (("Bats", bats), ("Pos", pos)):
                    narrowed = [r for r in cands
                                if str(r.get(key) or "").upper()[:len(str(want))]
                                == str(want or "").upper()]
                    if len(narrowed) == 1:
                        cands = narrowed
                        break
                    if narrowed:
                        cands = narrowed
            if len(cands) > 1:
                cands = sorted(cands, key=lambda r: -_num(r, "PA"))[:1]
            rid = _row_id(cands[0]) if cands else None
            ids.append(int(rid) if rid else UNRESOLVED_BATTER)
        got = sum(1 for i in ids if i != UNRESOLVED_BATTER)
        return ids if got >= min_resolved else []

    @staticmethod
    def _opener_bf_shape() -> Sequence[int]:
        """Real opener-length starts when cached, the hand-drawn shape otherwise."""
        if not _OPENER_SHAPE:
            got = []
            try:
                with open(STINT_CACHE) as fh:
                    got = [s["bf"] for s in json.load(fh)
                           if s.get("starter") and 0 < (s.get("bf") or 0) <= OPENER_BF_MAX]
            except (OSError, ValueError):
                got = []
            _OPENER_SHAPE.append(tuple(got) if len(got) >= 100
                                 else _OPENER_BF_SHAPE)
        return _OPENER_SHAPE[0]

    @staticmethod
    def _pit_row(pid: int, season: int, save_dir: Path) -> Optional[dict]:
        """One pitcher's board row, by id. Indexed, not scanned."""
        return Boards._board_index("pit", season, save_dir).get(int(pid))


# --- PROJECTED lineups, for the hours before the real ones are posted ------
# StatsAPI's `lineups` hydrate is EMPTY until a club files its card, so a morning
# run has `USE_POSTED_LINEUP` on and nothing to use it with, and falls back to
# the board's best-nine-by-PA — which is POSITIVELY SELECTED (5.6a).
# `GUIMLBlineups` already scrapes Rotowire with nothing but requests and bs4.
# **A projection is not a posted lineup and the difference is recorded**, never
# folded in silently: a silent substitution is indistinguishable from having
# used the real thing.
USE_PROJECTED_LINEUP = True


_ROTO_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),?\s+(20\d\d)")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


UNRESOLVED_BATTER = -1


def fetch_probables(date: Optional[str] = None, timeout: float = 20.0
                    ) -> Dict[tuple, dict]:
    """{(away_abbr, home_abbr): {...}} for one date, from MLB StatsAPI.

    Abbreviations are mapped into the FanGraphs board's spelling, since the
    two disagree on seven clubs. Free, no key, one request for the slate.
    """
    if date is None:
        date = datetime.date.today().isoformat()
    url = StatsApi.schedule_url(date=date,
                                hydrate="probablePitcher,lineups,team")

    def _fetch():
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # Memo the RAW payload, never the parsed card: callers own their dict and
    # a shared one would let a mutation leak into the next call. Parsing again
    # is free next to the request.
    data = Query.memo(f"schedule|probables|{date}", _fetch)

    def abbr(side_team: dict) -> str:
        a = (side_team.get("team") or {}).get("abbreviation") or ""
        return _FG_ALIAS.get(a, a)

    # **Keyed on (away, home) — which a DOUBLEHEADER collides on.** The dict
    # kept whichever game was parsed last, so the live path priced game two
    # under game one's key and reported success. `game_number` is now part of
    # the key and a bare lookup resolves to game 1. Note `odds_by_game` drops
    # ambiguous keys and `odds_by_pk` resolves them on the final score — the
    # archive DOES distinguish the two, it is the (date, home, away) triple that
    # cannot.
    out: Dict[tuple, dict] = {}
    for day in data.get("dates", []):
        for g in day.get("games", []):
            t = g.get("teams") or {}
            home, away = t.get("home") or {}, t.get("away") or {}
            ha, aa = abbr(home), abbr(away)
            if not ha or not aa:
                continue
            lu = g.get("lineups") or {}
            out[(aa, ha, int(g.get("gameNumber") or 1))] = {
                "game_pk": g.get("gamePk"),
                "game_number": int(g.get("gameNumber") or 1),
                "start": g.get("gameDate"),
                "status": ((g.get("status") or {}).get("detailedState") or ""),
                "venue": (g.get("venue") or {}).get("name") or "",
                "home_sp": ((home.get("probablePitcher") or {}).get("id")),
                "away_sp": ((away.get("probablePitcher") or {}).get("id")),
                "home_sp_name": ((home.get("probablePitcher") or {}).get("fullName")),
                "away_sp_name": ((away.get("probablePitcher") or {}).get("fullName")),
                "home_lineup": [p.get("id") for p in (lu.get("homePlayers") or [])],
                "away_lineup": [p.get("id") for p in (lu.get("awayPlayers") or [])],
                # Filled from the beat-writer projection below when the club
                # has not filed its card. TAGGED, never folded in silently.
                "home_lineup_source": "posted", "away_lineup_source": "posted",
                # **The CATCHER, by name rather than by lineup slot.** Framing
                # is a player skill, so the club aggregate is the wrong object
                # to lag — the hydrate carries `primaryPosition`, so the man
                # actually behind the plate is free to identify.
                "home_catcher": LiveSlate._lineup_catcher(lu.get("homePlayers")),
                "away_catcher": LiveSlate._lineup_catcher(lu.get("awayPlayers")),
            }
    # **The projection fills only what StatsAPI left EMPTY**, and only when
    # the club has not filed. A posted nine is always preferred: it is the
    # real card, the projection is a forecast of it, and overwriting one with
    # the other would be a downgrade wearing the label of a fallback.
    if USE_PROJECTED_LINEUP and any(
            len(r.get(f"{s}_lineup") or []) < 9
            for r in out.values() for s in ("home", "away")):
        proj = LiveSlate.projected_lineups(date)
        if proj:
            season = int(date[:4])
            cache: Dict[str, List[int]] = {}
            for key, row in out.items():
                aa, ha = key[0], key[1]
                for side, abbr in (("home", ha), ("away", aa)):
                    if len(row.get(f"{side}_lineup") or []) >= 9:
                        continue
                    if abbr not in cache:
                        cache[abbr] = LiveSlate.resolve_projected_lineup(
                            abbr, proj.get(abbr) or [], season)
                    ids = cache[abbr]
                    if ids:
                        row[f"{side}_lineup"] = ids
                        row[f"{side}_lineup_source"] = "projected"

    return out


def probable_for(card: Dict[tuple, dict], away: str, home: str,
                 game_number: Optional[int] = None) -> dict:
    """One matchup out of `fetch_probables`, doubleheader-aware.

    Without `game_number` this returns the EARLIEST game, which is the sane
    default: it is deterministic, and a caller that does not know a doubleheader
    exists is better served the first game than an arbitrary one.
    """
    hits = sorted((k[2], v) for k, v in card.items()
                  if k[0] == away and k[1] == home)
    if not hits:
        return {}
    if game_number is not None:
        for n, v in hits:
            if n == int(game_number):
                return v
        return {}
    return hits[0][1]


def game_weather(game_pk: int, date: Optional[str] = None,
                 timeout: float = 30.0) -> Optional[dict]:
    """Tonight's conditions for one game, in `weather_tilt`'s shape.

    StatsAPI hydrates weather on the schedule endpoint, and its wind string is
    "12 mph, Out To CF" — a FIELD-relative label, not a compass bearing, so it
    carries `wind_label` rather than `wind_dir_deg`.
    """
    date = date or datetime.date.today().isoformat()
    url = StatsApi.schedule_url(date=date, hydrate="weather", game_type="R")
    # **This is a WHOLE-DAY payload scanned for ONE gamePk**, and `run_clv`
    # calls it inside its per-game loop — 15 downloads of the same response on
    # a 15-game slate, 14 of them discarded. Memo the raw payload per date.
    data = Query.memo(f"schedule|weather|{date}",
                      lambda: requests.get(url, timeout=timeout).json())
    for day in data.get("dates", []):
        for g in day.get("games", []):
            if int(g.get("gamePk", -1)) != int(game_pk):
                continue
            wx = g.get("weather") or {}
            if not wx:
                return None
            m = re.match(r"\s*(\d+(?:\.\d+)?)\s*mph,\s*(.*)",
                         str(wx.get("wind") or ""))
            temp = wx.get("temp")
            return {
                "condition": wx.get("condition"),
                "temp_f": float(temp) if temp not in (None, "") else None,
                "wind_mph": float(m.group(1)) if m else None,
                "wind_label": (m.group(2).strip() if m else ""),
            }
    return None


# An OPENER is a reliever making the start, and he must not inherit the starter
# hook curve — San Diego started Wandy Peralta (55 G / 5 GS, 4.83 BF/outing) and
# the sim ran him 5.14 IP, because `build_side_live` handed the named starter
# the generic curve regardless of who he is. Detected on the board rather than
# guessed; his hook comes from HIS OWN measured `bf_per_outing`.
#
# **0.15, not 0.5 — MEASURED, and the 0.5 was drawing the line in the wrong
# place.** Median batters faced in a START by the pitcher's own GS share, over
# 3,728 cached starts (`START_BF_BY_GS_SHARE`, measured 2026-08-23):
#
#     GS share    n      median BF
#     < 0.15      168        6.0     <- the length actually collapses here
#     0.15-0.30    41       21.0
#     0.30-0.50   177       21.0     <- ordinary starts, mis-hooked as openers
#     0.50-0.75   221       21.0
#     0.75+      3120       23.0
#
# Arms between 0.15 and 0.50 throw ORDINARY 21-batter starts; only below 0.15
# does the length collapse. 4j found this while repairing `start_bf_estimate`
# and recorded it as "a separate, unmade change" — it is a CLASSIFICATION
# constant and that pass was about the length ESTIMATE, so nothing picked it
# up. §5.12's shape: a measured signal outranked by a legacy default.
#
# Live case, 2026-08-28: Blade Tidwell (SFG, 12 G / 4 GS = 0.333) was hooked as
# an opener against ARI and simulated 2.73 IP against a real start. Worth
# **1.9 points** of win probability on that game; one of thirty probables on
# the slate moved. Games whose starter is outside 0.15-0.50 are bit-identical —
# the hazard VALUE changes, the draw count does not.
OPENER_GS_SHARE = 0.15

# Shape of a short outing, mean 5.0 BF, rescaled to the arm's own mean.
# **Hand-drawn, and the real distribution is on disk** — mean 5.00 against a
# real 6.23 over 222 opener-length starts, sd 1.47 against 2.31. Kept only as
# the fallback for a checkout with no stint cache.
_OPENER_BF_SHAPE = (3, 3, 4, 4, 4, 5, 5, 5, 6, 6, 7, 8)
_OPENER_SHAPE: List[Sequence[int]] = []


_GS_SHARE: Dict[int, Dict[int, float]] = {}
_PIT_ROWS: Dict[tuple, Dict[int, dict]] = {}


def starter_gs_share(pid: Optional[int], season: Optional[int] = None,
                     save_dir: Path = SAVE_DIR) -> Optional[float]:
    """GS / G off the pitching board. None when the arm is not on it."""
    season = CURRENT_SEASON if season is None else int(season)
    if pid is None:
        return None
    tab = _GS_SHARE.get(season)
    if tab is None:
        tab = {}
        for row in load_board("pit", season, save_dir):
            rid = _row_id(row)
            g = _num(row, "G")
            if rid and g > 0:
                tab[rid] = _num(row, "GS") / g
        _GS_SHARE[season] = tab
    return tab.get(int(pid))


# Batters faced per inning, league. Converts an innings-per-start figure into
# the batters-faced units the hook curve is indexed by.
BF_PER_INNING = 4.30


# --- what a real start actually looks like, MEASURED ----------------------
# 121 PURE starters (G == GS, so the relief netting is identically zero) span
# 4.10 to 6.60 IP/start, median 5.46, p95 6.05. The shipped clamp ceiling was
# 7.00 — above anything a real starter does — and it CLAMPED rather than
# refused, so a 16.10 IP/start netting was served as a 7-inning starter.
START_IP_CEILING = 6.6
START_IP_FLOOR = 0.7            # a true opener legitimately goes ~1 inning

# `ip_rel` is netted out of season IP and the remainder divided by GS, so an
# error in it is amplified by **(G - GS) / GS** — up to 59x on the 2026 board,
# where 86 of 335 arms sit above 3x. Only 34.8% of board pitchers carry a
# measured `ip_per_outing`; the rest take a 1.0 default, and at high leverage
# that guess cannot carry the estimate. Above this, refuse.
START_NET_MAX_LEVERAGE = 1.0

# Median batters faced in a START, by the pitcher's own GS share, over 3,728
# cached starts. **The step is at 0.15**: arms between 0.15 and 0.50 throw
# ordinary 21-batter starts (n=41 and n=177, median 21.0 each), and only below
# 0.15 does the length collapse to a median of 6.0. That is the population
# split the old docstring was reaching for.
#
# `OPENER_GS_SHARE` sat at 0.5 against this table from 2026-08-23 to 08-28 and
# now sits ON the step; a test pins the two together, because a classification
# constant beside its own measurement is how they drifted 0.35 apart.
START_BF_BY_GS_SHARE: Tuple[Tuple[float, float], ...] = (
    (0.15, 6.0), (0.75, 21.0), (2.0, 23.0))


def population_start_bf(pid: Optional[int], season: Optional[int] = None,
                        save_dir: Path = SAVE_DIR) -> float:
    """What an arm with THIS GS share throws in a start, measured.

    The fallback when his own line cannot resolve it. Deliberately not his
    relief `bf_per_outing`: `start_bf_estimate`'s own docstring says the
    relief workload is the wrong number for a start, and then the call site
    used it as the fallback anyway — which is how a man with four real starts
    was handed a one-inning target.
    """
    season = CURRENT_SEASON if season is None else int(season)
    share = starter_gs_share(pid, season, save_dir)
    if share is None:
        return START_BF_BY_GS_SHARE[-1][1]
    for hi, bf in START_BF_BY_GS_SHARE:
        if share < hi:
            return bf
    return START_BF_BY_GS_SHARE[-1][1]


def start_bf_estimate(pid: Optional[int], season: Optional[int] = None,
                      save_dir: Path = SAVE_DIR) -> Optional[float]:
    """Batters this arm faces in a START, from his own IP/GS on the board.

    **His RELIEF workload is the wrong number for this.** IP is shared with his
    relief work — Wandy Peralta has 61 IP over 53 G but only 4 GS, so a naive
    ratio calls him a 15-inning starter — so the relief innings are netted out
    first using his own measured relief length.

    **Returns None rather than a number it cannot stand behind.** The netting
    divides by GS, so an error in `ip_rel` is multiplied by (G - GS) / GS: Lake
    Bachar came out at 7.05 IP per start and the old clamp turned that into the
    longest projected start on the slate. A clamp is the wrong instrument — it
    converts "this arithmetic did not resolve" into "this man throws a complete
    game" (trap 11). The caller falls back to `population_start_bf`.
    """
    season = CURRENT_SEASON if season is None else int(season)
    if pid is None:
        return None
    row = LiveSlate._pit_row(int(pid), season, save_dir)
    if row is None:
        return None
    gs, g, ip = _num(row, "GS"), _num(row, "G"), _innings(row, "IP")
    if gs < 1 or ip <= 0:
        return None
    relief = max(0.0, g - gs)
    tr = RelieverTraits.load_reliever_traits(season).get(int(pid)) or {}
    measured = tr.get("ip_per_outing")
    # **Refuse when the netting is underdetermined.** With no measured relief
    # length we are guessing, and the guess is amplified by the leverage.
    if relief > 0 and measured is None and relief / gs > START_NET_MAX_LEVERAGE:
        return None
    ip_start = (ip - relief * float(measured or 1.0)) / gs
    # **Refuse rather than clamp.** Outside the band real starters occupy, the
    # netting has failed and the number carries no information.
    if not (START_IP_FLOOR <= ip_start <= START_IP_CEILING):
        return None
    return ip_start * BF_PER_INNING


def opener_hazard(bf_target: float) -> List[float]:
    """A hook curve centred on a measured batters-faced target."""
    shape = LiveSlate._opener_bf_shape()
    base = statistics.mean(shape) if shape else 5.0
    scale = max(0.4, float(bf_target or 4.5)) / base
    return hook_hazard([max(1, round(b * scale)) for b in shape])


def build_side_live(abbr: str, bat_table: Dict[int, dict],
                    pit_table: Dict[int, dict], *,
                    sp_id: Optional[int] = None,
                    lineup_ids: Optional[Sequence[int]] = None,
                    catcher_id: Optional[int] = None,
                    season: Optional[int] = None,
                    hazard: Optional[List[float]] = None,
                    save_dir: Path = SAVE_DIR,
                    use_itp_pen: bool = True):
    """A TeamSide using tonight's ACTUAL starter, posted lineup and BULLPEN.

    Falls back to `build_side`'s season-board choices for whatever is missing and
    REPORTS which parts were substituted, because a silent fallback is
    indistinguishable from having used the real thing.

    **The pen comes from insidethepen, not the season board.** The board gives
    the UNION of every reliever a club used all year (24.2 arms); the real pen is
    8, and for Oakland only 4 of its 8 current arms were on the board list the sim
    had been using. Arms resting on real recent workload are dropped here.
    """
    season = CURRENT_SEASON if season is None else int(season)
    side = build_side(abbr, bat_table, pit_table, season, hazard, save_dir)
    # **`sp_source` exists because `used["sp"]` cannot tell a BOARD starter from
    # a MiLB-built one, and a reader who cannot tell will mistrust the right
    # answer.** A debut has no board row, so a readout that prints board columns
    # shows `IP 0.0 ERA 0.00` — which reads as "priced at replacement" when it
    # actually means "priced off 431 Double-A batters". Same silent-substitution
    # shape as `lineup_source`, which this file has now fixed three times.
    used = {"sp": False, "sp_source": "", "lineup": False,
            "pen": "board", "framing": "club"}

    # **Tonight's actual catcher, not the club's season average.** Only when
    # the pitch-level series is on — the Savant CSV is club-level and has no
    # per-catcher figure to reach for. Lagged by `TEAM_CONTEXT_LAG` like every
    # other team-context term; lagging a CATCHER is legitimate because his
    # skill travels with him, which a club aggregate's does not.
    if catcher_id is not None:
        side.catcher_id = int(catcher_id)
        if USE_PITCH_FRAMING:
            v = Framing.catcher_framing_per_game(int(catcher_id),
                                         season - TEAM_CONTEXT_LAG, save_dir)
            if v is not None:
                side.framing = v
                used["framing"] = "catcher"

    if use_itp_pen:
        try:
            pen, rep = build_pen_from_itp(abbr, pit_table)
        except Exception as e:
            pen, rep = [], {"ok": False, "reason": str(e)}
        if pen:
            side.bullpen = pen
            used["pen"] = "itp"
            used["pen_report"] = rep
        else:
            used["pen_report"] = rep

    if sp_id:
        # An OPENER gets his OWN hook, not a starter's — see OPENER_GS_SHARE.
        share = starter_gs_share(sp_id, season, save_dir)
        is_opener = share is not None and share < OPENER_GS_SHARE
        # His own start length where his line can resolve one, else what arms
        # with HIS GS SHARE actually throw. **The old chain fell back to
        # `traits["bf_per_outing"]`, which is a RELIEF length** — the very number
        # `start_bf_estimate` says is wrong for a start — and then to a bare 4.5,
        # so the two outcomes were a 7-inning start or a one-inning one with
        # nothing in between.
        bf_target = (start_bf_estimate(sp_id, season, save_dir)
                     or population_start_bf(sp_id, season, save_dir))
        hz = opener_hazard(bf_target) if is_opener else (hazard or [])
        sp = make_pitcher(int(sp_id), pit_table, is_starter=True, hazard=hz)
        sp_source = "board" if sp is not None else ""
        if sp is None:
            # **A DEBUT has no board row, so `make_pitcher` returns None and
            # the side silently keeps the club's board starter — its BEST arm.**
            # Kade Anderson's 2026-08-22 debut was priced as Logan Gilbert,
            # worth 4.1 points of win probability. The minor league ladder can
            # say something where the board cannot, so it is asked first.
            sp = Boards.milb_only_pitcher(int(sp_id), season, save_dir,
                                   is_starter=True, hazard=hz)
            sp_source = "milb" if sp is not None else ""
        if sp is not None:
            side.starter = sp
            used["sp"] = True
            used["sp_source"] = sp_source
            used["opener"] = is_opener
            # the named starter must not also be sitting in his own bullpen
            side.bullpen = [p for p in side.bullpen if p.player_id != int(sp_id)]

    if lineup_ids:
        lineup = [make_batter(int(p), bat_table, season, save_dir)
                  for p in lineup_ids[:9]]
        lineup = [b for b in lineup if b is not None]
        if len(lineup) == 9:
            side.lineup = lineup
            used["lineup"] = True

    return side, used



# ===========================================================================
# 14. RELIEVER USAGE TRAITS
# ===========================================================================
# Every number a bullpen decision needs, MEASURED per arm into
# `MLBAnalytics/reliever_traits_<season>.csv`; `validate_bullpen_usage()` then
# checks the sim REPRODUCES them, because a trait that is loaded and not
# reproduced is not modelled. All four are ratios the board already contains.
# League marks for sanity: appearance rate median 10.8%, **max 53.4%** — any
# simulated arm above ~50% is wrong by construction.

MLBA_DIR = _APP_ROOT.parent / "MLBAnalytics"
RELIEVER_TRAIT_COLS = ("player_name", "player_id", "team", "season",
                       "g", "team_games", "app_rate", "bf_per_outing",
                       "ip_per_outing", "gm_li", "sv", "hld", "siera")


def _team_games(season: Optional[int] = None, save_dir: Path = SAVE_DIR) -> Dict[str, float]:
    """Games played per club, taken as the max G on the batting board."""
    season = CURRENT_SEASON if season is None else int(season)
    out: Dict[str, float] = {}
    for r in load_board("bat", season, save_dir) or []:
        t = r.get("TeamNameAbb")
        if t and "Tms" not in str(t):
            out[t] = max(out.get(t, 0.0), _num(r, "G"))
    return out


class RelieverTraits:
    """Per-arm usage traits, and the real bullpen state from insidethepen."""

    @staticmethod
    def export_reliever_traits(season: Optional[int] = None,
                               save_dir: Path = SAVE_DIR,
                               with_itp: bool = False,
                               min_app_rate: float = 0.0,
                               workers: int = 8) -> Path:
        """Write per-reliever usage traits to MLBAnalytics as a flat CSV.

        `with_itp` additionally fetches each arm's insidethepen deployment traits
        (when he enters, the score he is trusted in, whether he goes back-to-back).
        That is one HTTP request per pitcher, so it is threaded and off by default —
        the FanGraphs-derived columns need no network at all.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rows = load_board("pit", season, save_dir) or []
        tg = _team_games(season, save_dir)
        MLBA_DIR.mkdir(exist_ok=True)
        path = MLBA_DIR / f"reliever_traits_{season}.csv"

        keep = []
        for r in rows:
            team, g = r.get("TeamNameAbb"), _num(r, "G")
            if not team or "Tms" in str(team) or g <= 0:
                continue
            if _num(r, "GS") / g >= 0.5:
                continue
            games = tg.get(team, 0.0)
            if games <= 0 or (g / games) < min_app_rate:
                continue
            keep.append(r)

        itp: Dict[int, dict] = {}
        if with_itp:
            sess = _itp_session()
            ids = [pid for pid in (_row_id(r) for r in keep) if pid]
            print(f"[traits] fetching insidethepen for {len(ids)} arms...")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for pid, tr in zip(ids, ex.map(
                        lambda i: RelieverTraits.fetch_itp_traits(i, sess), ids)):
                    if tr:
                        itp[pid] = tr

        cols = list(RELIEVER_TRAIT_COLS) + list(ITP_TRAIT_COLS)
        n = 0
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in keep:
                team, g = r.get("TeamNameAbb"), _num(r, "G")
                games = tg.get(team, 0.0)
                pid = _row_id(r)
                w.writerow({**(itp.get(pid) or {}), **{
                    "player_name": r.get("PlayerName"),
                    "player_id": _row_id(r) or "",
                    "team": team, "season": season,
                    "g": int(g), "team_games": int(games),
                    "app_rate": round(g / games, 4),
                    "bf_per_outing": round(_num(r, "TBF") / g, 3),
                    "ip_per_outing": round(_num(r, "IP") / g, 3),
                    "gm_li": round(_num(r, "gmLI", 1.0), 3),
                    "sv": int(_num(r, "SV")), "hld": int(_num(r, "HLD")),
                    "siera": round(_num(r, "SIERA"), 3),
                }})
                n += 1
        print(f"[traits] wrote {n} relievers to {path}")
        return path

    @staticmethod
    def load_reliever_traits(season: Optional[int] = None) -> Dict[int, dict]:
        """{mlbam_id: traits} from the CSV, generated on first use if absent."""
        season = CURRENT_SEASON if season is None else int(season)
        if season in _TRAITS:
            return _TRAITS[season]
        path = MLBA_DIR / f"reliever_traits_{season}.csv"
        if not path.exists():
            RelieverTraits.export_reliever_traits(season)
        out: Dict[int, dict] = {}
        try:
            with open(path) as fh:
                for row in csv.DictReader(fh):
                    try:
                        pid = int(row["player_id"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    rec = {}
                    for k, v in row.items():
                        if v in (None, ""):
                            continue
                        try:                       # numeric where possible
                            rec[k] = float(v)
                        except (TypeError, ValueError):
                            rec[k] = v             # names, teams, role labels
                    out[pid] = rec
        except OSError:
            pass
        _TRAITS[season] = out
        return out

    @staticmethod
    def _itp_login_into(s) -> bool:
        """Log `s` in and persist the cookie jar. False on any failure."""
        try:
            import Creds
            email = getattr(Creds, "INSIDETHEPEN_EMAIL", None)
            pw = getattr(Creds, "INSIDETHEPEN_PASSWORD", None)
        except Exception:
            email = pw = None
        if email and pw:
            try:
                # The form needs a CSRF token from the login page, and the password
                # field is `pass2` — not `password`. Posting the obvious field
                # names returns 200 and simply leaves you logged out, so the traits
                # come back empty rather than erroring.
                from bs4 import BeautifulSoup
                r = s.get(f"{InsideThePen.BASE}/login.html", timeout=20)
                tok = BeautifulSoup(r.content, "lxml").find(
                    "input", {"name": "csrf_token"})
                r2 = s.post(f"{InsideThePen.BASE}/login.html", timeout=20, data={
                    "csrf_token": tok.get("value") if tok else "",
                    "email": email, "pass2": pw, "stayin": "1"})
                ok = "logout" in r2.text.lower() or "login.html" not in r2.url
                if ok:
                    try:
                        ITP_COOKIES_FILE.write_text(json.dumps(dict(s.cookies)))
                    except Exception:
                        pass
                else:
                    print("[itp] login FAILED — gated traits will be missing")
                return ok
            except Exception as e:
                print(f"[itp] login error: {e}")
        return False

    @staticmethod
    def _yn(v: Optional[str]) -> Optional[float]:
        if v is None:
            return None
        t = str(v).strip().lower()
        return 1.0 if t.startswith("y") else (0.0 if t.startswith("n") else None)

    @staticmethod
    def fetch_itp_traits(pid: int, session=None) -> dict:
        """One reliever's deployment traits from insidethepen. {} on any failure."""
        s = session or _itp_session()
        out: dict = {}
        try:
            r = s.get(f"{InsideThePen.BASE}/pitcher/x-{pid}.html", timeout=20)
            if r.status_code != 200:
                return out
            from bs4 import BeautifulSoup
            flat = BeautifulSoup(r.content, "lxml").get_text("\n", strip=True)
            traits = {}
            for label in ITP_TRAIT_LABELS:
                mm = re.search(re.escape(label) + r":?\s*\n?([^\n]+)", flat)
                if mm:
                    traits[label] = mm.group(1).strip()
            mm = re.search(r"Primary Role\(s\):\s*\n?([^\n]+)", flat)
            if mm:
                out["itp_role"] = mm.group(1).strip()
            mm = re.search(r"IP \(last 7 games\):\s*\n?([\d.]+)", flat)
            if mm:
                out["itp_ip7"] = float(mm.group(1))
            def num(lbl):
                v = traits.get(lbl)
                try:
                    return float(str(v).strip())
                except (TypeError, ValueError):
                    return None
            out["itp_avg_inning"] = num("Avg Inning when called")
            out["itp_avg_run_diff"] = num("Avg Run Diff when called")
            out["itp_back_to_back"] = RelieverTraits._yn(traits.get("back to back days"))
            out["itp_over_30"] = RelieverTraits._yn(traits.get("over 30 pitches"))
            out["itp_before_8th"] = RelieverTraits._yn(traits.get("before the 8th"))
            out["itp_vs_lh"] = RelieverTraits._yn(traits.get("versus LH batters"))
            out["itp_vs_rh"] = RelieverTraits._yn(traits.get("versus RH batters"))
        except Exception as e:
            print(f"[itp] {pid}: {e}")
        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def itp_bullpen_cache_path(abbr: str, date: Optional[str] = None,
                               save_dir: Path = SAVE_DIR) -> Path:
        d = date or datetime.date.today().isoformat()
        return Path(save_dir) / "itp" / d / f"{normalize_club(abbr)}.json"

    @staticmethod
    def load_itp_bullpen(abbr: str, date: Optional[str] = None,
                         save_dir: Path = SAVE_DIR,
                         session=None, timeout: float = 25.0,
                         refresh: bool = False) -> dict:
        """One club's bullpen, fetched at most ONCE PER DAY.

        A bullpen page changes when the club plays, so the natural key is the
        DATE; anything finer re-fetches the same page. Without this, pricing a
        slate twice was 60 fetches and a day of sweeps ran to four figures — which
        is what produced the read timeouts.

        **A timeout is written to the cache as a MISS, not as an empty pen.**
        `fetch_itp_bullpen` returns {} on failure, which callers must read as
        "unknown", never "everyone is available".
        """
        path = RelieverTraits.itp_bullpen_cache_path(abbr, date, save_dir)
        if path.exists() and not refresh:
            try:
                got = json.loads(path.read_text())
                if got.get("pen"):
                    return got
            except Exception:
                pass
        got = RelieverTraits.fetch_itp_bullpen(abbr, session=session, timeout=timeout)
        if got.get("pen"):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(got))
            tmp.replace(path)              # atomic: a killed write cannot leave a
                                           # half-parsed bullpen behind
        return got

    @staticmethod
    def fetch_itp_bullpen(abbr: str, session=None,
                          timeout: float = 25.0) -> dict:
        """One club's CURRENT bullpen and its seven-day workload.

        Returns {"pen": [{name, hand, eff, ftg, ip, g, era, fip}],
                 "workload": {name: {date_label: {ip, bf, pitches, strikes}}},
                 "days": [date labels, most recent first]}
        and {} on any failure — callers must treat that as "unknown", never as
        "everyone is available".
        """
        from bs4 import BeautifulSoup
        s = session or _itp_session()
        ab = _ITP_ALIAS.get(abbr, abbr)
        try:
            r = s.get(InsideThePen.TEAM_URL.format(abbr=ab), timeout=timeout)
            if r.status_code != 200:
                return {}
            soup = BeautifulSoup(r.content, "lxml")
        except Exception as e:
            print(f"[itp] {abbr} bullpen: {e}")
            return {}

        pen, workload, days = [], {}, []
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            if not rows:
                continue
            hdr = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
            if hdr[:2] == ["HND", "Pitcher"]:
                for tr in rows[1:]:
                    c = [x.get_text(" ", strip=True) for x in tr.find_all(["th", "td"])]
                    if len(c) < 2 or not c[1]:
                        continue
                    def num(i):
                        try:
                            return float(c[i])
                        except (ValueError, IndexError):
                            return None
                    pen.append({"name": c[1], "hand": c[0], "eff": num(2),
                                "ftg": num(3), "ip": num(4), "g": num(5),
                                "era": num(6), "fip": num(7)})
            elif hdr[:1] == ["Player"]:
                # Date columns look like "Aug-14". The grid also has IP / NP-S /
                # ERA summary columns, and "NP-S" contains a hyphen too — letting
                # it through made days[0] a non-date, so every arm read as rested
                # yesterday and therefore available.
                days = [h for h in hdr[1:] if _RE_ITP_DAY.match(h)]
                for tr in rows[1:]:
                    c = [x.get_text(" ", strip=True) for x in tr.find_all(["th", "td"])]
                    if len(c) < 2 or not c[0]:
                        continue
                    per = {}
                    for lab, cell in zip(hdr[1:], c[1:]):
                        if lab in days:
                            w = _itp_cell_workload(cell)
                            if w:
                                per[lab] = w
                    # **Key on the NORMALISED name.** The pen table tags roles
                    # onto the name ("Edwin Díaz CL") and the workload grid does
                    # not, so a raw-string lookup silently missed every CLOSER —
                    # Díaz threw 26 pitches then 24 and still read "available".
                    workload[_norm_name(_itp_clean_name(c[0]))] = per
        return {"pen": pen, "workload": workload, "days": days} if pen else {}

    @staticmethod
    def copy_pitcher(p: "Pitcher") -> "Pitcher":
        """A fresh Pitcher with the same inputs — the sim mutates per-game state."""
        return copy.copy(p)

    @staticmethod
    def validate_bullpen_usage(teams: Sequence[str] = (), n: int = 3000,
                               season: Optional[int] = None, seed: int = 11) -> List[dict]:
        """Does the sim REPRODUCE each reliever's measured usage?

        A trait that is loaded but not reproduced is not modelled. Compares the
        traits file against the simulated games on `app_rate`, `avg_inning` and
        `bf_per_outing`. League marks to check against: appearance rate median
        10.8%, p90 41.2%, **max 53.4%** — any simulated arm above ~50% is wrong by
        construction, which is what happens with no availability model.
        """
        season = CURRENT_SEASON if season is None else int(season)
        bat_table, _ = build_rates("bat")
        pit_table, _ = build_rates("pit")
        traits = RelieverTraits.load_reliever_traits(season)
        hz = starter_hazard()
        clubs = list(teams) or sorted(team_roster("bat", season))[:8]
        out: List[dict] = []
        for club in clubs:
            try:
                side = build_side(club, bat_table, pit_table, season, hz)
                opp = build_side(
                    [c for c in sorted(team_roster("bat", season)) if c != club][0],
                    bat_table, pit_table, season, hz)
            except ValueError:
                continue
            res = simulate_many(opp, side, n=n, seed=seed)   # `side` is away
            app = {p.name: 0 for p in side.bullpen}
            outs = {p.name: 0 for p in side.bullpen}
            for r in res:
                for nm in app:
                    line = r.pitchers.get(nm)
                    if line and line.bf:
                        app[nm] += 1
                        outs[nm] += line.outs
            for p in side.bullpen:
                tr = traits.get(p.player_id or -1) or {}
                a = app[p.name]
                out.append({
                    "team": club, "name": p.name,
                    "app_actual": float(tr.get("app_rate", float("nan"))),
                    "app_sim": a / len(res),
                    "ip_actual": float(tr.get("ip_per_outing", float("nan"))),
                    "ip_sim": (outs[p.name] / a / 3.0) if a else 0.0,
                    "avg_inning": tr.get("itp_avg_inning"),
                })
        return out


_TRAITS: Dict[int, Dict[int, dict]] = {}


# --- InsideThePen deployment traits ----------------------------------------
# The FanGraphs board says how OFTEN and in what LEVERAGE an arm is used, not
# the things a manager decides on: entry inning, run differential when called,
# back-to-back days, over-30-pitch capacity, before-the-8th role, and platoon
# specialisation. `EffortMLB.fetch_reliever_page_sync` fetches these live, but
# importing that module pulls in Qt, so the fetch is duplicated here Qt-free and
# the RESULT is written to the CSV. **The CSV is the interface; the sim never
# touches the network.** sim_state.md A.14.

ITP_TRAIT_LABELS = (
    "Games Pitched this Season", "Games Started this Season",
    "versus LH batters", "versus RH batters",
    "Avg Inning when called", "Avg Run Diff when called",
    "over 30 pitches", "before the 8th", "back to back days",
)
ITP_TRAIT_COLS = ("itp_role", "itp_ip7", "itp_avg_inning", "itp_avg_run_diff",
                  "itp_back_to_back", "itp_over_30", "itp_before_8th",
                  "itp_vs_lh", "itp_vs_rh")


ITP_COOKIES_FILE = SHARED_DIR / "itp_cookies.json"
_ITP_SESSION = []          # at most one; a list so it survives re-import
_ITP_LOCK = __import__("threading").Lock()


def _itp_session():
    """ONE logged-in session, cookies persisted, reused for the process.

    **This used to log in on every call**, two round-trips per bullpen, with no
    cache on `fetch_itp_bullpen` — so pricing a 15-game slate twice was 180
    requests and a day of sweeps ran to four figures. That is what times
    insidethepen out, and the timeouts then read as "bullpen unknown" and fell
    back to the season board. Mirrors `EffortMLB`'s scheme rather than inventing a
    second one; they share the cookie file, so a login in either warms both.
    """
    with _ITP_LOCK:
        if _ITP_SESSION:
            return _ITP_SESSION[0]
        s = requests.Session()
        s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64; rv:144.0) "
                                   "Gecko/20100101 Firefox/144.0")
        if ITP_COOKIES_FILE.exists():
            try:
                s.cookies.update(json.loads(ITP_COOKIES_FILE.read_text()))
                _ITP_SESSION.append(s)
                return s                      # trust the jar; a dead cookie
                                              # costs one failed page, not a
                                              # login on every call
            except Exception:
                pass
        RelieverTraits._itp_login_into(s)
        _ITP_SESSION.append(s)
        return s


# ---------------------------------------------------------------------------
# The REAL bullpen state — insidethepen's per-team page
# ---------------------------------------------------------------------------
# **The authority on pen composition and workload, replacing reconstruction of
# either.** `/team/<ABBR>-bullpen.html` is UNGATED and carries the CURRENT 7-8
# arm pen — the season board gives the UNION of every pen a club used all year
# (24.2 arms), which puts a July call-up in an April game — plus a SEVEN-DAY
# workload grid with PITCH COUNTS, which is the availability state directly.
# The pre-digested page is premium-gated for 28 of 30 clubs. This is TODAY's
# state, so a BACKTEST still needs `appearance_dates`.
_RE_ITP_DAY = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{1,2}$")

# insidethepen uses StatsAPI-style abbreviations, not the board's.
_ITP_ALIAS = {v: k for k, v in _FG_ALIAS.items()}


def _itp_cell_workload(txt: str) -> Optional[dict]:
    """'1.0 9 27-18' -> {ip, bf, pitches, strikes}. None when the arm rested."""
    t = (txt or "").strip()
    if not t:
        return None
    parts = t.split()
    out: dict = {}
    try:
        out["ip"] = float(parts[0])
        if len(parts) > 1:
            out["bf"] = int(parts[1])
        if len(parts) > 2 and "-" in parts[2]:
            p, st = parts[2].split("-", 1)
            out["pitches"] = int(p)
            out["strikes"] = int(st)
    except (ValueError, IndexError):
        return None
    return out or None


# Pitch-count threshold for next-day availability. `back to back days` on an
# arm's ITP page says whether he is USED that way at all; this gates on what he
# actually threw.
#
# **`ITP_BACK_TO_BACK_PITCHES = 15` was REMOVED 2026-08-24.** It read as the
# second half of a live rule and reached nothing: `itp_availability` makes a
# TWO-way split, so a 15-pitch outing and a 24-pitch one are already identical.
# Same family and precedent as `ENTRY_INNING_SCALE` (4j).
ITP_HEAVY_PITCHES = 25          # a heavy outing yesterday -> very likely rest


def itp_availability(state: dict, skip_today: bool = True) -> Dict[str, str]:
    """{pitcher name: 'available' | 'likely_rest'} from real recent workload.

    Reads the seven-day grid rather than a season-average frequency: two straight
    days, or one heavy outing yesterday, means rest — three straight is not a
    thing, measured.

    **The grid's FIRST column is TODAY, not yesterday** — the page is built for
    the current date, so that column is empty until the games are played, and
    reading it as "yesterday" made every arm look rested. `skip_today=False` is
    for a grid already trimmed to completed days.
    """
    days = list(state.get("days") or [])
    if skip_today and days:
        days = days[1:]
    work = state.get("workload") or {}
    out: Dict[str, str] = {}
    for p in state.get("pen") or []:
        nm = p["name"]
        per = work.get(_norm_name(_itp_clean_name(nm))) or {}
        y = per.get(days[0]) if len(days) > 0 else None
        d2 = per.get(days[1]) if len(days) > 1 else None
        if y and (d2 or (y.get("pitches") or 0) >= ITP_HEAVY_PITCHES):
            out[nm] = "likely_rest"
        else:
            out[nm] = "available"
    return out


# How much worse than league average an arm with no board row is. A pitcher
# with no FanGraphs line is a fresh call-up, not a league-average reliever — the
# same trap as defaulting `app_rate`. Deliberately coarse: the alternative is
# dropping him from the roster, which is worse.
REPLACEMENT_TILT = -0.06


def replacement_pitcher_rates() -> List[float]:
    """League baseline tilted to replacement level, from the PITCHER's side."""
    return offence_tilt(list(LEAGUE_BASELINE), -REPLACEMENT_TILT)


def replacement_batter(season: Optional[int] = None,
                       save_dir: Path = SAVE_DIR) -> "Batter":
    """A hitter the rate layer has never seen — a callup with no board row.

    A player nobody has a line for is not league average; he is who a club
    reaches for once it has run out of the ones it preferred. The running game
    stays at the league marks, since there is nothing else to go on.

    **The tilt sign is OPPOSITE to `replacement_pitcher_rates`** — `offence_tilt`
    RAISES offence on a negative argument. Copying the pitcher's sign made the
    unknown callup a 0.330 on-base hitter against a league 0.317, an upgrade on
    the average regular.
    """
    return Batter(name="replacement",
                  rates=offence_tilt(list(LEAGUE_BASELINE),
                                     REPLACEMENT_TILT))


# Role tags insidethepen appends to the name cell ("David Bednar CL").
_ITP_ROLE_TAGS = ("CL", "SU", "SP", "LR", "MR")


def _itp_clean_name(name: str) -> str:
    parts = (name or "").split()
    while parts and parts[-1] in _ITP_ROLE_TAGS:
        parts.pop()
    return " ".join(parts)


def _norm_name(name: str) -> str:
    t = unicodedata.normalize("NFKD", name or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return "".join(c for c in t.lower() if c.isalnum())


_ITP_PEN_CACHE: Dict[tuple, tuple] = {}


def build_pen_from_itp(abbr: str, pit_table: Dict[int, dict],
                       session=None, drop_resting: bool = True,
                       cache: bool = True) -> Tuple[List["Pitcher"], dict]:
    """The club's REAL current bullpen, as Pitcher objects.

    Returns (pen, report). `report` says what was matched and what was not,
    because a silent name-match failure is indistinguishable from a short pen.

    Arms reading `likely_rest` are dropped when `drop_resting` — that is the
    whole point, and it is real state rather than a frequency draw.
    """
    key = (abbr, drop_resting)
    if cache and key in _ITP_PEN_CACHE:
        pen, rep = _ITP_PEN_CACHE[key]
        return [RelieverTraits.copy_pitcher(p) for p in pen], dict(rep)
    # `_ITP_PEN_CACHE` above is in-PROCESS and dies with the interpreter, so a
    # day of separate A/B scripts re-fetched every club every time. The disk
    # cache is keyed on the DATE, which is when the page actually changes.
    state = RelieverTraits.load_itp_bullpen(abbr, session=session)
    if not state:
        return [], {"ok": False, "reason": "itp fetch failed"}
    by_name = {}
    for pid, rec in pit_table.items():
        by_name.setdefault(_norm_name(rec.get("name", "")), (pid, rec))
    avail = itp_availability(state)

    pen, missing, rested = [], [], []
    traits = RelieverTraits.load_reliever_traits(2026)
    for row in state["pen"]:
        raw = row["name"]
        nm = _itp_clean_name(raw)
# **A resting arm stays on the ROSTER; he is just not available.** Deleting him
# shortened the pen — Oakland went to FOUR arms — which this function's own
# comment below calls the same error as truncating at 8. The consequence was a
# BROKEN pen, not a thin one: an empty pen on 50.8% of change decisions against
# under 4% elsewhere, and since all three call sites read `if nxt is not None`
# the man on the mound simply stayed, unremovable. Rest belongs in
# `_choose_reliever`'s READY tier — out of arms, a tired one pitches the 12th.
        resting = bool(drop_resting and avail.get(raw) == "likely_rest")
        if resting:
            rested.append(nm)
        hit = by_name.get(_norm_name(nm))
        arm = make_pitcher(hit[0], pit_table) if hit else None
        pid = hit[0] if hit else None
        if arm is None:
            # A call-up with no board row. Do NOT drop him — that silently
            # shortens the pen and hands his innings to better arms, which is
            # the same error as truncating the pen at 8. Replacement level is
            # the honest stand-in, and it is what these arms mostly are.
            missing.append(nm)
            arm = Pitcher(name=nm, rates=replacement_pitcher_rates(),
                          player_id=None)
            arm.app_rate = 0.20
        tr = traits.get(pid) or {}
        arm.app_rate = float(tr.get("app_rate") or 0.35)
        arm.bf_per_outing = float(tr.get("bf_per_outing", 4.0))
        arm.avg_inning = tr.get("itp_avg_inning")
        arm.avg_run_diff = tr.get("itp_avg_run_diff")
        arm.back_to_back = tr.get("itp_back_to_back")
        arm.throws = ("L" if (row.get("hand") or "").upper().startswith("L")
                      else "R")
        arm.multi_inning = float(tr.get("ip_per_outing", 1.0)) >= 1.25
        arm.availability = 0.0 if resting else 1.0
        pen.append(arm)
    rep = {"ok": bool(pen), "n_itp": len(state["pen"]),
           "matched": len(pen), "rested": rested, "unmatched": missing,
           # arms on the roster vs arms usable TONIGHT. A short pen and a
           # rested one are different problems and the report must not read
           # the same for both.
           "available": sum(1 for p in pen if p.availability > 0.0),
           "days": state.get("days", [])}
    if cache:
        _ITP_PEN_CACHE[key] = (pen, rep)
    return [RelieverTraits.copy_pitcher(p) for p in pen], dict(rep)


# ===========================================================================
# 15. OBSERVED RELIEVER ENTRIES — ground truth for deployment
# ===========================================================================
# The deployment scales in section 13 were nudged against a single pen. This
# extracts what managers ACTUALLY did: every pitching change in a sample of real
# games, with the state at the moment of the change and both handednesses. That
# gives the real per-role distribution of entry inning / margin / leverage —
# consumed DIRECTLY by `build_deployment` as per-arm histograms — and the real
# size of the handedness effect, which is otherwise an assertion.

# Keyed on SEASON. It was a single file, so a 2025 backtest was scored with
# deployment built from 2026 play-by-play — not merely look-ahead but the WRONG
# SEASON, with arms who did not exist yet and roles that had since changed.
ENTRY_CACHE_FMT = "reliever_entries_{season}.json"


def entry_cache_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
    p = Path(save_dir) / ENTRY_CACHE_FMT.format(season=season)
    if not p.exists() and season == 2026:
        legacy = Path(save_dir) / "reliever_entries.json"   # pre-2026-08 name
        if legacy.exists():
            return legacy
    return p


class RelieverUsage:
    """Observed entries, rest and appearance shape — ground truth for deployment."""

    @staticmethod
    def fetch_pbp_entries(game_pk: int, timeout: float = 20.0) -> List[dict]:
        """Every pitching change in one game, with the state at the change.

        A change is detected by the pitcher id differing from the previous plate
        appearance. The STARTER's first appearance is skipped — he did not enter,
        he began.
        """
        out: List[dict] = []
        try:
            plays = PlayByPlay.play_by_play(game_pk, timeout, final=True)
        except Exception:
            return out

        prev = {"top": None, "bot": None}
        for p in plays:
            about, mu, res = p.get("about") or {}, p.get("matchup") or {}, p.get("result") or {}
            is_top = bool(about.get("isTopInning"))
            side = "top" if is_top else "bot"          # side that is BATTING
            pid = (mu.get("pitcher") or {}).get("id")
            if pid is None:
                continue
            if prev[side] is None:                      # the starter
                prev[side] = pid
                continue
            if pid == prev[side]:
                continue
            prev[side] = pid
            away, home = res.get("awayScore", 0), res.get("homeScore", 0)
            # margin from the PITCHING side: a top-inning pitcher is the home club
            margin = (home - away) if is_top else (away - home)
            runners = p.get("runners") or []
            on = len({(rn.get("movement") or {}).get("start")
                      for rn in runners
                      if (rn.get("movement") or {}).get("start") in
                      ("1B", "2B", "3B")})
            out.append({
                "game_pk": game_pk,
                "pitcher": pid,
                # The pitching side is HOME when the top of the inning is batting.
                "p_home": bool(is_top),
                "p_hand": (mu.get("pitchHand") or {}).get("code"),
                "batter": (mu.get("batter") or {}).get("id"),
                "b_hand": (mu.get("batSide") or {}).get("code"),
                "inning": about.get("inning"),
                "margin": margin,
                "outs": (p.get("count") or {}).get("outs", 0),
                "on_base": on,
            })
        return out

    @staticmethod
    def appearance_dates(season: Optional[int] = None) -> Dict[int, List[str]]:
        """{pitcher id: sorted ISO dates he appeared in relief}."""
        season = CURRENT_SEASON if season is None else int(season)
        if season in _APPEARANCES:
            return _APPEARANCES[season]
        dates = game_dates(season)
        out: Dict[int, set] = {}
        try:
            with open(entry_cache_path(season)) as fh:
                entries = json.load(fh)
        except (OSError, ValueError):
            entries = []
        for e in entries:
            d = dates.get(e.get("game_pk"))
            pid = e.get("pitcher")
            if d and pid:
                out.setdefault(int(pid), set()).add(d)
        _APPEARANCES[season] = {k: sorted(v) for k, v in out.items()}
        return _APPEARANCES[season]

    @staticmethod
    def _days_before(iso: str, n: int) -> str:
        y, m, d = (int(x) for x in iso.split("-"))
        return (datetime.date(y, m, d)
                - datetime.timedelta(days=n)).isoformat()

    @staticmethod
    def season_game_pks(season: Optional[int] = None, save_dir: Path = SAVE_DIR) -> List[int]:
        """Every completed game id for a season.

        Prefers the PBP accumulator when it exists (2026 only), and otherwise falls
        back to the SLATE, which is cached for any season the backtest can reach.
        Without the fallback there was no way to collect 2025 entries at all.
        """
        season = CURRENT_SEASON if season is None else int(season)
        path = RateIngest._shared(save_dir) / "pbp" / f"season_{season}_v2.json"
        if path.exists():
            try:
                with open(path) as fh:
                    return json.load(fh)["games"]
            except (OSError, ValueError, KeyError):
                pass
        return [r["pk"] for r in season_slate(season, save_dir=save_dir) if r.get("pk")]

    @staticmethod
    def collect_reliever_entries(n_games: int = 0, workers: int = 12,
                                 refresh: bool = False, season: Optional[int] = None,
                                 save_dir: Path = SAVE_DIR) -> List[dict]:
        """Pitching changes across a season, cached to disk PER SEASON."""
        season = CURRENT_SEASON if season is None else int(season)
        path = entry_cache_path(season, save_dir)
        if path.exists() and not refresh:
            try:
                with open(path) as fh:
                    cached = json.load(fh)
                if len(cached) > 0:
                    return cached
            except (OSError, ValueError):
                pass
        pks = RelieverUsage.season_game_pks(season, save_dir)
        pks = pks[-n_games:] if n_games else pks
        print(f"[entries] {season}: fetching play-by-play for {len(pks)} games...")
        out: List[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for rows in ex.map(RelieverUsage.fetch_pbp_entries, pks):
                out.extend(rows)
        dest = Path(save_dir) / ENTRY_CACHE_FMT.format(season=season)
        with open(dest, "w") as fh:
            json.dump(out, fh)
        print(f"[entries] {season}: {len(out)} changes from {len(pks)} games")
        return out

    @staticmethod
    def fetch_pbp_stints(game_pk: int, timeout: float = 20.0) -> List[dict]:
        """Every pitcher's STINT in one game — batters faced, outs, innings.

        A stint is a maximal run of consecutive plate appearances by the same
        pitcher for one side. `mid_entry` is True when his first batter was not
        the first batter of a half-inning, which is the inherited-runner rescue.
        """
        try:
            plays = PlayByPlay.play_by_play(game_pk, timeout, final=True)
        except Exception:
            return []

        stints: Dict[str, List[dict]] = {"top": [], "bot": []}
        prev_outs = {"top": 0, "bot": 0}
        prev_half = {"top": None, "bot": None}
        for p in plays:
            about, mu = p.get("about") or {}, p.get("matchup") or {}
            side = "top" if about.get("isTopInning") else "bot"
            pid = (mu.get("pitcher") or {}).get("id")
            if pid is None:
                continue
            inning = about.get("inning")
            half_key = (inning, side)
            first_of_half = prev_half[side] != half_key
            if first_of_half:
                prev_half[side] = half_key
                prev_outs[side] = 0
            outs_after = (p.get("count") or {}).get("outs", 0)
            got = max(outs_after - prev_outs[side], 0)
            prev_outs[side] = outs_after

            cur = stints[side][-1] if stints[side] else None
            if cur is None or cur["pitcher"] != pid:
                stints[side].append({
                    "game_pk": game_pk, "pitcher": pid, "side": side,
                    "starter": cur is None,
                    "mid_entry": (not first_of_half) and cur is not None,
                    "entry_inning": inning,
                    "bf": 0, "outs": 0, "innings": set(), "pitches": 0,
                })
                cur = stints[side][-1]
            cur["bf"] += 1
            cur["outs"] += got
            cur["innings"].add(inning)
            # **Pitches per STINT, from the same response.** A manager hooks on
            # the pitch count and this engine hooks on BATTERS FACED. Counted off
            # `playEvents` rather than the boxscore, because the boxscore is per
            # PITCHER and a pitcher can have two stints.
            cur["pitches"] += sum(1 for e in (p.get("playEvents") or [])
                                  if e.get("isPitch"))

        out: List[dict] = []
        for side in ("top", "bot"):
            for s in stints[side]:
                s["innings"] = len(s["innings"])
                out.append(s)
        return out

    @staticmethod
    def collect_reliever_stints(n_games: int = 0, workers: int = 12,
                                refresh: bool = False) -> List[dict]:
        """Every pitcher stint over the season's play-by-play, cached to disk."""
        if STINT_CACHE.exists() and not refresh:
            try:
                with open(STINT_CACHE) as fh:
                    cached = json.load(fh)
                if cached:
                    return cached
            except (OSError, ValueError):
                pass
        with open(SHARED_DIR / "pbp" / "season_2026_v2.json") as fh:
            pks = json.load(fh)["games"]
        pks = pks[-n_games:] if n_games else pks
        print(f"[stints] fetching play-by-play for {len(pks)} games...")
        out: List[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for rows in ex.map(RelieverUsage.fetch_pbp_stints, pks):
                out.extend(rows)
        with open(STINT_CACHE, "w") as fh:
            json.dump(out, fh)
        print(f"[stints] {len(out)} stints from {len(pks)} games")
        return out


# ---------------------------------------------------------------------------
# Who is actually available tonight — rest, from real recent usage
# ---------------------------------------------------------------------------
# The per-game availability draw was a season-average FREQUENCY with no memory.
# **Measured over 2026**: back-to-back is 16.8% of appearances against a base
# rate near 34% of days, so an arm who pitched yesterday is roughly HALF as
# likely to pitch today; and **three days in a row essentially never happens** —
# across Oakland's whole season not one reliever did it. So availability is a
# STATE carried from the previous days, and for a real slate it is knowable
# rather than modelled.
MAX_CONSECUTIVE_DAYS = 2
P_PITCH_ON_ZERO_REST = 0.50      # relative to his normal chance, measured 16.8/34

# Keyed on SEASON. Both were bare globals, so the first season loaded was
# served for every later request — the same silent no-op that would have made
# TEAM_CONTEXT_LAG report success while changing nothing.
_GAME_DATES: Dict[int, Dict[int, str]] = {}
_APPEARANCES: Dict[int, Dict[int, List[str]]] = {}


def game_dates(season: Optional[int] = None, save_dir: Path = SAVE_DIR,
               refresh: bool = False) -> Dict[int, str]:
    """{game_pk: 'YYYY-MM-DD'} for the season, cached."""
    season = CURRENT_SEASON if season is None else int(season)
    if season in _GAME_DATES and not refresh:
        return _GAME_DATES[season]
    path = save_dir / f"game_dates_{season}.json"
    if path.exists() and not refresh:
        try:
            with open(path) as fh:
                _GAME_DATES[season] = {int(k): v
                                       for k, v in json.load(fh).items()}
                return _GAME_DATES[season]
        except (OSError, ValueError):
            pass
    out: Dict[int, str] = {}
    url = StatsApi.schedule_url(start=f"{season}-03-01",
                                end=f"{season}-11-15", game_type="R")
    try:
        data = requests.get(url, timeout=StatsApi.SLOW_TIMEOUT).json()
        for day in data.get("dates", []):
            for g in day.get("games", []):
                out[int(g["gamePk"])] = day["date"]
    except Exception as e:
        print(f"mlb_sim: game_dates failed: {e}")
        return {}
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({str(k): v for k, v in out.items()}, fh)
    except OSError:
        pass
    _GAME_DATES[season] = out
    return out


# A club uses 24.2 relievers across a season but carries only ~8 at a time —
# the season list is a UNION of many different pens, not one pen. Carrying all
# of them into every game lets a July call-up pitch in April and flattens the
# usage distribution across arms who were never on the roster together. An arm
# counts as rostered for a date if he appeared within this many days of it.
PEN_ROSTER_WINDOW_DAYS = 14


def available_bullpen(bullpen: Sequence["Pitcher"], on_date: Optional[str],
                      rng: random.Random, season: Optional[int] = None,
                      window: Optional[int] = None,
                      future: bool = False) -> List["Pitcher"]:
    """Filter a pen to the arms that could realistically pitch on `on_date`.

    REAL recent usage, not a season-average frequency: off the roster around this
    date -> not in the pen; `MAX_CONSECUTIVE_DAYS` in a row -> unavailable, since
    a third straight day essentially never happens; pitched yesterday ->
    `P_PITCH_ON_ZERO_REST` of normal.

    `future=True` looks only BACKWARD for the roster test, which is what a live
    projection must do; the default also looks forward, correct for a backtest and
    wrong for a forecast. With no date this is a no-op — the honest default, since
    the alternative is to invent a rest state.
    """
    window = PEN_ROSTER_WINDOW_DAYS if window is None else int(window)
    season = CURRENT_SEASON if season is None else int(season)
    if not on_date:
        return list(bullpen)
    app = RelieverUsage.appearance_dates(season)
    prior = [RelieverUsage._days_before(on_date, k)
             for k in range(1, MAX_CONSECUTIVE_DAYS + 1)]
    lo = RelieverUsage._days_before(on_date, window)
    hi = RelieverUsage._days_before(on_date, -window) if not future else on_date
    out = []
    for p in bullpen:
        ds = app.get(int(p.player_id)) if p.player_id else None
        if not ds:
            out.append(p)
            continue
        if not any(lo <= d <= hi for d in ds):
            continue                       # not on the roster around this date
        s = set(ds)
        if all(d in s for d in prior):
            continue                       # three straight days: never
        if prior[0] in s and rng.random() > P_PITCH_ON_ZERO_REST:
            continue                       # back-to-back, and today he rests
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# How LONG a relief appearance is — ground truth for sim_state.md 5.6
# ---------------------------------------------------------------------------
# The board gives the mean (4.481 TBF, 1.033 IP per outing) but not the SHAPE,
# and the shape is what 5.6 is about: an arm who touches two innings is a
# different usage pattern from one who faces six men in one, and both average
# the same. Note what is NOT measurable from the entries cache — it records
# where a reliever came IN, never where he went out.
STINT_CACHE = SAVE_DIR / "reliever_stints.json"


def stint_profile(stints: Optional[Sequence[dict]] = None,
                  team_games: Optional[int] = None) -> dict:
    """Relief-appearance shape: BF, outs and innings touched.

    Takes stints from either source — `collect_reliever_stints` (real) or
    `sim_stints` — so the two are scored by the SAME code and cannot drift
    apart on a definition.
    """
    rows = [s for s in (stints if stints is not None
                        else RelieverUsage.collect_reliever_stints()) if not s["starter"]]
    if not rows:
        return {}
    n = len(rows)
    inn = collections.Counter(s["innings"] for s in rows)
    tg = team_games or len({(s["game_pk"], s["side"]) for s in rows})
    return {
        "n": n,
        "apps_per_team_game": n / tg if tg else 0.0,
        "bf": statistics.mean(s["bf"] for s in rows),
        "outs": statistics.mean(s["outs"] for s in rows),
        "innings": statistics.mean(s["innings"] for s in rows),
        "mid_entry": sum(bool(s["mid_entry"]) for s in rows) / n,
        "multi_inning": sum(v for k, v in inn.items() if k >= 2) / n,
        "by_innings": {int(k): v / n for k, v in sorted(inn.items())},
    }


def sim_stints(log: Sequence[dict], game_pk: int = 0) -> List[dict]:
    """`simulate_game(log=[])` -> the same stint rows `fetch_pbp_stints` makes.

    `half` in the log is the side BATTING, so it identifies the pitching staff
    just as well; a stint is a maximal run of consecutive plate appearances by
    one pitcher within that stream.
    """
    out: List[dict] = []
    for half in ("home", "away"):
        stream = [ev for ev in log if ev["half"] == half]
        cur = None
        prev_inning = None
        for ev in stream:
            first_of_half = ev["inning"] != prev_inning
            prev_inning = ev["inning"]
            if cur is None or cur["pitcher"] != ev["pitcher"]:
                cur = {
                    "game_pk": game_pk, "pitcher": ev["pitcher"],
                    "side": half, "starter": cur is None,
                    "mid_entry": (not first_of_half) and cur is not None,
                    "entry_inning": ev["inning"],
                    "bf": 0, "outs": 0, "innings": set(),
                }
                out.append(cur)
            cur["bf"] += 1
            cur["outs"] += max(ev["outs_after"] - ev["outs_before"], 0)
            cur["innings"].add(ev["inning"])
    for s in out:
        s["innings"] = len(s["innings"])
    return out


def validate_stint_shape(n_games: int = 400, season: Optional[int] = None,
                         seed: int = 7, save_dir: Path = SAVE_DIR) -> dict:
    """Simulated relief-appearance shape against the real one — 5.6.

    The board gives only the mean. This is the distribution, and the
    distribution is where the defect is: matching BF per outing while touching
    too many innings means the sim is letting arms roll over inning
    boundaries instead of rescuing mid-inning.
    """
    season = CURRENT_SEASON if season is None else int(season)
    slate = season_slate(season, save_dir=save_dir)[:n_games]
    bat, _ = build_rates("bat", save_dir=save_dir)
    pit, _ = build_rates("pit", save_dir=save_dir)
    hz = starter_hazard()
    sides = slate_sides(slate, bat, pit, season, hz, save_dir)

    rows: List[dict] = []
    played = 0
    for idx, row in enumerate(slate):
        h, a = sides.get(row["home"]), sides.get(row["away"])
        if h is None or a is None:
            continue
        hs, _, _ = _game_side(h, row.get("home_sp"), row.get("home_lineup"),
                              bat, pit, season, save_dir,
                              row.get("home_catcher"))
        as_, _, _ = _game_side(a, row.get("away_sp"), row.get("away_lineup"),
                               bat, pit, season, save_dir,
                               row.get("away_catcher"))
        log: List[dict] = []
        simulate_game(hs, as_, random.Random(seed * 1_000_003 + idx), log=log,
                      weather=_slate_weather(row),
                      venue=resolve_venue(row["venue"]))
        rows += sim_stints(log, row["pk"])
        played += 1

    sim = stint_profile(rows, team_games=2 * played)
    real = stint_profile()
    return {"games": played, "sim": sim, "real": real}


# ===========================================================================
# 15b. BASE-RUNNING AND FRAMING, MEASURED — one play-by-play pass
# ===========================================================================
# §5.6c's pattern: *a plausible stand-in, written when the real data did not
# exist, surviving after it did* — and it never fails a test, because the MEAN
# is usually right and only the SHAPE is wrong.
#
# All of these are OUTCOME questions off the base-out state before the play, so
# one traversal answers them: P_SAC_FLY, P_GIDP, P_GB_ADVANCE, P_GB_SCORES,
# P_STEAL_SUCCESS and the FRAMING_K_SHARE count table.
#
# **§2's claim that the first four "cannot be measured" holds only for the
# MOVEMENT-RECORD method.** A runner who holds generates no record, so that
# method can only ever see the runners who moved. Counted as OUTCOMES instead —
# take the base-out state BEFORE the play and ask what happened — the holders
# are just the denominator minus the numerator, and nothing has to be inferred
# from an absence. sim_state.md A.2.
BASERUN_CACHE_FMT = "baserunning_{season}.json"
# `BASERUN_SEASON` is declared with the constants it feeds, in section 2.

# Statcast trajectories, split the way the rate model splits outs.
_TRAJ_GB = ("ground_ball",)
_TRAJ_AIR = ("fly_ball", "line_drive", "popup")
_TRAJ_BUNT_GB = ("bunt_grounder",)
_TRAJ_BUNT_AIR = ("bunt_popup", "bunt_line_drive")
_HIT_EVENTS = ("single", "double", "triple", "home_run")
# Half the rulebook plate, in feet, and how far off the edge still counts as a
# framing chance. Savant's own "shadow" band straddles the edge by about a
# ball's width either side; 0.25 ft is that band and it reproduces their
# chances-per-team-game to within a few percent.
_ZONE_HALF_W = 17.0 / 2.0 / 12.0
_SHADOW_FT = 0.25


def _is_running_event(name: str) -> bool:
    """A runner movement that is NOT the batted ball — the running game.

    Matched on the event NAME rather than a fixed set, because the feed spells
    these as 'Stolen Base 2B', 'Pickoff Caught Stealing 2B', 'Defensive
    Indifference' and so on, and a missed spelling would silently leave a
    stolen runner standing on first for the batted-ball classification.
    """
    n = (name or "").lower()
    return n.startswith(("stolen base", "caught stealing", "pickoff",
                         "wild pitch", "passed ball", "balk",
                         "defensive indifference"))


class BaseRunningPbp:
    """Base-running and run expectancy, measured in one play-by-play pass."""

    @staticmethod
    def fetch_pbp_baserunning(game_pk: int, timeout: float = 30.0) -> Dict[str, int]:
        """Every base-running outcome and every count in one game, as counters.

        Returns a flat {name: count} dict so the season merge is a `Counter`
        update and the cache is plain JSON. Empty on any failure — a missing game
        is a smaller error than a half-parsed one.
        """
        c: Dict[str, int] = collections.Counter()
        try:
            plays = PlayByPlay.play_by_play(game_pk, timeout, final=True)
        except Exception:
            return dict(c)
        c["games"] = 1

        state = {"1B": None, "2B": None, "3B": None}
        prev_half, prev_outs = None, 0
        # (bases bitmask, outs, runs on the play) for the half-inning in progress.
        # RE24 is banked a half-inning at a time because a half that did not end
        # in three outs — a walk-off, a called game — has to be dropped whole.
        half_rows: List[Tuple[int, int, int]] = []

        def _bank_re24() -> None:
            if not half_rows or prev_outs != 3:
                return
            tail = 0
            for base, outs, runs in reversed(half_rows):
                tail += runs
                if outs < 3:
                    c[f"re_{base}_{outs}_runs"] += tail
                    c[f"re_{base}_{outs}_n"] += 1

        for p in plays:
            about = p.get("about") or {}
            half = (about.get("inning"), about.get("isTopInning"))
            if half != prev_half:
                _bank_re24()
                half_rows = []
                prev_half, prev_outs = half, 0
                state = {"1B": None, "2B": None, "3B": None}
            res = p.get("result") or {}
            ev = res.get("eventType") or ""
            outs_after = (p.get("count") or {}).get("outs", 0)
            runners = p.get("runners") or []
            c["pa"] += 1
            # **Runs are counted off the SCORING MOVEMENTS, not off a score
            # difference.** `walk_half_innings` in EffortMLB.py — which produced
            # the RE24 table this is compared against — seeds its running score
            # from the FIRST play of the half, so a leadoff home run is silently
            # free and the loss lands on the (empty, 0 out) cell that every
            # run-expectancy calibration keys off.
            base_before = ((1 if state["1B"] else 0) | (2 if state["2B"] else 0)
                           | (4 if state["3B"] else 0))
            half_rows.append((base_before, prev_outs, sum(
                1 for rn in runners
                if ((rn.get("movement") or {}).get("end")) == "score")))

            # --- opportunity is measured on the state the PA STARTED with, which
            # is where `running_game` is called from in the engine.
            if any(state.values()):
                c["runner_on_pa"] += 1
            if state["1B"] is not None and state["2B"] is None:
                c["steal_opp"] += 1

            # --- the running game resolves first, and it moves both the bases and
            # the out count before the ball is ever put in play.
            pre_outs = prev_outs
            at_contact = dict(state)
            wild = False
            for rn in runners:
                det, mv = rn.get("details") or {}, rn.get("movement") or {}
                evn = det.get("event") or ""
                if not _is_running_event(evn):
                    continue
                if evn.startswith("Stolen Base 2B"):
                    c["sb2"] += 1
                elif evn.startswith("Pickoff Caught Stealing 2B"):
                    c["pocs2"] += 1
                elif evn.startswith("Caught Stealing 2B"):
                    c["cs2"] += 1
                elif evn.startswith("Stolen Base 3B"):
                    c["sb3"] += 1
                elif evn.startswith("Caught Stealing 3B"):
                    c["cs3"] += 1
                if evn.startswith(("Wild Pitch", "Passed Ball", "Balk")):
                    wild = True
                if mv.get("isOut"):
                    pre_outs += 1
                rid = (det.get("runner") or {}).get("id")
                st = mv.get("originBase") or mv.get("start")
                end_ = mv.get("end")
                if st in at_contact and at_contact.get(st) == rid:
                    at_contact[st] = None
                if end_ in ("1B", "2B", "3B"):
                    at_contact[end_] = rid
            if wild:
                c["wild_play"] += 1
            # A runner who MOVED on the batted ball tells us where he stood when
            # it was hit; this corrects any drift the block above left behind.
            for rn in runners:
                det, mv = rn.get("details") or {}, rn.get("movement") or {}
                if _is_running_event(det.get("event") or ""):
                    continue
                st = mv.get("originBase") or mv.get("start")
                if st in ("1B", "2B", "3B"):
                    at_contact[st] = (det.get("runner") or {}).get("id")
            outs_bb = max(outs_after - pre_outs, 0)

            traj = None
            for e in reversed(p.get("playEvents") or []):
                hd = e.get("hitData")
                if hd and hd.get("trajectory"):
                    traj = hd["trajectory"]
                    break

            end: Dict[int, str] = {}
            for rn in runners:
                det, mv = rn.get("details") or {}, rn.get("movement") or {}
                if _is_running_event(det.get("event") or ""):
                    continue
                rid = (det.get("runner") or {}).get("id")
                if rid is None:
                    continue
                end[rid] = "out" if mv.get("isOut") else (mv.get("end") or "")

            on1, on2, on3 = at_contact["1B"], at_contact["2B"], at_contact["3B"]
            # Bunts are counted under their own prefix. They belong in the rates —
            # the engine's GB_OUT rate includes them, and dropping them would lose
            # the advancement a sacrifice buys — but they are a different intent
            # from a swing, so the split is kept on disk rather than assumed away.
            pre = ("b" if traj in _TRAJ_BUNT_GB + _TRAJ_BUNT_AIR else "")
            live = ev not in _HIT_EVENTS and outs_bb >= 1 and pre_outs < 2
            if live and traj in _TRAJ_GB + _TRAJ_BUNT_GB:
                c[pre + "gb_out"] += 1
                if on1 is not None:
                    c[pre + "gidp_den"] += 1
                    if outs_bb >= 2:
                        c[pre + "gidp_num"] += 1
                if outs_bb == 1:                       # the productive-out branch
                    if on3 is not None:
                        c[pre + "gbscore_den"] += 1
                        if end.get(on3) == "score":
                            c[pre + "gbscore_num"] += 1
                    if on2 is not None and on3 is None:
                        c[pre + "gbadv_den"] += 1
                        adv = end.get(on2) in ("3B", "score")
                        c[pre + "gbadv_num"] += int(adv)
                        # Split on whether first was occupied. The engine applies
                        # one rate to both, and the two are not close: with a man
                        # on first the play goes to the batter and the runner
                        # walks to third, without one he can be the play.
                        if on1 is not None:
                            c[pre + "gbadv_f1_den"] += 1
                            c[pre + "gbadv_f1_num"] += int(adv)
            if live and traj in _TRAJ_AIR + _TRAJ_BUNT_AIR:
                c[pre + "air_out"] += 1
                if on3 is not None:
                    c[pre + "sf_den"] += 1
                    scored = end.get(on3) == "score"
                    c[pre + "sf_num"] += int(scored)
                    if traj == "fly_ball":
                        c["sf_fly_den"] += 1
                        c["sf_fly_num"] += int(scored)
            if ev == "sac_fly":
                c["sac_fly_ev"] += 1
            if ev == "field_error":
                c["roe"] += 1
                if traj in _TRAJ_GB + _TRAJ_BUNT_GB:
                    c["roe_gb"] += 1

            # --- the three advancement rates that ARE already measured, recounted
            # as outcomes. They are not read by anything; they are the check that
            # this traversal agrees with the movement-record pass that produced
            # `runner_advance.json`, and a traversal that disagreed with a known
            # answer would not be trusted for the four that have no known answer.
            if outs_bb == 0:
                if ev == "single":
                    if on1 is not None:
                        c["adv_1b_on1_den"] += 1
                        c["adv_1b_on1_num"] += int(end.get(on1) in ("3B", "score"))
                    if on2 is not None:
                        c["adv_1b_on2_den"] += 1
                        c["adv_1b_on2_num"] += int(end.get(on2) == "score")
                elif ev == "double" and on1 is not None:
                    c["adv_2b_on1_den"] += 1
                    c["adv_2b_on1_num"] += int(end.get(on1) == "score")

            # --- the count table, for FRAMING_K_SHARE
            isk = ev.startswith("strikeout")
            isbb = ev == "walk"
            b = s = 0
            seen = set()
            for e in p.get("playEvents") or []:
                if not e.get("isPitch"):
                    continue
                if b > 3 or s > 2:
                    break                              # the PA is already decided
                seen.add((b, s))
                det = e.get("details") or {}
                code = (det.get("call") or {}).get("code") or ""
                if code in ("B", "*B", "C"):           # a TAKE, the framing chance
                    pd = e.get("pitchData") or {}
                    co = pd.get("coordinates") or {}
                    px, pz = co.get("pX"), co.get("pZ")
                    top, bot = pd.get("strikeZoneTop"), pd.get("strikeZoneBottom")
                    if None not in (px, pz, top, bot):
                        # Signed distance outside the rulebook zone: positive is a
                        # ball, negative a strike, and the band around zero is
                        # where the catcher earns anything.
                        d = max(abs(px) - _ZONE_HALF_W, pz - top, bot - pz)
                        if abs(d) <= _SHADOW_FT:
                            c[f"c{b}{s}_take"] += 1
                if det.get("isBall"):
                    b += 1
                elif det.get("isStrike"):
                    if not (code in ("F", "T", "L") and s >= 2):
                        s += 1
            for (bb_, ss_) in seen:
                c[f"c{bb_}{ss_}_reach"] += 1
                if isk:
                    c[f"c{bb_}{ss_}_k"] += 1
                if isbb:
                    c[f"c{bb_}{ss_}_bb"] += 1

            prev_outs = outs_after
            mu = p.get("matchup") or {}
            state = {"1B": (mu.get("postOnFirst") or {}).get("id"),
                     "2B": (mu.get("postOnSecond") or {}).get("id"),
                     "3B": (mu.get("postOnThird") or {}).get("id")}
        _bank_re24()
        return dict(c)

    @staticmethod
    def baserunning_rates(c: Dict[str, int]) -> Dict[str, float]:
        """Counters -> the constants, with the shipped fallbacks left to the caller.

        Bunts are INCLUDED in the ground-ball population: the engine's GB_OUT rate
        counts them, so leaving them out would price a population the engine never
        simulates. The bunt-only counters stay in the file so the size of that
        choice is visible rather than argued about.
        """
        def tot(name: str) -> int:
            return int(c.get(name, 0)) + int(c.get("b" + name, 0))

        both = {k: tot(k) for k in
                ("gidp_num", "gidp_den", "gbscore_num", "gbscore_den",
                 "gbadv_num", "gbadv_den", "gbadv_f1_num", "gbadv_f1_den",
                 "sf_num", "sf_den", "gb_out", "air_out")}
        att = int(c.get("sb2", 0)) + int(c.get("cs2", 0)) + int(c.get("pocs2", 0))
        out: Dict[str, float] = {}
        for key, num, den in (("sac_fly", "sf_num", "sf_den"),
                              ("gidp", "gidp_num", "gidp_den"),
                              ("gb_scores", "gbscore_num", "gbscore_den"),
                              ("gb_advance", "gbadv_num", "gbadv_den"),
                              ("gb_advance_forced", "gbadv_f1_num",
                               "gbadv_f1_den")):
            v = _rate(both, num, den)
            if v is not None:
                out[key] = round(v, 4)
        if att >= 200:
            out["steal_success"] = round(c["sb2"] / att, 4)
        if c.get("steal_opp", 0) >= 2000:
            out["steal_attempt"] = round(att / c["steal_opp"], 4)
        if c.get("runner_on_pa", 0) >= 2000:
            out["wild_advance"] = round(c.get("wild_play", 0)
                                        / c["runner_on_pa"], 4)
        # Read by nothing — the agreement check described above.
        for key, tag in (("first_to_third", "adv_1b_on1"),
                         ("second_scores", "adv_1b_on2"),
                         ("first_scores_2b", "adv_2b_on1")):
            v = _rate(c, tag + "_num", tag + "_den")
            if v is not None:
                out[key] = round(v, 4)
        v = framing_k_share(c)
        if v is not None:
            out["framing_k_share"] = round(v, 4)
        return out

    @staticmethod
    def baserunning_report(season: Optional[int] = None, refresh: bool = False,
                           workers: int = 12) -> dict:
        """Measured against shipped, for every constant in sim_state.md 5.6c."""
        season = BASERUN_SEASON if season is None else int(season)
        counts = collect_baserunning(season, workers=workers, refresh=refresh)
        rates = BaseRunningPbp.baserunning_rates(counts)
        def _both(k: str) -> int:
            return int(counts.get(k, 0)) + int(counts.get("b" + k, 0))

        # Steal attempts are not a stored key — they are SB + CS + pickoff-CS, so
        # the sample column has to be given the number rather than a key name.
        # It printed a blank and a "0/33500" until it was.
        att = (int(counts.get("sb2", 0)) + int(counts.get("cs2", 0))
               + int(counts.get("pocs2", 0)))
        rows = [
            ("P_SAC_FLY", "sac_fly", P_SAC_FLY, _both("sf_num"), _both("sf_den")),
            ("P_GIDP", "gidp", P_GIDP, _both("gidp_num"), _both("gidp_den")),
            ("P_GB_ADVANCE", "gb_advance", P_GB_ADVANCE,
             _both("gbadv_num"), _both("gbadv_den")),
            ("P_GB_SCORES", "gb_scores", P_GB_SCORES,
             _both("gbscore_num"), _both("gbscore_den")),
            ("P_STEAL_SUCCESS", "steal_success", P_STEAL_SUCCESS,
             int(counts.get("sb2", 0)), att),
            ("P_STEAL_ATTEMPT", "steal_attempt", P_STEAL_ATTEMPT, att,
             int(counts.get("steal_opp", 0))),
            ("P_WILD_ADVANCE", "wild_advance", P_WILD_ADVANCE,
             int(counts.get("wild_play", 0)), int(counts.get("runner_on_pa", 0))),
            ("FRAMING_K_SHARE", "framing_k_share", FRAMING_K_SHARE,
             sum(v for k, v in counts.items() if k.endswith("_take")), 0),
            ("P_FIRST_TO_THIRD_ON_1B", "first_to_third", P_FIRST_TO_THIRD_ON_1B,
             int(counts.get("adv_1b_on1_num", 0)),
             int(counts.get("adv_1b_on1_den", 0))),
            ("P_SECOND_SCORES_ON_1B", "second_scores", P_SECOND_SCORES_ON_1B,
             int(counts.get("adv_1b_on2_num", 0)),
             int(counts.get("adv_1b_on2_den", 0))),
            ("P_FIRST_SCORES_ON_2B", "first_scores_2b", P_FIRST_SCORES_ON_2B,
             int(counts.get("adv_2b_on1_num", 0)),
             int(counts.get("adv_2b_on1_den", 0))),
        ]
        print(f"\nbase-running, measured over {counts.get('games', 0)} games / "
              f"{counts.get('pa', 0)} PA — season {season}\n")
        print(f"  {'constant':<24s} {'shipped':>8s} {'measured':>9s} "
              f"{'delta':>8s}   sample")
        for name, key, shipped, n, d in rows:
            got = rates.get(key)
            if got is None:
                print(f"  {name:<24s} {shipped:8.4f} {'—':>9s}")
                continue
            smp = f"{n}/{d}" if d else f"{n} takes"
            print(f"  {name:<24s} {shipped:8.4f} {got:9.4f} {got - shipped:+8.4f}"
                  f"   {smp}")
        fwd = rates.get("gb_advance_forced")
        if fwd is not None:
            un_d = _both("gbadv_den") - _both("gbadv_f1_den")
            un_n = _both("gbadv_num") - _both("gbadv_f1_num")
            print(f"\n  P_GB_ADVANCE is two populations: {fwd:.3f} with a man "
                  f"also on first (the play goes to the batter and he walks to "
                  f"third), {un_n / un_d if un_d else 0.0:.3f} without. The "
                  f"engine applies one rate to the mix.")
        print(f"\n  cross-check: {counts.get('sac_fly_ev', 0)} plays the feed "
              f"calls a sacrifice fly against {counts.get('sf_num', 0) + counts.get('bsf_num', 0)} "
              f"runners measured home from third on an air out.")
        return {"counts": counts, "rates": rates}

    @staticmethod
    def real_re24(season: Optional[int] = None, save_dir: Path = SAVE_DIR
                  ) -> Dict[Tuple[int, int], Tuple[float, int]]:
        """{(bases bitmask, outs): (runs to end of inning, opportunities)}."""
        season = BASERUN_SEASON if season is None else int(season)
        counts = collect_baserunning(season, save_dir=save_dir)
        out: Dict[Tuple[int, int], Tuple[float, int]] = {}
        for base in range(8):
            for outs in range(3):
                n = int(counts.get(f"re_{base}_{outs}_n", 0))
                if n:
                    out[(base, outs)] = (
                        float(counts.get(f"re_{base}_{outs}_runs", 0)), n)
        return out

    @staticmethod
    def re24_report(n: int = 6000, seed: int = 5,
                    season: Optional[int] = None) -> dict:
        """Simulated run expectancy against the measured table, cell by cell.

        League-average clones on both sides, so nothing here is about a roster —
        it is the base-out transition model on its own, which is what the
        advancement constants are.
        """
        season = BASERUN_SEASON if season is None else int(season)
        side = league_side

        home, away = side("H"), side("A")
        logs: List[List[dict]] = []
        for i in range(n):
            log: List[dict] = []
            simulate_game(home, away, random.Random(seed * 1_000_003 + i), log=log)
            logs.append(log)
        sim, real = sim_re24(logs), BaseRunningPbp.real_re24(season)

        print(f"\nrun expectancy — {n} simulated games against {season} "
              f"play-by-play\n")
        print(f"  {'state':>7s} {'sim':>7s} {'real':>7s} {'diff':>7s} "
              f"{'sim n':>8s} {'real n':>8s}")
        w_abs = w_n = 0.0
        rows = []
        for outs in range(3):
            for base in range(8):
                s_ = sim.get((base, outs))
                r_ = real.get((base, outs))
                if not s_ or not r_:
                    continue
                sv, rv = s_[0] / s_[1], r_[0] / r_[1]
                rows.append((base, outs, sv, rv, s_[1], r_[1]))
                w_abs += abs(sv - rv) * r_[1]
                w_n += r_[1]
                print(f"  {_BASE_LABEL[base]}/{outs} {sv:7.3f} {rv:7.3f} "
                      f"{sv - rv:+7.3f} {s_[1]:8d} {r_[1]:8d}")
        print(f"\n  opportunity-weighted mean |error|  {w_abs / w_n:.4f} runs"
              if w_n else "")
        return {"sim": sim, "real": real, "rows": rows,
                "mean_abs_error": (w_abs / w_n) if w_n else None}


def collect_baserunning(season: Optional[int] = None, workers: int = 12,
                        refresh: bool = False, n_games: int = 0,
                        save_dir: Path = SAVE_DIR) -> Dict[str, int]:
    """Season-wide base-running and count counters, cached to disk.

    Keyed on SEASON from the start, because every cache in this file that was
    not has cost a silent wrong-season run at least once.
    """
    season = BASERUN_SEASON if season is None else int(season)
    path = Path(save_dir) / BASERUN_CACHE_FMT.format(season=season)
    if path.exists() and not refresh:
        try:
            with open(path) as fh:
                got = json.load(fh)
            if got.get("counts"):
                return got["counts"]
        except (OSError, ValueError):
            pass
    pks = RelieverUsage.season_game_pks(season, save_dir)
    pks = pks[-n_games:] if n_games else pks
    print(f"[baserunning] {season}: play-by-play for {len(pks)} games...")
    tot: collections.Counter = collections.Counter()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(BaseRunningPbp.fetch_pbp_baserunning, pks):
            tot.update(got)
            done += 1
            if done % 200 == 0:
                print(f"[baserunning]   {done}/{len(pks)}", flush=True)
    counts = {k: int(v) for k, v in sorted(tot.items())}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"season": season, "counts": counts,
                   "rates": BaseRunningPbp.baserunning_rates(counts)}, fh, indent=1)
    print(f"[baserunning] {season}: {counts.get('pa', 0)} PA from "
          f"{counts.get('games', 0)} games -> {path.name}")
    return counts


def _rate(c: Dict[str, int], num: str, den: str,
          floor: int = 200) -> Optional[float]:
    """num/den, or None when the denominator is too thin to ship."""
    d = int(c.get(den, 0))
    return (int(c.get(num, 0)) / d) if d >= floor else None


def framing_k_share(c: Dict[str, int]) -> Optional[float]:
    """How an extra called strike splits between making Ks and killing walks.

    **`FRAMING_K_SHARE` was 0.5 by assertion, and sim_state.md pointed at the
    wrong data to settle it** — Savant's `rv_11`..`rv_19` are run value by ZONE,
    not by COUNT (which is why 15 is missing from the sequence). What answers it
    is the count table: a borderline take continues the PA from (b, s+1) or
    (b+1, s), so

        dK  = P(K | b, s+1) - P(K | b+1, s)
        dBB = P(BB | b, s+1) - P(BB | b+1, s)

    weighted by where framing chances occur. The constant is a share of a
    MULTIPLIER, so it splits the two RELATIVE moves — and walks being a quarter
    as common as strikeouts is the whole reason 0.5 is wrong. Sanity mark: ~0.13
    runs per extra strike against a published ~0.125. sim_state.md A.10.
    """
    def p_end(b: int, s: int, kind: str) -> float:
        """P(this PA ends in `kind`) from count (b, s). `kind` is "k" or "bb".

        One function for both, because they differ only in which terminal count
        is a certainty: three strikes ends it as a K, four balls as a BB. The
        loop below advances only ONE of the two, so the guards never both fire.
        """
        if s >= 3:
            return 1.0 if kind == "k" else 0.0
        if b >= 4:
            return 0.0 if kind == "k" else 1.0
        n = c.get(f"c{b}{s}_reach", 0)
        return c.get(f"c{b}{s}_{kind}", 0) / n if n else 0.0

    dk = dbb = w = 0.0
    for b in range(4):
        for s in range(3):
            wt = float(c.get(f"c{b}{s}_take", 0))
            if not wt:
                continue
            dk += wt * (p_end(b, s + 1, "k") - p_end(b + 1, s, "k"))
            dbb += wt * (p_end(b, s + 1, "bb") - p_end(b + 1, s, "bb"))
            w += wt
    if w < 5000 or dk <= 0 or dbb >= 0:
        return None
    dk, dbb = dk / w, dbb / w
    rk = dk / LEAGUE_BASELINE[K]
    rb = -dbb / LEAGUE_BASELINE[BB]
    return rk / (rk + rb)


# --- RUN EXPECTANCY, as the instrument for a changed advancement model -----
# §2 says the free-advancement constants are re-fit against our own measured
# RE24; that instrument existed only as a table on disk and a throwaway script.
#
# **The table it used to be scored against is biased, and only in one cell.**
# `walk_half_innings` differences a running score seeded from the first play of
# the half, so the whole loss lands on bases-empty-nobody-out: 0.4665 against a
# measured 0.4977, almost exactly a leadoff home run.  `collect_baserunning`
# counts runs off the SCORING MOVEMENTS instead, which cannot drift.

_BASE_LABEL = ("___", "1__", "_2_", "12_", "__3", "1_3", "_23", "123")


def sim_re24(logs: Sequence[Sequence[dict]]
             ) -> Dict[Tuple[int, int], Tuple[float, int]]:
    """The same table off `simulate_game(log=[])`, banked the same way.

    Half-innings that did not end in three outs are dropped, exactly as the
    real pass drops them — a walk-off or an unbatted home half would otherwise
    drag every state's expectancy down.
    """
    acc: Dict[Tuple[int, int], List[float]] = {}
    for log in logs:
        halves: Dict[tuple, List[dict]] = {}
        for ev in log:
            halves.setdefault((ev["inning"], ev["half"]), []).append(ev)
        for evs in halves.values():
            if evs[-1]["outs_after"] != 3 and not evs[-1].get("half_ended_rg"):
                continue
            tail = 0
            for ev in reversed(evs):
                # A run scored by the running game belongs to the state it was
                # scored FROM, which is the previous plate appearance's, so it
                # joins the tail only after this row has been banked.
                tail += ev.get("runs_after", 0) + ev["runs"]
                cell = acc.setdefault(
                    (ev["bases_before"], ev["outs_before"]), [0.0, 0])
                cell[0] += tail
                cell[1] += 1
                tail += ev.get("runs_before", 0)
    return {k: (v[0], int(v[1])) for k, v in acc.items()}


# ===========================================================================
# 16. EMPIRICAL DEPLOYMENT — per pitcher, from what he actually did
# ===========================================================================
# Sections 13/15 scored arms with a hand-tuned formula. Wrong shape of solution:
# **we have every entry he made.** Mason Miller's real distribution is 8th 14% /
# 9th 84% — he has never entered a 6th or a 7th — and no exponential penalty
# reproduces that as cleanly as reading it off. Three distributions, each shrunk
# toward the next-coarsest level by its own sample size, plus a home/away tie
# factor (closers enter 9th-inning ties 1.52x more at home). Everything is per
# pitcher and therefore per TEAM by construction.

INNING_BUCKETS = tuple(range(1, 11))          # 10 = "10th or later"
MARGIN_BUCKETS = ("lead4", "lead13", "tied", "trail13", "trail4")
# Sample size at which a pitcher's own histogram is half-believed, set to the
# median entries-per-pitcher in the data rather than chosen.
# Keyed on SEASON, like every other cache in this file. A bare global meant a
# 2025 backtest was deployed off 2026 entries.
_DEPLOY: Dict[int, dict] = {}

# Which season's deployment and traits the simulation uses. `backtest` sets it
# to the season being replayed; it travels to the pool via `_slate_overrides`.
DEPLOY_SEASON = 2026


class Deployment:
    """Empirical deployment, per pitcher, from what he actually did."""

    @staticmethod
    def margin_bucket(d: int) -> str:
        if d >= 4:
            return "lead4"
        if d >= 1:
            return "lead13"
        if d == 0:
            return "tied"
        if d >= -3:
            return "trail13"
        return "trail4"

    @staticmethod
    def build_deployment(season: Optional[int] = None) -> dict:
        """Per-pitcher entry distributions, shrunk toward role. Cached per season."""
        season = DEPLOY_SEASON if season is None else season
        if season in _DEPLOY:
            return _DEPLOY[season]
        entries = RelieverUsage.collect_reliever_entries(season=season)
        traits = RelieverTraits.load_reliever_traits(season)

        by_p: Dict[int, List[dict]] = {}
        for x in entries:
            by_p.setdefault(x["pitcher"], []).append(x)
        depths = sorted(len(v) for v in by_p.values())
        stabilize = float(depths[len(depths) // 2]) if depths else 10.0
        # Mean entry inning per arm — the ITP-free route to a role (see `_role_of`).
        pbp_inn = {pid: statistics.mean(int(r["inning"] or 0) for r in rows)
                   for pid, rows in by_p.items() if rows}

        def hist(rows, key, buckets):
            c = {b: 0.0 for b in buckets}
            for r in rows:
                c[key(r)] = c.get(key(r), 0.0) + 1.0
            n = sum(c.values())
            return {b: (v / n if n else 1.0 / len(buckets)) for b, v in c.items()}

        inn_key = lambda r: min(int(r["inning"] or 1), 10)
        mar_key = lambda r: Deployment.margin_bucket(int(r["margin"] or 0))

        role_rows: Dict[str, List[dict]] = {}
        for pid, rows in by_p.items():
            role_rows.setdefault(_role_of(pid, traits, pbp_inn.get(pid)),
                                 []).extend(rows)
        role_inn = {r: hist(v, inn_key, INNING_BUCKETS) for r, v in role_rows.items()}
        role_mar = {r: hist(v, mar_key, MARGIN_BUCKETS) for r, v in role_rows.items()}
        # P(margin | inning, role) — the JOINT, which the product of marginals
        # cannot express. A closer's 9th-inning probability is so dominant that
        # multiplying it by a low blowout probability still beat every other
        # arm's 7th-shaped distribution, so he took 30% of his entries in
        # blowouts against a real 14%.
        role_joint: Dict[tuple, Dict[str, float]] = {}
        for r, rows_ in role_rows.items():
            by_inn: Dict[int, list] = {}
            for x in rows_:
                by_inn.setdefault(inn_key(x), []).append(x)
            for i, rr in by_inn.items():
                if len(rr) >= 20:
                    role_joint[(r, i)] = hist(rr, mar_key, MARGIN_BUCKETS)

        # Home/road split on TIE games — the "save him for the 10th on the road"
        # behaviour, measured rather than assumed.
        # CLOSERS only — the behaviour is "save him for the 10th on the road", and
        # averaging every arm's tie-game entries washes it out (1.08 across all
        # pitchers against 1.52 for closers, which is the real effect).
        tie = [x for x in entries if int(x["inning"] or 0) == 9
               and int(x["margin"] or 0) == 0
               and _role_of(x["pitcher"], traits,
                            pbp_inn.get(x["pitcher"])) == "closer"]
        h = sum(1 for x in tie if x.get("p_home"))
        a = len(tie) - h
        tie_home_factor = (h / a) if a else 1.0

        out: Dict[int, dict] = {}
        for pid, rows in by_p.items():
            role = _role_of(pid, traits, pbp_inn.get(pid))
            n = len(rows)
            w = n / (n + stabilize)
            mine_i = hist(rows, inn_key, INNING_BUCKETS)
            mine_m = hist(rows, mar_key, MARGIN_BUCKETS)
            ri = role_inn.get(role) or mine_i
            rm = role_mar.get(role) or mine_m
            out[pid] = {
                "role": role, "n": n,
                "inning": {b: w * mine_i[b] + (1 - w) * ri.get(b, 0.0)
                           for b in INNING_BUCKETS},
                "margin": {b: w * mine_m[b] + (1 - w) * rm.get(b, 0.0)
                           for b in MARGIN_BUCKETS},
            }
        _DEPLOY[season] = {"pitchers": out, "role_inning": role_inn,
                           "role_margin": role_mar, "role_joint": role_joint,
                           "tie_home_factor": tie_home_factor,
                           "stabilize": stabilize}
        return _DEPLOY[season]

    @staticmethod
    def render_pbp(log: Sequence[dict], home: str = "HOME",
                   away: str = "AWAY") -> str:
        """A simulated game as readable play-by-play.

        Exists to be READ. Aggregate validation says the appearance rates are
        right; only walking an actual game shows a closer entering the sixth, a
        long man leaving after four batters of a blowout, or a pitching change
        that no manager would make.
        """
        _EV = {"K": "strikes out", "BB": "walks", "HBP": "hit by pitch",
               "GB_OUT": "grounds out", "AIR_OUT": "flies out",
               "1B": "singles", "2B": "doubles", "3B": "triples",
               "HR": "HOMERS"}
        out: List[str] = []
        half_now = None
        for e in log:
            key = (e["inning"], e["half"])
            if key != half_now:
                half_now = key
                side = away if e["half"] == "away" else home
                out.append(f"\n--- {'Top' if e['half'] == 'away' else 'Bot'} "
                           f"{e['inning']}  ({side} batting)   "
                           f"{away} {e['score'][0]} - {e['score'][1]} {home}")
            if e["new_pitcher"]:
                out.append(f"    >> PITCHING CHANGE: {e['pitcher']} "
                           f"({e['throws'] or '?'})")
            on = f" [{e['on_before']} on]" if e["on_before"] else ""
            rbi = f"  ({e['runs']} run{'s' if e['runs'] != 1 else ''})" if e["runs"] else ""
            out.append(f"    {e['outs_before']} out{on}  "
                       f"{e['batter']} ({e['bats'] or '?'}) "
                       f"{_EV.get(e['outcome'], e['outcome'])}{rbi}")
        return "\n".join(out)


def _role_of(pid: int, traits: dict,
             pbp_avg_inning: Optional[float] = None) -> str:
    """Bullpen role, from insidethepen when available and from the PLAY-BY-PLAY
    when it is not.

    **ITP only serves the CURRENT season**, so every past-season run classified
    every arm "other" — 2025 produced 713 relievers and zero closers, silently
    disabling the whole role layer for any backtest before this year. The
    play-by-play carries the same quantity (ITP's "average inning when called" IS
    the mean entry inning). Validated on 2026 where both exist: **corr +0.90**,
    MAE 0.34 innings, reproducing ITP's own label 83% of the time. Rounded,
    because ITP reports on an integer-ish scale and the thresholds were set to it.
    """
    t = traits.get(pid) or {}
    ai = t.get("itp_avg_inning")
    if ai is None and pbp_avg_inning is not None:
        ai = round(pbp_avg_inning)
    ai = ai or 0
    if t.get("itp_role") == "Closer" or ai >= 9:
        return "closer"
    if ai >= 8:
        return "setup"
    if ai and ai <= 6:
        return "middle"
    return "other"


def deployment_score(pid: Optional[int], inning: int, margin: int,
                     is_home: bool) -> float:
    """How likely THIS pitcher is to be the one entering in THIS state."""
    dep = Deployment.build_deployment()
    rec = dep["pitchers"].get(pid or -1)
    if rec is None:
        ri = dep["role_inning"].get("other") or {}
        rm = dep["role_margin"].get("other") or {}
        p = ri.get(min(inning, 10), 0.1) * rm.get(Deployment.margin_bucket(margin), 0.2)
        return max(p, 1e-6)
    inn = min(inning, 10)
    mb = Deployment.margin_bucket(margin)
    # The role's JOINT P(margin | inning), RAKED by this arm's own deviation
    # from his role's margin marginal. **The joint alone cannot separate two
    # arms in the same role, and that was the whole defect** (§4i): it is a
    # four-way label, and in the bucket that matters the roles agree to within
    # 0.07, so a term ~equal for every arm cancels in a ratio — the engine's
    # closer was about as likely to enter down six in the 8th as its mop-up man,
    # and 14 of 30 pens conceded BACKWARDS. `rec["margin"]` measures it per arm,
    # but `role_joint` covered 82% of cells and overrode it.
    joint = dep["role_joint"].get((rec["role"], inn))
    if joint:
        role_mar = (dep["role_margin"].get(rec["role"]) or {}).get(mb, 0.0)
        ratio = (rec["margin"].get(mb, 0.0) / role_mar) if role_mar > 0 else 1.0
        p_margin = joint.get(mb, 0.0) * ratio
    else:
        p_margin = rec["margin"].get(mb, 0.0)
    p = rec["inning"].get(inn, 0.0) * p_margin
    if inning >= 9 and margin == 0 and rec["role"] == "closer":
        p *= dep["tie_home_factor"] if is_home else 1.0
    return max(p, 1e-9)



# ===========================================================================
# 17. SIM vs REALITY — validate against baseball, not against the market
# ===========================================================================
# A market line is a proxy with its own noise and its own vig; agreement with it
# is neither necessary nor sufficient for the simulation being right.
#
# **The half-inning reference marks were previously wrong** — the sd was 4.1%
# high, which made sqrt(9) x half-inning sd land on the real game sd and founded
# the conclusion that REAL INNINGS ARE INDEPENDENT. They are not: **11.1% of
# team-game run variance is between-inning covariance** over innings 1-8.
#
# **It is still not momentum, and no rally term is warranted** — the covariance
# is FLAT in lag with lag 1 the LOWEST, the signature of a SHARED PER-GAME
# FACTOR, and the opposing STARTER's identity carries ~47% of it. Which is why
# `validate_vs_reality` cannot show it: clones make matchup heterogeneity zero
# BY CONSTRUCTION. Use `validate_slate_vs_reality()`. sim_state.md A.17.

REAL_MARKS = {
    "team_game_runs_mean": 4.479, "team_game_runs_sd": 3.225,
    "game_total_mean": 8.958, "game_total_median": 8.0, "game_total_sd": 4.536,
    "home_win_rate": 0.5269,
    "half_inning_runs_mean": 0.5036, "half_inning_runs_sd": 1.0356,
    "half_inning_scoreless": 0.7260,
    "innings_batted_per_team_game": 8.894,
    "extra_inning_fraction": 0.0860,
    # innings 1-8 variance decomposition, per team-game
    "inn18_var": 9.660, "inn18_indep": 8.584, "inn18_cov": 1.076,
    "inn18_pair_cov": 0.01914,
}

REAL_MARKS_SOURCE = (
    "StatsAPI /schedule?sportId=1&gameType=R&hydrate=linescore, "
    "2026-03-01..2026-08-14, games with >=8 innings of linescore."
)

# The dict above is a FALLBACK, not the authority. Run-scoring drifts — the
# league moved ~0.6 runs a game across 2019-2023 alone — so a frozen mark
# silently becomes a wrong target and the engine gets "validated" against a
# season no longer being played. `real_marks()` is what the harness should call.
MARKS_CACHE = SAVE_DIR / "real_marks_{season}.json"


class Validation:
    """Sim vs reality — validate against baseball, not against the market."""

    @staticmethod
    def measure_real_marks(season: Optional[int] = None, start: Optional[str] = None,
                           end: Optional[str] = None, refresh: bool = False,
                           timeout: float = 90.0) -> dict:
        """Recompute the reference marks from StatsAPI linescores, and cache them.

        Every quantity `validate_vs_reality` scores against, measured off the same
        pull so the denominators cannot drift apart — which is exactly how the
        half-inning marks went wrong before: 4.477 runs / 0.520 per half-inning
        implies 8.61 innings batted per team-game, and a team bats 8.894.
        """
        season = CURRENT_SEASON if season is None else int(season)
        path = Path(str(MARKS_CACHE).format(season=season))
        if path.exists() and not refresh:
            try:
                with open(path) as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass

        start = start or f"{season}-03-01"
        end = end or f"{season}-11-01"
        url = StatsApi.schedule_url(start=start, end=end,
                                    hydrate="linescore", game_type="R")
        data = requests.get(url, timeout=timeout).json()

        vectors: List[List[int]] = []          # per team-game inning runs
        totals: List[int] = []
        home_wins = decided = extras = 0
        for day in data.get("dates", []):
            for g in day.get("games", []):
                if (g.get("status") or {}).get("abstractGameState") != "Final":
                    continue
                inns = (g.get("linescore") or {}).get("innings") or []
                a = [i["away"]["runs"] for i in inns
                     if (i.get("away") or {}).get("runs") is not None]
                h = [i["home"]["runs"] for i in inns
                     if (i.get("home") or {}).get("runs") is not None]
                if len(a) < 8 or len(h) < 8:
                    continue
                vectors += [a, h]
                totals.append(sum(a) + sum(h))
                if sum(h) != sum(a):
                    decided += 1
                    home_wins += sum(h) > sum(a)
                extras += max(len(a), len(h)) > 9

        if not totals:
            return dict(REAL_MARKS)
        tg = [sum(v) for v in vectors]
        halves = [x for v in vectors for x in v]
        v8 = [v[:8] for v in vectors if len(v) >= 8]
        var8 = statistics.pstdev([sum(v) for v in v8]) ** 2
        indep8 = sum(statistics.pstdev(c) ** 2 for c in zip(*v8))

        marks = {
            "team_game_runs_mean": statistics.mean(tg),
            "team_game_runs_sd": statistics.pstdev(tg),
            "game_total_mean": statistics.mean(totals),
            "game_total_median": statistics.median(totals),
            "game_total_sd": statistics.pstdev(totals),
            "home_win_rate": home_wins / decided if decided else 0.5,
            "half_inning_runs_mean": statistics.mean(halves),
            "half_inning_runs_sd": statistics.pstdev(halves),
            "half_inning_scoreless": sum(1 for x in halves if x == 0) / len(halves),
            "innings_batted_per_team_game": statistics.mean(len(v) for v in vectors),
            "extra_inning_fraction": extras / len(totals),
            "inn18_var": var8,
            "inn18_indep": indep8,
            "inn18_cov": var8 - indep8,
            "inn18_pair_cov": (var8 - indep8) / 56.0,
            # Per-inning mean over 1-8. NOT flat, and the shape is the thing:
            # inning 1 is the highest-scoring inning in the game (section 5.4).
            "inn18_mean_by_inning": [statistics.mean(c) for c in zip(*v8)],
            "_games": len(totals),
            "_season": season,
            "_range": f"{start}..{end}",
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                json.dump(marks, fh, indent=1)
        except OSError:
            pass
        return marks

    @staticmethod
    def real_marks(season: Optional[int] = None, measured: bool = True) -> dict:
        """Reference marks: measured when they can be, frozen when they cannot."""
        season = CURRENT_SEASON if season is None else int(season)
        if measured:
            try:
                return Validation.measure_real_marks(season)
            except Exception:
                pass
        return dict(REAL_MARKS)

    @staticmethod
    def inning_vectors(results_log: Sequence[Sequence[dict]]
                       ) -> List[List[int]]:
        """Per team-game runs-by-inning vectors, off `simulate_game(log=...)`."""
        out: List[List[int]] = []
        for log in results_log:
            acc: Dict[tuple, int] = {}
            for ev in log:
                key = (ev["inning"], ev["half"])
                acc[key] = acc.get(key, 0) + ev["runs"]
            for half in ("home", "away"):
                v = [acc[k] for k in acc if k[1] == half]
                if v:
                    out.append(v)
        return out

    @staticmethod
    def dispersion_report(vectors: Sequence[Sequence[int]], k: int = 8) -> dict:
        """Split team-game run variance into independent and covariance parts.

        Over innings 1..k ONLY, and k must stay at 8. Innings 9+ are selected on
        the score — the home half of the 9th is not batted when the home side
        leads, and extras happen only in tied games and are then inflated by the
        auto-runner — so including them mixes three negative-covariance selection
        effects into the number and hides the thing being measured.
        """
        v = [list(x[:k]) for x in vectors if len(x) >= k]
        if not v:
            return {}
        tot = [sum(x) for x in v]
        var = statistics.pstdev(tot) ** 2
        indep = sum(statistics.pstdev(c) ** 2 for c in zip(*v))
        n_pairs = k * (k - 1) // 2
        lags = {}
        for lag in range(1, k):
            pts = [(x[i], x[i + lag]) for x in v for i in range(k - lag)]
            mx = statistics.mean(a for a, _ in pts)
            my = statistics.mean(b for _, b in pts)
            lags[lag] = sum((a - mx) * (b - my) for a, b in pts) / len(pts)
        return {
            "team_games": len(v),
            "mean": statistics.mean(tot),
            "sd": statistics.pstdev(tot),
            "var": var,
            "indep": indep,
            # Per-inning MEAN, which is a different defect from the covariance and
            # needs its own scoreboard. Real baseball's profile is not flat and its
            # shape is specific: inning 1 is the HIGHEST-scoring inning of the game
            # (0.531 against a 1-8 average of 0.500), because the top of the order
            # bats and the starter has not settled. Section 5.4 of sim_state.md.
            "by_inning": [statistics.mean(c) for c in zip(*v)],
            "cov": var - indep,
            "cov_share": (var - indep) / var if var else 0.0,
            "pair_cov": (var - indep) / (2 * n_pairs) if n_pairs else 0.0,
            "by_lag": lags,
            # Where the covariance sits. Real baseball puts MOST of it in the
            # bullpen innings (+0.0316) and least inside the starter's own window
            # (+0.0135) — so it is not a starter's nightly form, and a per-starter
            # noise draw would reproduce the wrong shape.
            "window": {
                "starter_1_5": Validation._window_cov(v, lambda i, j: j <= 4),
                "bullpen_6_8": Validation._window_cov(v, lambda i, j: i >= 5),
                "spanning": Validation._window_cov(v, lambda i, j: i <= 4 and j >= 5),
            },
        }

    @staticmethod
    def _window_cov(v: Sequence[Sequence[int]], sel) -> Optional[float]:
        k = len(v[0])
        pts = [(x[i], x[j]) for x in v
               for i in range(k) for j in range(i + 1, k) if sel(i, j)]
        if not pts:
            return None
        mx = statistics.mean(a for a, _ in pts)
        my = statistics.mean(b for _, b in pts)
        return sum((a - mx) * (b - my) for a, b in pts) / len(pts)

    @staticmethod
    def _clone_dispersion(n: int, seed: int) -> dict:
        """`dispersion_report` over `n` games of LEAGUE-AVERAGE clones.

        Clones on both sides on purpose: with no matchup heterogeneity the
        covariance term isolates what the game-level form draw contributes.
        `_form_probe` and `validate_dispersion` ran the identical loop.
        """
        rng = random.Random(seed)
        home, away = league_side("H"), league_side("A")
        logs = []
        for _ in range(n):
            log: List[dict] = []
            simulate_game(home, away, rng, log=log)
            logs.append(log)
        return Validation.dispersion_report(Validation.inning_vectors(logs))

    @staticmethod
    def _form_probe(sd: float, shift: float, n: int, seed: int) -> dict:
        """Run league-average clones at a given form draw and report the effect."""
        global GAME_FORM_SD, GAME_FORM_MEAN_SHIFT
        old = (GAME_FORM_SD, GAME_FORM_MEAN_SHIFT)
        GAME_FORM_SD, GAME_FORM_MEAN_SHIFT = sd, shift
        try:
            rep = Validation._clone_dispersion(n, seed)
        finally:
            GAME_FORM_SD, GAME_FORM_MEAN_SHIFT = old
        return rep

    @staticmethod
    def calibrate_form(target_extra_cov: float = 0.0159, n: int = 8000,
                       seed: int = 23, verbose: bool = True) -> dict:
        """Fit `GAME_FORM_SD` and `GAME_FORM_MEAN_SHIFT` against measured data.

        Two quantities, fitted in order because they are nearly independent: the
        sd is solved so the added per-inning covariance matches `target_extra_cov`
        (covariance is quadratic in the tilt, so one probe fixes the scale), then
        the shift, because runs are CONVEX in offensive rate so a symmetric tilt
        RAISES the mean. Skipping the second ships a variance fix that quietly
        moves every total. Returns the fitted values; it does NOT write them.
        """
        base = Validation._form_probe(0.0, 0.0, n, seed)
        # **Fit a GRID, do not iterate.** The covariance estimate carries ~20%
        # sampling error at this n, so a secant step chases noise — successive
        # iterations bounced 0.0111 / 0.0115 / 0.0197 for monotonically
        # increasing sd. Covariance is very nearly quadratic in the tilt, so
        # probe a spread, fit `cov = k * sd^2` through the origin, solve once.
        grid = [0.06, 0.09, 0.12, 0.15, 0.18]
        pts = []
        for g in grid:
            cov = Validation._form_probe(g, 0.0, n, seed)["pair_cov"] - base["pair_cov"]
            pts.append((g, cov))
            if verbose:
                print(f"    probe sd {g:.3f} -> extra cov {cov:+.5f}")
        num = sum((g ** 2) * c for g, c in pts)
        den = sum((g ** 2) ** 2 for g, _ in pts)
        k = num / den if den else 0.0
        if k <= 0:
            raise RuntimeError("mlb_sim: form probe produced no covariance")
        sd = math.sqrt(target_extra_cov / k)
        if verbose:
            print(f"    fitted k = {k:.4f}  ->  sd = {sd:.5f}")
        at_sd = Validation._form_probe(sd, 0.0, n, seed)
        # mean shift per team-game, converted back into tilt units by the same
        # local slope the probe measured
        d_mean = at_sd["mean"] - base["mean"]
        shift = 0.0
        if d_mean > 0:
            lo = Validation._form_probe(sd, 0.004, n, seed)
            per_unit = (lo["mean"] - at_sd["mean"]) / 0.004
            if per_unit < 0:
                shift = max(0.0, d_mean / -per_unit)
        final = Validation._form_probe(sd, shift, n, seed)

        out = {"GAME_FORM_SD": sd, "GAME_FORM_MEAN_SHIFT": shift,
               "base": base, "final": final,
               "target_extra_cov": target_extra_cov}
        if verbose:
            marks = Validation.real_marks()
            print(f"form calibration ({n} games/probe, innings 1-8)")
            print(f"  target extra pair-cov      {target_extra_cov:+.5f}")
            print(f"  GAME_FORM_SD               {sd:.5f}")
            print(f"  GAME_FORM_MEAN_SHIFT       {shift:.5f}")
            print(f"\n  {'':12s} {'before':>10s} {'after':>10s} {'real':>10s}")
            print(f"  {'mean':12s} {base['mean']:10.4f} {final['mean']:10.4f}"
                  f" {marks.get('team_game_runs_mean', 0)*8/8.894:10.4f}")
            print(f"  {'sd':12s} {base['sd']:10.4f} {final['sd']:10.4f}"
                  f" {marks.get('inn18_var', 0)**0.5:10.4f}")
            print(f"  {'pair_cov':12s} {base['pair_cov']:10.5f}"
                  f" {final['pair_cov']:10.5f}"
                  f" {marks.get('inn18_pair_cov', 0):10.5f}")
            print(f"  {'cov term':12s} {base['cov']:10.4f} {final['cov']:10.4f}"
                  f" {marks.get('inn18_cov', 0):10.4f}")
        return out

    @staticmethod
    def validate_dispersion(n: int = 6000, seed: int = 11,
                            season: Optional[int] = None) -> dict:
        """Score the sim's run DISPERSION, not just its level, against reality.

        The level marks pass while the shape does not, and the shape is what
        prices totals and run lines. Note this runs league-average CLONES, which
        have no matchup heterogeneity at all: expect the covariance term near
        zero here, and read it against `inn18_cov` as the size of what real
        matchups plus an explicit game-level term have to supply.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rep = Validation._clone_dispersion(n, seed)
        marks = Validation.real_marks(season)
        rep["real"] = {"var": marks.get("inn18_var"),
                       "indep": marks.get("inn18_indep"),
                       "cov": marks.get("inn18_cov"),
                       "pair_cov": marks.get("inn18_pair_cov")}
        return rep

    @staticmethod
    def _slate_row(g: dict) -> Optional[dict]:
        """One hydrated schedule game -> the fields the harness needs, or None."""
        if (g.get("status") or {}).get("abstractGameState") != "Final":
            return None
        inns = (g.get("linescore") or {}).get("innings") or []
        away = [i["away"]["runs"] for i in inns
                if (i.get("away") or {}).get("runs") is not None]
        home = [i["home"]["runs"] for i in inns
                if (i.get("home") or {}).get("runs") is not None]
        if len(away) < 8 or len(home) < 8:
            return None

        t = g.get("teams") or {}
        hs, aws = t.get("home") or {}, t.get("away") or {}

        def abbr(side: dict) -> str:
            a = (side.get("team") or {}).get("abbreviation") or ""
            return normalize_club(a)

        ha, aa = abbr(hs), abbr(aws)
        if not ha or not aa:
            return None

        lu = g.get("lineups") or {}
        wx = g.get("weather") or {}
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*mph,\s*(.*)", str(wx.get("wind") or ""))
        temp = wx.get("temp")
        return {
            "pk": g.get("gamePk"),
            "date": g.get("officialDate") or "",
            # FIRST PITCH, UTC ISO. The slate carried only the DATE, so the
            # engine could not ask any question involving when a game starts —
            # day/night, body clock, shadows. §7 records circadian as
            # UNDERPOWERED rather than absent, and that test had to run against
            # a different database because this one had no clock.
            "start": g.get("gameDate") or "",
            "day_night": g.get("dayNight") or "",
            "home": ha, "away": aa,
            "venue": (g.get("venue") or {}).get("name") or "",
            "home_sp": ((hs.get("probablePitcher") or {}).get("id")),
            "away_sp": ((aws.get("probablePitcher") or {}).get("id")),
            "home_lineup": [p.get("id") for p in (lu.get("homePlayers") or [])][:9],
            "away_lineup": [p.get("id") for p in (lu.get("awayPlayers") or [])][:9],
            # The posted CATCHER. The hydrate already carries `primaryPosition`;
            # keeping only ids threw it away, which is why per-catcher framing was
            # not testable in the backtest. Framing is a PLAYER skill, so a club
            # aggregate is the wrong object to lag (see `catcher_framing_per_game`).
            "home_catcher": LiveSlate._lineup_catcher(lu.get("homePlayers")),
            "away_catcher": LiveSlate._lineup_catcher(lu.get("awayPlayers")),
            "condition": wx.get("condition"),
            "temp_f": float(temp) if temp not in (None, "") else None,
            "wind_mph": float(m.group(1)) if m else None,
            "wind_label": (m.group(2).strip() if m else ""),
            "home_innings": home,
            "away_innings": away,
        }


def validate_vs_reality(n: int = 20000, seed: int = 5) -> dict:
    """Run the league-average sim and score it against the real marks."""
    side = league_side

    res = simulate_many(side("H"), side("A"), n=n, seed=seed)
    runs = [r.runs_home for r in res] + [r.runs_away for r in res]
    tot = [r.runs_home + r.runs_away for r in res]
    dec = [r for r in res if r.runs_home != r.runs_away]
    got = {
        "team_game_runs_mean": statistics.mean(runs),
        "team_game_runs_sd": statistics.pstdev(runs),
        "game_total_mean": statistics.mean(tot),
        "game_total_median": statistics.median(tot),
        "game_total_sd": statistics.pstdev(tot),
        "home_win_rate": sum(1 for r in dec if r.runs_home > r.runs_away) / len(dec),
    }
    marks = Validation.real_marks()
    return {k: {"sim": v, "real": marks[k], "diff": v - marks[k]}
            for k, v in got.items()}


# ===========================================================================
# 17b. THE REAL SLATE — the comparison league-average clones cannot make
# ===========================================================================
# `validate_vs_reality` and `validate_dispersion` put league-average CLONES on
# both sides — right for the base/out machinery, WRONG for anything involving
# matchup spread, which is zero there by construction. **The real comparison is
# computed from the same games that were simulated** and through the same
# `dispersion_report`, so a difference cannot be a difference in what was
# measured.

SLATE_CACHE = SAVE_DIR / "season_slate_{season}.json"

# StatsAPI serves the whole hydrate in one request per window; 30 days keeps
# each response near 3 MB.
SLATE_WINDOW_DAYS = 30


def _dedupe_slate(rows: List[dict]) -> List[dict]:
    """One row per gamePk, the LAST entry winning.

    A rescheduled or resumed game comes back under TWO schedule entries sharing a
    `pk` and differing only in `start`/`day_night` — five across 2025-26 — so a
    duplicate counts that game's runs TWICE, in the park factors, the forecast
    fetch and any backtest. 0.2% of games, with no error attached.

    **NOT the doubleheader case.** Those share a date and both clubs while
    carrying DISTINCT `pk`s — 32 in 2025 — and must survive.
    """
    seen: Dict[int, dict] = {}
    for r in rows:
        try:
            seen[int(r["pk"])] = r
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(seen.values(), key=lambda r: (r["date"], r["pk"]))


def season_slate(season: Optional[int] = None, start: Optional[str] = None,
                 end: Optional[str] = None, refresh: bool = False,
                 timeout: float = 90.0,
                 save_dir: Path = SAVE_DIR) -> List[dict]:
    """Every completed regular-season game as a ready-to-simulate matchup.

    One hydrated schedule pull carries all of it — the real starter, the real
    posted lineup, the venue, the game-time weather and the linescore the sim
    will be scored against. Cached, because a season is ~190 requests.

    Note the weather here is StatsAPI's own, whose wind string is already
    FIELD-relative ("12 mph, Out To CF"), so it carries `wind_label` and needs
    no azimuth rotation — see `weather_tilt`.
    """
    season = CURRENT_SEASON if season is None else int(season)
    path = Path(str(SLATE_CACHE).format(season=season))
    if path.exists() and not refresh:
        try:
            with open(path) as fh:
                # deduped on READ as well as on write: the caches on disk
                # predate this and re-scraping a season to fix five rows would
                # be 190 requests for a 0.2% correction
                return _dedupe_slate(json.load(fh))
        except (OSError, ValueError):
            pass

    d0 = datetime.date.fromisoformat(start or f"{season}-03-01")
    d1 = datetime.date.fromisoformat(end or f"{season}-11-01")
    out: List[dict] = []
    cur = d0
    while cur <= d1:
        hi = min(cur + datetime.timedelta(days=SLATE_WINDOW_DAYS - 1), d1)
        url = StatsApi.schedule_url(
            start=cur.isoformat(), end=hi.isoformat(), game_type="R",
            hydrate="linescore,weather,team,probablePitcher,lineups")
        data = requests.get(url, timeout=timeout).json()
        for day in data.get("dates", []):
            for g in day.get("games", []):
                row = Validation._slate_row(g)
                if row is not None:
                    out.append(row)
        cur = hi + datetime.timedelta(days=1)

    out.sort(key=lambda r: (r["date"], r["pk"]))
    # **One row per gamePk.** A rescheduled or resumed game comes back under TWO
    # schedule entries sharing a `pk` and differing only in `start`/`day_night` —
    # five in 2025-26 — so a duplicate counts that game's runs TWICE, in the park
    # factors, the forecast fetch and any backtest. 0.2% of games, no error
    # attached. The LAST entry wins, which is the rescheduled one. NOT the
    # doubleheader case: those carry DISTINCT `pk`s and must survive.
    out = _dedupe_slate(out)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(out, fh)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# PERIOD-CORRECT weather — the forecast the market actually had
# ---------------------------------------------------------------------------
# **The slate's weather is StatsAPI's GAME-TIME OBSERVATION, and that is a
# look-ahead**: the opening price is hung a median ~1.1 days early off a
# FORECAST, so 3d.12's CLV is partly measuring that. Ablating weather answers
# the wrong question — what we want is the SAME weather the market had, and
# Open-Meteo archives its own past forecast runs.
#
# **The correction is not cosmetic**: at Wrigley the day-1 forecast misses by
# **32.2 degrees on wind DIRECTION**, and 5b.2 established that direction is the
# whole term. One request per park covers a season. sim_state.md A.17b.
FORECAST_WX_PATH_FMT = "weather_forecast_{season}_d{lag}.json"

# Which weather the rate/context layer sees. "observed" is StatsAPI's game-time
# reading and ships, because a LIVE projection legitimately has tonight's
# forecast and this is the closest thing to it. "forecast_d1" is the
# period-correct version and is what any comparison against a MARKET PRICE
# should use. Uppercase, so `_slate_overrides` ships it to a pool worker.
WEATHER_SOURCE = "observed"
WEATHER_FORECAST_LAG_DAYS = 1

_FCST_WX: Dict[tuple, Dict[int, dict]] = {}


class SlateWeather:
    """Period-correct weather — the forecast the market actually had."""

    @staticmethod
    def forecast_weather_path(season: int, lag: int = 1,
                              save_dir: Path = SAVE_DIR) -> Path:
        return Path(save_dir) / FORECAST_WX_PATH_FMT.format(season=season, lag=lag)

    @staticmethod
    def _slate_context(season: int, save_dir: Path) -> tuple:
        key = (int(season), str(save_dir))
        got = _SLATE_TABLES.get(key)
        if got is None:
            bat_table, _ = build_rates("bat", save_dir=save_dir)
            pit_table, _ = build_rates("pit", save_dir=save_dir)
            hz = starter_hazard()
            got = (bat_table, pit_table, hz)
            _SLATE_TABLES[key] = got
        return got

    @staticmethod
    def _slate_total_report(sim_tot: Sequence[float], sim_mc: Sequence[float],
                            real_tot: Sequence[int], reps: int) -> dict:
        """Game-total agreement, with the harness's OWN noise floor removed.

        **`reps` is not a free knob and a low one silently fakes both numbers.** A
        per-game mean over `reps` sims carries Monte Carlo noise of variance
        `mc/reps`; at reps=10 that is ~1.4 runs against a real model spread near
        1.0, so most of `model_sd` is the harness and the correlation is
        attenuated by about the same factor. Two fatigue variants once read 0.194
        and 0.152 purely on that.

        So the MC component is measured per game and reported alongside:
        `model_sd_adj` and `corr_adj` have it removed. Neither can be trusted when
        `mc_share` is large — raise `reps` instead.
        """
        n = len(sim_tot)
        if n < 3:
            return {"n": n}
        var_obs = statistics.pstdev(sim_tot) ** 2
        var_mc = (statistics.mean(sim_mc) / reps) if reps > 1 else 0.0
        var_adj = max(var_obs - var_mc, 0.0)
        corr = _corr(sim_tot, [float(x) for x in real_tot])
        corr_adj = None
        if corr is not None and var_adj > 0:
            corr_adj = corr * (var_obs / var_adj) ** 0.5
        return {
            "n": n, "reps": reps,
            "sim_mean": statistics.mean(sim_tot),
            "real_mean": statistics.mean(real_tot),
            "model_sd": var_obs ** 0.5,
            "mc_sd": var_mc ** 0.5,
            "model_sd_adj": var_adj ** 0.5,
            "mc_share": (var_mc / var_obs) if var_obs else 0.0,
            "corr": corr, "corr_adj": corr_adj,
            "rmse": statistics.mean((a - b) ** 2 for a, b
                                    in zip(sim_tot, real_tot)) ** 0.5,
        }


def weather_source_lag(source: Optional[str] = None) -> Optional[int]:
    """`"forecast_d1"` -> 1. None when the source is the observation.

    **Day 0 is not the same claim as day 1 and both are needed.** Day 0 is
    Open-Meteo's own analysis — still a look-ahead, like the shipped observation —
    but it reaches the engine through the SAME continuous-bearing path as the
    forecast. Without it an arm changes the information set AND the representation
    at once (StatsAPI's label is a coarse eight-way bucket), and the two cannot be
    told apart. Day 0 is the matched control.
    """
    src = WEATHER_SOURCE if source is None else source
    m = re.match(r"forecast_d(\d+)$", str(src or ""))
    return int(m.group(1)) if m else None


def load_forecast_weather(season: int, lag: int = 1,
                          save_dir: Path = SAVE_DIR) -> Dict[int, dict]:
    """{game_pk: weather} for a season at one forecast lag. {} when absent."""
    key = (int(season), int(lag))
    if key in _FCST_WX:
        return _FCST_WX[key]
    path = SlateWeather.forecast_weather_path(season, lag, save_dir)
    out: Dict[int, dict] = {}
    if path.exists():
        try:
            with open(path) as fh:
                # JSON keys are strings; the callers hold ints
                out = {int(k): v for k, v in json.load(fh).items()}
        except (OSError, ValueError):
            out = {}
    _FCST_WX[key] = out
    return out


def fetch_forecast_weather(season: Optional[int] = None,
                           lag_days: Optional[int] = None,
                           save_dir: Path = SAVE_DIR,
                           verbose: bool = True) -> Dict[int, dict]:
    """The forecast as it stood `lag_days` before each game, per game_pk.

    One request per PARK covering the whole season, indexed onto each game by its
    first-pitch UTC hour. Two things are deliberate: **the wind comes back as a
    COMPASS bearing** and is tagged `wind_frame="compass"` so `weather_tilt`
    rotates it by the park azimuth — StatsAPI's label is already field-relative,
    and mixing the two is the error CLAUDE.md records; and **`condition` is
    carried over from the observation** purely so the ROOF-CLOSED test still
    fires, since a shut roof is close to knowable in advance and is not the leak
    being closed here.
    """
    season = CURRENT_SEASON if season is None else int(season)
    lag = WEATHER_FORECAST_LAG_DAYS if lag_days is None else lag_days
    wm = weatherman
    slate = season_slate(season, save_dir=save_dir)
    by_park: Dict[str, List[dict]] = {}
    unresolved: Dict[str, int] = {}
    for row in slate:
        park = resolve_venue(row.get("venue") or "")
        if not park or park not in wm.STADIUM_DATA:
            unresolved[str(row.get("venue"))] = (
                unresolved.get(str(row.get("venue")), 0) + 1)
            continue
        by_park.setdefault(park, []).append(row)

    suffix = f"_previous_day{lag}" if lag else ""
    out: Dict[int, dict] = {}
    if verbose:
        print(f"[forecastwx] {season}: {len(by_park)} parks, "
              f"{sum(len(v) for v in by_park.values())} games, "
              f"forecast as of {lag} day(s) out")
        if unresolved:
            print(f"[forecastwx] {sum(unresolved.values())} games at parks with "
                  f"no coordinates, left WITHOUT weather rather than given the "
                  f"observation: {unresolved}")
    Archive._progress(f"forecastwx {season}: {len(by_park)} parks to fetch")

    for i, (park, rows) in enumerate(sorted(by_park.items()), 1):
        meta = wm.STADIUM_DATA[park]
        days = sorted({r["date"] for r in rows if r.get("date")})
        if not days:
            continue
        params = {
            "latitude": meta["lat"], "longitude": meta["lon"],
            # a day either side, because first pitch in UTC can land on the
            # neighbouring calendar day for a night game
            "start_date": RelieverUsage._days_before(days[0], 1),
            "end_date": RelieverUsage._days_before(days[-1], -1),
            # **`surface_pressure`, NOT `pressure_msl`.** The former is at the
            # park's own elevation, which is what air density wants; feeding a
            # sea-level reading into a station-level correction turns Coors'
            # 840 hPa into 664 — a 21% density error worth +27 ft of carry
            # (CLAUDE.md, the `pressure_frame` tag exists for this).
            "hourly": ",".join(f"{v}{suffix}" for v in (
                "temperature_2m", "wind_speed_10m", "wind_direction_10m",
                "surface_pressure", "relative_humidity_2m")),
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "UTC"}
        try:
            r = requests.get(OpenMeteo.PREVIOUS_RUNS, params=params,
                             timeout=OpenMeteo.TIMEOUT)
            h = (r.json() or {}).get("hourly") or {}
        except Exception as e:                       # network, JSON, anything
            print(f"[forecastwx] {park}: FAILED ({e}) — its games keep no "
                  f"weather rather than the observation")
            continue
        idx = {}
        t = h.get("time") or []
        for j, ts in enumerate(t):
            idx[ts] = (
                (h.get(f"temperature_2m{suffix}") or [None] * len(t))[j],
                (h.get(f"wind_speed_10m{suffix}") or [None] * len(t))[j],
                (h.get(f"wind_direction_10m{suffix}") or [None] * len(t))[j],
                (h.get(f"surface_pressure{suffix}") or [None] * len(t))[j],
                (h.get(f"relative_humidity_2m{suffix}") or [None] * len(t))[j])
        hit = 0
        for row in rows:
            key = _forecast_hour_key(row.get("start"))
            got = idx.get(key) if key else None
            if not got or got[0] is None:
                continue
            temp, spd, deg, pres, rh = got
            out[int(row["pk"])] = {
                "condition": row.get("condition"),
                "temp_f": temp, "wind_mph": spd, "wind_dir_deg": deg,
                # STATION-level pressure (hPa) and relative humidity (%), the
                # two inputs air density needs beyond temperature. Tagged so a
                # consumer cannot mistake the frame.
                "pressure_hpa": pres, "humidity_pct": rh,
                "pressure_frame": "station",
                # NOT field-relative: it is a compass bearing and must be
                # rotated by the park azimuth (CLAUDE.md, section 4)
                "wind_frame": "compass"}
            hit += 1
        if verbose:
            print(f"[forecastwx] {i:2d}/{len(by_park)} {park:28s} "
                  f"{hit}/{len(rows)} games", flush=True)
        Archive._progress(f"forecastwx {season} {i}/{len(by_park)} {park} {hit}")

    path = SlateWeather.forecast_weather_path(season, lag, save_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump({str(k): v for k, v in out.items()}, fh)
    _FCST_WX.pop((int(season), int(lag)), None)
    if verbose:
        print(f"\n[forecastwx] {season}: {len(out)}/{len(slate)} games -> {path}")
    Archive._progress(f"forecastwx {season} FINISHED {len(out)}/{len(slate)}")
    return out


# A nine-inning game spans about three hours and the weather does not hold
# still. At Sutter Health Park on 2026-08-24 the forecast ran 85.0F/9.8mph at
# first pitch and 75.0F/7.4mph three hours later. Priced off first pitch alone
# that game reads +1.88 runs of weather; across the window it is +1.06, and the
# window MEAN is within 0.2F of the park's own reference — so the entire heat
# term was an artifact of the hour we happened to sample.
GAME_WINDOW_HOURS = 3


def _game_window_mean(arch, venue: str, start_iso: str) -> Optional[dict]:
    """Forecast rows averaged over the hours a game actually spans.

    **The wind is averaged as a VECTOR, not as a speed and a bearing.** Wind
    direction is circular — 350 and 10 degrees average to 0, not to 180 — and
    what the physics consumes is the out-to-centre COMPONENT, which is exactly
    the projection of the mean vector. Averaging speed and bearing separately
    would be wrong in both directions at once.
    """
    try:
        rows = arch.park_forecast(venue, forecast_days=3, past_days=1)
        t0 = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        t0 = t0.astimezone(datetime.timezone.utc)
    except Exception:                                          # noqa: BLE001
        return None
    # Round to the NEAREST hour first, as `WeatherArchive.at` does. Truncating
    # a 01:40 first pitch to 01:00 starts the window 40 minutes before the game
    # and ends it an hour before the game does — at Sutter that pulled the
    # hottest, windiest hour of the evening INTO the average and dropped the
    # calmest one out.
    t0 = (t0 + datetime.timedelta(minutes=30)).replace(
        minute=0, second=0, microsecond=0)
    want = {(t0 + datetime.timedelta(hours=k)).strftime("%Y-%m-%dT%H:00")
            for k in range(GAME_WINDOW_HOURS)}
    got = [r for r in rows
           if str(r.get("time", ""))[:13] + ":00" in {w[:13] + ":00" for w in want}
           and r.get("temperature") is not None]
    if len(got) < 2:
        return None
    n = float(len(got))
    u = sum(-(r["wind_speed"] or 0.0)
            * math.sin(math.radians(r["wind_direction"] or 0.0)) for r in got) / n
    v = sum(-(r["wind_speed"] or 0.0)
            * math.cos(math.radians(r["wind_direction"] or 0.0)) for r in got) / n
    spd = math.hypot(u, v)
    deg = (math.degrees(math.atan2(-u, -v))) % 360.0
    out = dict(got[0])
    out["temperature"] = sum(r["temperature"] for r in got) / n
    out["wind_speed"], out["wind_direction"] = spd, deg
    for k in ("humidity", "pressure_hpa"):
        vals = [r.get(k) for r in got if r.get(k) is not None]
        if vals:
            out[k] = sum(vals) / len(vals)
    return out


def forecast_game_weather(venue: Optional[str], start_iso: Optional[str]
                          ) -> Optional[dict]:
    """The FORECAST for one scheduled game, in `weather_tilt`'s own shape.

    **The forecast pipeline in this module is retrospective by construction and
    cannot serve a future game.** `fetch_forecast_weather` iterates COMPLETED
    games against Open-Meteo's PREVIOUS-RUNS archive — a look-ahead control for
    the backtest, not a forward projection. So a live projection of tomorrow's
    slate got `weather_tilt = 0.0` on every game, silently, and weather is worth
    0.0317 runs/degF and 0.0618 runs/mph wind-out.

    The frame TAGS are carried through deliberately and not re-derived:
    Open-Meteo's wind is a COMPASS bearing needing the park-azimuth rotation, and
    its `surface_pressure` is already at the park's elevation. Mislabelling
    either is the error CLAUDE.md records.
    """
    if not venue or not start_iso:
        return None
    try:
        wm = weatherman
        # **RESOLVE the name first.** StatsAPI serves "Rate Field" where
        # `STADIUM_DATA` holds "Guaranteed Rate Field". An exact-match test
        # silently returned None, so an OPEN-roof park lost its forecast and
        # priced neutral with nothing to say so — trap 2, a silent key miss
        # returns a default, not an error.
        venue = resolve_venue(venue) or venue
        if venue not in wm.STADIUM_DATA:
            return None
        # `WeatherArchive`, not `WeatherService` — the latter is the
        # OpenWeather current-conditions client and has no forecast hours.
        arch = wm.WeatherArchive()
        row = arch.at(venue, str(start_iso))
        row = _game_window_mean(arch, venue, str(start_iso)) or row
    except Exception:                                          # noqa: BLE001
        return None
    if not row or row.get("temperature") is None:
        return None
    # **The ROOF, which a forecast cannot see and a sky condition is not.**
    # `weather_tilt` tests `condition` against `ROOF_CLOSED_CONDITIONS`, but
    # Open-Meteo's `condition` is the WMO SKY code ("Clear"), so passing it
    # through silently disables the roof test — Chase Field on a 103.6F day took
    # a full +0.65-run heat bonus for a game played under a shut roof at ~72F.
    #
    # A fixed roof is KNOWN and stamped closed. A RETRACTABLE one is a decision
    # made on the day, and guessing it is exactly the "correction added for a
    # plausible reason with no support" this project keeps recording — so those
    # games get NO forecast. Weather is wired where it cannot be confounded.
    roof = str(((weatherman.STADIUM_DATA.get(venue) or {}).get("roof") or "")).lower()
    if roof in ("retractable",):
        return None
    if roof in ("dome", "fixed", "closed"):
        return {"condition": "dome", "temp_f": row.get("temperature"),
                "wind_mph": 0.0, "wind_label": "", "source": "forecast"}
    return {"condition": row.get("condition"),
            "temp_f": row.get("temperature"),
            "wind_mph": row.get("wind_speed"),
            "wind_dir_deg": row.get("wind_direction"),
            "pressure_hpa": row.get("pressure_hpa"),
            "humidity_pct": row.get("humidity"),
            # tagged, never inferred — see the docstring
            "pressure_frame": "station",
            "wind_frame": row.get("wind_frame") or "compass",
            "source": "forecast"}


# ---------------------------------------------------------------------------
# WHICH weather source a LIVE projection uses
# ---------------------------------------------------------------------------
# **StatsAPI's wind is an 8-way TEXT LABEL and it can be badly wrong.** On
# 2026-08-29 BAL @ ATH it read "R To L" — a crosswind, out-component exactly
# 0.0 — while Open-Meteo's bearing (209 deg) and RotoGrinders (SSW) INDEPENDENTLY
# agreed the wind was blowing OUT at ~7.4 mph, and agreed with each other on
# speed (7.9 / 8.0) against StatsAPI's 11. A ~70 degree miss, nearly two label
# buckets. At Sutter Health Park, whose wind factor is 2.296 — the highest of
# the thirty — that was **1.24 runs on the game total**, and it flipped the
# model from +0.7 over the market to -0.07 and back.
#
# The forecast carries a NUMERIC bearing that `weather_tilt` rotates into the
# park frame; the label throws that resolution away before the model ever sees
# it. So a LIVE projection asks the forecast FIRST.
#
# CLAUDE.md validated these labels over 265 games — circular mean offset
# +0.1 deg, R = 0.72, no PARK off by more than one 45-degree bucket. That is a
# park-MEAN result: it says the rotation is right on average, not that any one
# game's label is. R = 0.72 leaves exactly the per-game scatter that bit here.
# The forecast is used instead. No switch: a neutralised knob is dead code with
# a switch on it, and this file has retired two already on that reasoning.
#
# `game_weather` is NOT deleted — it is still the right object for a game that
# has already been played, and `season_slate` rows carry the same observation
# for every backtest. It is out of the LIVE pricing path, which is where a
# forecast is the honest information set anyway.


def live_game_weather(game_pk: Optional[int], date: str,
                      venue: Optional[str], start_iso: Optional[str]
                      ) -> Optional[dict]:
    """Tonight's conditions for a LIVE projection: the FORECAST.

    **The ROOF is the one thing still read from StatsAPI, and it is not
    weather — it is a stadium state.** Open-Meteo's `condition` is a WMO SKY
    code, so a forecast cannot see a shut roof; `forecast_game_weather` stamps
    the FIXED domes itself but deliberately refuses to guess a RETRACTABLE one,
    which is a decision made on the day. Dropping the observed roof would price
    a covered game as an open one — the Chase Field defect, +0.65 runs on a
    103F day played at ~72F. So the roof crosses over and nothing else does.
    """
    fc = forecast_game_weather(venue, start_iso)
    obs = None
    if game_pk:
        try:
            obs = game_weather(int(game_pk), date)
        except Exception:                                      # noqa: BLE001
            obs = None
    roof = (obs and str(obs.get("condition") or "").strip().lower()
            in ROOF_CLOSED_CONDITIONS)
    if fc is None:
        # No forecast for this park. Do NOT silently price it neutral — a
        # closed roof is still knowable and is the whole run environment.
        return {"condition": obs["condition"], "temp_f": None,
                "wind_mph": None, "source": "roof-only"} if roof else None
    if roof:
        fc = dict(fc)
        fc["condition"] = obs["condition"]
    return fc


def _forecast_hour_key(start_iso: Optional[str]) -> Optional[str]:
    """First pitch -> the Open-Meteo hourly key, truncated to the hour, UTC."""
    if not start_iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(
            str(start_iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:00")


def _slate_weather(row: dict) -> Optional[dict]:
    """The weather dict `weather_tilt` consumes, out of a slate row.

    Under `WEATHER_SOURCE = "forecast_d1"` this serves the PERIOD-CORRECT
    forecast instead of the game-time observation. A game with no forecast row
    gets NO weather rather than the observation: falling back would quietly
    reintroduce the exact look-ahead the setting exists to remove, on the
    handful of games least likely to be noticed.
    """
    lag = weather_source_lag()
    if lag is not None:
        try:
            season = int(str(row.get("date") or "")[:4])
        except ValueError:
            return None
        return load_forecast_weather(season, lag).get(int(row.get("pk") or 0))
    if row.get("temp_f") is None and row.get("wind_mph") is None:
        return None
    return {"condition": row.get("condition"), "temp_f": row.get("temp_f"),
            "wind_mph": row.get("wind_mph"),
            "wind_label": row.get("wind_label") or ""}


def slate_sides(slate: Sequence[dict], bat_table: Dict[int, dict],
                pit_table: Dict[int, dict], season: Optional[int] = None,
                hazard: Optional[List[float]] = None,
                save_dir: Path = SAVE_DIR) -> Dict[str, "TeamSide"]:
    """One board-built TeamSide per club appearing on the slate.

    Built once and shared: `simulate_game` never mutates a side, and rebuilding
    30 clubs per game costs more than every simulation put together.
    """
    season = CURRENT_SEASON if season is None else int(season)
    clubs = sorted({r["home"] for r in slate} | {r["away"] for r in slate})
    out: Dict[str, TeamSide] = {}
    for c in clubs:
        try:
            out[c] = build_side(c, bat_table, pit_table, season, hazard,
                                save_dir)
        except (ValueError, KeyError):
            continue
    return out


# **The posted lineup is a LOOK-AHEAD against the opening price**, which is the
# one thing that could manufacture the CLV in 3d.12: lineups go up after the
# opener is hung and before the close, and they MOVE a baseball line. Off,
# `_game_side` keeps the board's best-nine-by-PA — which is POSITIVELY SELECTED
# (5.6a), so the ablated arm carries a slightly BETTER offence than reality and
# biases toward finding less difference, not more.
#
# The STARTER is deliberately NOT gated with it: probables are announced days
# ahead and are normally known when the opener is hung. Late scratches are the
# exception and are not separable here.
USE_POSTED_LINEUP = True


def _game_side(base: "TeamSide", sp_id: Optional[int],
               lineup_ids: Sequence[int], bat_table: Dict[int, dict],
               pit_table: Dict[int, dict], season: int,
               save_dir: Path,
               catcher_id: Optional[int] = None
               ) -> Tuple["TeamSide", bool, bool]:
    """`base` with tonight's real starter and posted lineup swapped in.

    Returns (side, used_sp, used_lineup) — a silent fallback to the board's
    highest-GS arm is indistinguishable from having used the real thing, and
    on a season slate it is the difference between modelling the pitching
    matchup and not modelling it at all.
    """
    lineup, pen, sp = base.lineup, base.bullpen, base.starter
    used_sp = used_lineup = False
    # Tonight's actual catcher, when the pitch-level series is on. Lagged by
    # `TEAM_CONTEXT_LAG` like every other team-context term — and lagging a
    # CATCHER is legitimate where lagging a CLUB is not, because his skill
    # goes with him when he is traded.
    catcher_framing = (Framing.catcher_framing_per_game(
        catcher_id, season - TEAM_CONTEXT_LAG, save_dir)
        if (USE_PITCH_FRAMING and catcher_id is not None) else None)

    if sp_id:
        share = starter_gs_share(int(sp_id), season, save_dir)
        traits = RelieverTraits.load_reliever_traits(season).get(int(sp_id)) or {}
        is_opener = share is not None and share < OPENER_GS_SHARE
        bf_target = (start_bf_estimate(int(sp_id), season, save_dir)
                     or traits.get("bf_per_outing") or 4.5)
        # `base.starter` is replacement-level rather than None now, but this
        # must not depend on that: it is read to build the REPLACEMENT for
        # itself, so a caller who hands in a hand-built side with no starter
        # gets the league curve instead of an AttributeError.
        hz = (opener_hazard(bf_target) if is_opener
              else (base.starter.hazard if base.starter is not None
                    else starter_hazard()))
        cand = make_pitcher(int(sp_id), pit_table, is_starter=True, hazard=hz)
        if cand is not None:
            sp, used_sp = cand, True
            pen = [p for p in pen if p.player_id != int(sp_id)]

    if USE_POSTED_LINEUP and lineup_ids and len(lineup_ids) >= 9:
        # **A hitter with no rate row gets a REPLACEMENT-LEVEL line, not a
        # rejected lineup.** This used to require all nine to resolve and fall
        # back to the board's best-nine-by-PA otherwise, which discarded eight
        # known hitters because a callup had no board row — and the fallback
        # lineup is positively selected, so those games were handed a BETTER
        # offence than the one that actually played. It fired on 1.8-9% of
        # games depending on the cutoff, worst in April, which is exactly
        # where the as-of backtest is thinnest. Same error as the pen arm that
        # was dropped instead of replaced (5.5a): dropping an entity is never
        # neutral, and the direction of the bias is never zero.
        got = []
        for p in lineup_ids[:9]:
            b = make_batter(int(p), bat_table, season, save_dir)
            got.append(b if b is not None
                       else replacement_batter(season, save_dir))
        if len(got) == 9 and all(b is not None for b in got):
            lineup, used_lineup = got, True

    return (TeamSide(lineup=lineup, starter=sp, bullpen=pen,
                     oaa=base.oaa, of_arm=base.of_arm,
                     # THIS catcher's framing when he is on file, the club's
                     # otherwise — a backup with no prior-season line falls
                     # back rather than being handed a zero.
                     framing=(catcher_framing if catcher_framing is not None
                              else base.framing),
                     catcher_id=catcher_id), used_sp, used_lineup)


# Per-worker caches. Rebuilding the rate tables and the 30 club sides costs
# ~5 s, so each process pays it once instead of once per chunk.
_SLATE_TABLES: Dict[tuple, tuple] = {}


# **Calibration state must travel to the pool as DATA, and the list of what
# travels must NOT be maintained by hand.** Under forkserver a worker
# RE-IMPORTS this module and gets the shipped constants back, so the parent
# reports the shipped model's numbers as the variant's with no error attached.
# It bit three times in one session — a fixed 4-tuple missed `STABILIZE_PA_*`,
# and the hand-maintained NAME LIST that replaced it missed `FRAMING_TILT_SCALE`
# the very next time a constant was added, both times tellingly producing two
# byte-identical result blocks. So the capture is AUTOMATIC.
_SLATE_OVERRIDE_EXTRA = ("_FATIGUE_FORCE",)
_SLATE_OVERRIDE_TYPES = (int, float, str, bool, tuple)


def _slate_overrides() -> Dict[str, object]:
    """Every constant a probe could have rebound, by name. Captured, not listed."""
    g = globals()
    out = {k: v for k, v in g.items()
           if k.isupper() and not k.startswith("__")
           and isinstance(v, _SLATE_OVERRIDE_TYPES)}
    out.update({k: g[k] for k in _SLATE_OVERRIDE_EXTRA if k in g})
    return out


def _slate_val_worker(job):
    """Simulate one chunk of the real slate. MUST stay at module level.

    `multiprocessing` pickles the callable by qualified name. Returns the raw
    8-inning vectors rather than a finished report, because the covariance
    decomposition has to be taken over the POOLED set: summing per-chunk
    covariances would drop every cross-chunk pair and centre each chunk on its own
    mean.
    """
    (rows, season, reps, seed, use_weather, use_venue, use_real_sp,
     use_real_lineups, save_dir, overrides, variant) = job
    # **Every calibration in this file works by rebinding a module global, and
    # on Python 3.14 that no longer survives the pool.** State travels as DATA in
    # the job, never as inherited memory. A NAME->VALUE dict rather than a fixed
    # tuple on purpose: the tuple version enumerated four constants, and the
    # first calibration to touch a fifth silently compared a variant against
    # itself.
    globals().update(overrides)
    # Anything derived from an overridden constant has to be recomputed, or the
    # worker uses a cache built from the shipped values.
    _BOARDS.clear(); _ASOF_BOARDS.clear()
    _PRIOR_CURVE.clear(); _PRIOR_LEAGUE.clear()
    _SLATE_TABLES.clear()
    _PIT_ROWS.clear(); _BAT_ROWS.clear()
    _PLATOON_GAPS.clear(); _CHED.clear(); _MILB_THROWS.clear()
    if variant is not None:
        # Same reason, one level worse: a monkeypatched FUNCTION cannot be
        # pickled into a fresh interpreter at all, so the fatigue probe sends
        # its parameters and the worker rebuilds the closure here.
        globals()["fatigue_multipliers"] = SlateCalibration._fatigue_variant(*variant)
    bat_table, pit_table, hz = SlateWeather._slate_context(season, Path(save_dir))
    bases = slate_sides([r for _, r in rows], bat_table, pit_table, season,
                        hz, Path(save_dir))

    vec: List[List[int]] = []
    tot: List[float] = []
    mc: List[float] = []
    real_vec: List[List[int]] = []
    real_tot: List[int] = []
    used = {"sp": 0, "lineup": 0, "weather": 0, "venue": 0, "games": 0}

    for idx, row in rows:
        hb, ab = bases.get(row["home"]), bases.get(row["away"])
        if hb is None or ab is None:
            continue
        home, hsp, hlu = _game_side(
            hb, row["home_sp"] if use_real_sp else None,
            row["home_lineup"] if use_real_lineups else (),
            bat_table, pit_table, season, Path(save_dir),
            row.get("home_catcher") if use_real_lineups else None)
        away, asp, alu = _game_side(
            ab, row["away_sp"] if use_real_sp else None,
            row["away_lineup"] if use_real_lineups else (),
            bat_table, pit_table, season, Path(save_dir),
            row.get("away_catcher") if use_real_lineups else None)
        venue = resolve_venue(row["venue"]) if use_venue else None
        wx = _slate_weather(row) if use_weather else None

        used["games"] += 1
        used["sp"] += int(hsp) + int(asp)
        used["lineup"] += int(hlu) + int(alu)
        used["weather"] += int(wx is not None)
        used["venue"] += int(venue is not None)

        # **Seeded per GAME, not per worker.** A worker-seeded stream would
        # make every number here depend on how many processes happened to be
        # free, so two runs of the same fit would disagree for a reason that
        # has nothing to do with the model.
        rng = random.Random(seed * 1_000_003 + idx)
        tots: List[int] = []
        for _ in range(reps):
            log: List[dict] = []
            res = simulate_game(home, away, rng, log=log, weather=wx,
                                venue=venue)
            vec += [v[:8] for v in Validation.inning_vectors([log]) if len(v) >= 8]
            tots.append(res.runs_home + res.runs_away)
        tot.append(statistics.mean(tots))
        mc.append(statistics.variance(tots) if reps > 1 else 0.0)
        real_vec += [row["away_innings"][:8], row["home_innings"][:8]]
        real_tot.append(sum(row["away_innings"]) + sum(row["home_innings"]))

    return {"vec": vec, "tot": tot, "mc": mc, "real_vec": real_vec,
            "real_tot": real_tot, "used": used}


def validate_slate_vs_reality(season: Optional[int] = None, reps: int = 15,
                              seed: int = 17, limit: Optional[int] = None,
                              use_weather: bool = True,
                              use_venue: bool = True,
                              use_real_sp: bool = True,
                              use_real_lineups: bool = True,
                              workers: Optional[int] = None,
                              save_dir: Path = SAVE_DIR) -> dict:
    """Simulate the season's real matchups and score them against themselves.

    Every game is played `reps` times with its own starters, lineups, park and
    game-time weather, and compared with the linescores of the very same games —
    so the comparison controls for schedule, park mix and opponent mix for free,
    none of which the clone harness can do. `workers` is processes, since the GIL
    makes threads worthless here; the answer does not depend on the count, because
    every game carries its own seed.
    """
    season = CURRENT_SEASON if season is None else int(season)
    slate = season_slate(season, save_dir=save_dir)
    if limit:
        slate = slate[:limit]
    if not slate:
        raise RuntimeError("mlb_sim: empty slate; run `mlb_sim.py slate --refresh`")

    workers = workers or max(1, min(len(slate), (os.cpu_count() or 4) - 2))
    workers = max(1, min(workers, len(slate)))
    indexed = list(enumerate(slate))
    # Round-robin, not contiguous blocks: the schedule is in date order, so
    # contiguous chunks hand one worker a whole month and its own club set.
    chunks = [indexed[i::workers] for i in range(workers)]
    overrides = _slate_overrides()
    jobs = [(c, season, reps, seed, use_weather, use_venue, use_real_sp,
             use_real_lineups, str(save_dir), overrides, _fatigue_variant_args)
            for c in chunks if c]

    if workers == 1:
        parts = [_slate_val_worker(j) for j in jobs]
    else:
        with multiprocessing.Pool(workers) as pool:
            parts = list(pool.imap_unordered(_slate_val_worker, jobs))

    sim_vec: List[List[int]] = []
    real_vec: List[List[int]] = []
    sim_tot: List[float] = []
    sim_mc: List[float] = []
    real_tot: List[int] = []
    used = {"sp": 0, "lineup": 0, "weather": 0, "venue": 0, "games": 0}
    for p in parts:
        sim_vec += p["vec"]
        real_vec += p["real_vec"]
        sim_tot += p["tot"]
        sim_mc += p["mc"]
        real_tot += p["real_tot"]
        for k in used:
            used[k] += p["used"][k]

    sim = Validation.dispersion_report(sim_vec)
    real = Validation.dispersion_report(real_vec)
    return {
        "season": season, "reps": reps, "workers": workers, "used": used,
        "sim": sim, "real": real,
        "game_total": SlateWeather._slate_total_report(sim_tot, sim_mc, real_tot, reps),
    }


# ---------------------------------------------------------------------------
# Fatigue, scored on the real slate — sim_state.md 5.4
# ---------------------------------------------------------------------------

class SlateCalibration:
    """Fatigue and the form draw, fitted on the REAL slate."""

    @staticmethod
    def multiplier_run_value(n: int = 4000, seed: int = 3,
                             probe: float = 0.04) -> dict:
        """Runs per PA per unit of the fatigue/HFA multiplier bundle. MEASURED.

        The bridge between this engine's units and the RV/PA the play-by-play
        measurements and the literature are quoted in — §5.4 is exactly that
        comparison, and the shipped 0.004/batter had to be converted before anyone
        could see it was 4.2 standard errors off a measured zero. Run on
        league-average clones through `simulate_game`'s own `context`, so the
        number comes out of the same code path the term uses.
        """
        side = league_side

        def at(d: float) -> Tuple[float, float]:
            bundle = {HR: d, S1B: d, S2B: d, BB: d,
                      K: 1.0 / d, GB_OUT: 1.0 / d, AIR_OUT: 1.0 / d}
            rng = random.Random(seed)
            home, away = side("H"), side("A")
            ctx = {"home": bundle, "away": bundle}
            runs, pa = [], []
            for _ in range(n):
                r = simulate_game(home, away, rng, context=ctx)
                runs += [r.runs_home, r.runs_away]
                pa.append(sum(p.bf for p in r.pitchers.values()) / 2.0)
            return statistics.mean(runs), statistics.mean(pa)

        lo, _ = at(1.0 - probe)
        mid, pa = at(1.0)
        hi, _ = at(1.0 + probe)
        per_unit = (hi - lo) / (2 * probe)
        return {"runs_per_team_game_per_unit": per_unit,
                "pa_per_team_game": pa,
                "rv_per_pa_per_unit": per_unit / pa if pa else 0.0,
                "runs_at": {round(1 - probe, 3): lo, 1.0: mid,
                            round(1 + probe, 3): hi}}

    @staticmethod
    def _fatigue_variant(decline: float, opening: float, opening_bf: int):
        """Build a `fatigue_multipliers` for one candidate curve. Probe only."""
        def patched(bf, decline_per_bf=None, ref_bf=None):
            ref_bf = FATIGUE_REF_BF if ref_bf is None else float(ref_bf)
            d = 1.0 + decline * (bf - ref_bf)
            if bf < opening_bf:
                d *= opening
            d = max(d, 0.5)
            return {HR: d, S1B: d, S2B: d, BB: d,
                    K: 1.0 / d, GB_OUT: 1.0 / d, AIR_OUT: 1.0 / d}
        return patched

    @staticmethod
    def _fatigue_probe(decline: float, opening: float, opening_bf: int,
                       season: int, reps: int, seed: int,
                       workers: Optional[int] = None) -> dict:
        """One fatigue variant, scored on the real slate."""
        global FATIGUE_DECLINE_PER_BF, _FATIGUE_FORCE, _fatigue_variant_args
        old = (FATIGUE_DECLINE_PER_BF, _FATIGUE_FORCE, _fatigue_variant_args)
        old_fn = globals()["fatigue_multipliers"]

        # The hot loop skips the call entirely at a zero gradient, so any variant
        # acting at bf 1-2 needs the call forced back on.
        FATIGUE_DECLINE_PER_BF = decline
        _FATIGUE_FORCE = decline != 0.0 or opening != 1.0
        _fatigue_variant_args = (decline, opening, opening_bf)
        globals()["fatigue_multipliers"] = SlateCalibration._fatigue_variant(*_fatigue_variant_args)
        try:
            r = validate_slate_vs_reality(season, reps=reps, seed=seed,
                                          workers=workers)
        finally:
            (FATIGUE_DECLINE_PER_BF, _FATIGUE_FORCE, _fatigue_variant_args) = old
            globals()["fatigue_multipliers"] = old_fn

        sim, real = r["sim"], r["real"]
        diff = [s - t for s, t in zip(sim["by_inning"], real["by_inning"])]
        return {
            "decline": decline, "opening": opening,
            "inning1_sim": sim["by_inning"][0], "inning1_real": real["by_inning"][0],
            "inning1_diff": diff[0],
            "lift_sim": sim["by_inning"][0] - statistics.mean(sim["by_inning"]),
            "lift_real": real["by_inning"][0] - statistics.mean(real["by_inning"]),
            "profile_rmse": statistics.mean(x * x for x in diff) ** 0.5,
            "mean": sim["mean"], "sd": sim["sd"], "cov": sim["cov"],
            "report": r,
        }

    @staticmethod
    def calibrate_fatigue(season: Optional[int] = None, reps: int = 12, seed: int = 17,
                          declines: Sequence[float] = (0.0, 0.002, 0.004),
                          openings: Sequence[float] = (1.0, 1.04, 1.078),
                          workers: Optional[int] = None,
                          verbose: bool = True) -> dict:
        """Score fatigue variants on the real slate's per-inning MEAN profile.

        **The gradient**: sweeping `declines` against inning 1 shows directly what
        the play-by-play measurement said — flat fits, 0.004 does not.

        **The opening penalty, which must NOT be applied.** The same measurement
        found starters worse for the first two batters (+0.0265 RV/PA, t 3.10), a
        multiplier of 1.078 — and applying it overshoots inning 1 by nearly 3x,
        because **it is mostly the top of the batting order, not the pitcher**: bf
        1-2 is always slots 1 and 2 while bf 3-24 averages the whole lineup, and
        the measurement controlled for pitcher but not for batter. A PA simulator
        bats the real order, so it already has that lift structurally. FOURTH
        instance of the trap, after uncentred fatigue, park and platoon. The sweep
        is kept so the conclusion stays re-derivable.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rv = SlateCalibration.multiplier_run_value()
        rows = []
        for d in declines:
            rows.append(SlateCalibration._fatigue_probe(d, 1.0, 2, season, reps, seed, workers))
        for o in openings:
            if o == 1.0:
                continue
            rows.append(SlateCalibration._fatigue_probe(0.0, o, 2, season, reps, seed, workers))

        if verbose:
            u = rv["rv_per_pa_per_unit"]
            print(f"fatigue calibration, real slate {season} "
                  f"({reps} sims/game, innings 1-8)")
            print(f"  multiplier scale: 1 unit = {u:.4f} runs/PA "
                  f"({rv['runs_per_team_game_per_unit']:.2f} runs per team-game, "
                  f"{rv['pa_per_team_game']:.1f} PA)")
            print(f"  so decline 0.004/bf = {0.004 * u:+.5f} RV/batter "
                  f"against a MEASURED -0.00019 +- 0.00034")
            print(f"  and the +0.0265 RV/PA opening penalty = "
                  f"x{1 + 0.0265 / u:.3f} on bf 1-2\n")
            print(f"  {'decline':>8s} {'open':>6s} {'inn1':>7s} {'real':>7s}"
                  f" {'diff':>7s} {'lift':>7s} {'real':>7s} {'rmse':>7s}"
                  f" {'mean':>7s}")
            for r in rows:
                print(f"  {r['decline']:8.4f} {r['opening']:6.3f} "
                      f"{r['inning1_sim']:7.3f} {r['inning1_real']:7.3f} "
                      f"{r['inning1_diff']:+7.3f} {r['lift_sim']:+7.4f} "
                      f"{r['lift_real']:+7.4f} {r['profile_rmse']:7.4f} "
                      f"{r['mean']:7.3f}")
            # **Do NOT rank these on `profile_rmse`.** It is taken over all
            # eight innings and dominated by the ~0.04 level deficit in 4-8,
            # which no fatigue curve touches — it separates the variants by
            # almost nothing (0.0334 vs 0.0335) and will happily nominate a term
            # that is wrong. Fatigue controls the inning-1 LIFT; the decision
            # rests on the structural argument, not on a summary statistic.
            best = min(rows, key=lambda r: abs(r["lift_sim"] - r["lift_real"]))
            print(f"\n  closest inning-1 lift: decline {best['decline']:.4f}, "
                  f"opening x{best['opening']:.3f}")
            print(f"  shipped: FATIGUE_DECLINE_PER_BF = "
                  f"{FATIGUE_DECLINE_PER_BF}, NO opening penalty")
            print("  NOTE: rank on the LIFT column, not on rmse — rmse is taken "
                  "over all\n        eight innings and is dominated by the level "
                  "deficit in 4-8, which\n        no fatigue curve touches. It "
                  "separates these variants by ~0.3%.")
        return {"rv_scale": rv, "rows": rows}

    @staticmethod
    def _slate_form_probe(sd: float, shift: float, season: int, reps: int,
                          seed: int, workers: Optional[int] = None) -> dict:
        """The real slate at a given form draw."""
        global GAME_FORM_SD, GAME_FORM_MEAN_SHIFT
        old = (GAME_FORM_SD, GAME_FORM_MEAN_SHIFT)
        GAME_FORM_SD, GAME_FORM_MEAN_SHIFT = sd, shift
        try:
            return validate_slate_vs_reality(season, reps=reps, seed=seed,
                                             workers=workers)
        finally:
            GAME_FORM_SD, GAME_FORM_MEAN_SHIFT = old

    @staticmethod
    def calibrate_form_on_slate(season: Optional[int] = None, reps: int = 20,
                                seed: int = 17,
                                grid: Sequence[float] = (0.08, 0.12, 0.16),
                                workers: Optional[int] = None,
                                verbose: bool = True) -> dict:
        """Fit `GAME_FORM_SD` against the covariance the REAL SLATE leaves over.

        `calibrate_form` fits on league-average clones, which have no matchup
        spread, so it must be given a target with an ASSUMPTION already
        subtracted. This fits the same quadratic on the real slate, where the
        target is simply the real number. Two quantities, and the second is the
        trap:

        1. `GAME_FORM_SD` — covariance is quadratic in the tilt, so probe a grid
           and solve once rather than iterating on a noisy estimate. **Know this
           harness's noise floor before reading a verification run**: at reps=20
           anything inside ~5% of target is the estimator, not the fit. Raise
           `reps` rather than re-fitting.
        2. `GAME_FORM_MEAN_SHIFT` — measured against the sim's OWN form-off mean,
           never against the real mean: the sim is ~0.17 runs light for unrelated
           reasons, and calibrating against reality would launder that deficit
           into the noise term.

        **The two are NOT independent, and fitting them in sequence undershoots**
        — the shift lowers the run level ~1.6% and the covariance scales with the
        level squared, so the grid is probed a SECOND time with each candidate's
        own matched shift. That pass also MEASURES the tilt slope rather than
        importing `RUNS_PER_TILT`, which was fitted on clones and **does not
        reliably transfer** (7.494 against 6.513 after the §5.9 fixes).
        """
        season = CURRENT_SEASON if season is None else int(season)
        base = SlateCalibration._slate_form_probe(0.0, 0.0, season, reps, seed, workers)
        base_cov = base["sim"]["pair_cov"]
        base_mean = base["sim"]["mean"]
        target = base["real"]["pair_cov"]
        den = sum((g ** 2) ** 2 for g in grid)

        def fit(pts):
            k = sum((g ** 2) * c for g, c, _ in pts) / den if den else 0.0
            if k <= 0:
                raise RuntimeError("mlb_sim: slate form probe produced no covariance")
            return k, math.sqrt(max(target - base_cov, 0.0) / k)

        # Pass 1 — shift 0. Fixes the Jensen lift, which must be measured with
        # nothing cancelling it, and gives a first sd.
        pts1 = []
        for g in grid:
            r = SlateCalibration._slate_form_probe(g, 0.0, season, reps, seed, workers)
            pts1.append((g, r["sim"]["pair_cov"] - base_cov, r["sim"]["mean"]))
            if verbose:
                print(f"    pass 1  sd {g:.3f} shift 0.0000 -> extra pair-cov "
                      f"{pts1[-1][1]:+.5f}   mean {pts1[-1][2]:.4f}")
        lift_k = sum((g ** 2) * (mn - base_mean) for g, _, mn in pts1) / den

        # `RUNS_PER_TILT` is a GAME-TOTAL slope (14.9, so 7.45 per team-game) while
        # everything here is innings 1-8, ~90% of a game. Only used to SEED pass 2;
        # the slope is then measured.
        full = (base["game_total"]["sim_mean"] / 2.0) / base_mean if base_mean else 1.0
        slope = (RUNS_PER_TILT / 2.0) / full

        # Pass 2 — each candidate at its own matched shift.
        pts2, slopes = [], []
        for g in grid:
            s_g = max(0.0, lift_k * g ** 2 / slope) if slope else 0.0
            r = SlateCalibration._slate_form_probe(g, s_g, season, reps, seed, workers)
            pts2.append((g, r["sim"]["pair_cov"] - base_cov, r["sim"]["mean"]))
            if s_g > 0:
                mean1 = next(mn for gg, _, mn in pts1 if gg == g)
                slopes.append((mean1 - r["sim"]["mean"]) / s_g)
            if verbose:
                print(f"    pass 2  sd {g:.3f} shift {s_g:.4f} -> extra pair-cov "
                      f"{pts2[-1][1]:+.5f}   mean {pts2[-1][2]:.4f}")

        if slopes:
            slope = statistics.mean(slopes)
        k, sd = fit(pts2)
        lift = lift_k * sd ** 2                       # runs per team-game, inn 1-8
        shift = max(0.0, lift / slope) if slope else 0.0

        out = {"GAME_FORM_SD": sd, "GAME_FORM_MEAN_SHIFT": shift,
               "base_pair_cov": base_cov, "target_pair_cov": target,
               "k": k, "jensen_lift_runs": lift, "probes": pts2,
               "slope_measured": slope, "slope_from_constant": (RUNS_PER_TILT / 2.0) / full,
               "inn18_share": 1.0 / full if full else 1.0}
        if verbose:
            final = SlateCalibration._slate_form_probe(sd, shift, season, reps, seed, workers)
            out["final"] = final
            s, real = final["sim"], final["real"]
            print(f"\nform calibration on the real slate {season} "
                  f"({reps} sims/game, innings 1-8)")
            print(f"  matchup+weather+park supply  {base_cov:+.5f} pair-cov "
                  f"on their own")
            print(f"  real                         {target:+.5f}")
            print(f"  tilt slope, innings 1-8      {slope:.3f} runs/unit measured"
                  f"   ({out['slope_from_constant']:.3f} from RUNS_PER_TILT)")
            print(f"  GAME_FORM_SD                 {sd:.5f}")
            print(f"  GAME_FORM_MEAN_SHIFT         {shift:.5f}  "
                  f"(cancels a {lift:+.3f}-run Jensen lift)")
            print(f"\n  {'':10s} {'form off':>10s} {'fitted':>10s} {'real':>10s}")
            for key in ("mean", "sd", "pair_cov", "cov"):
                print(f"  {key:10s} {base['sim'][key]:10.4f} {s[key]:10.4f} "
                      f"{real[key]:10.4f}")
        return out


# The fatigue variant currently under test, as PARAMETERS rather than a
# patched function, so it can be shipped to a worker process. None outside a
# calibration run.
_fatigue_variant_args: Optional[tuple] = None


# ---------------------------------------------------------------------------
# The form draw, fitted on the REAL slate rather than on clones
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# BACKTEST — the slate replayed on AS-OF rates
# ---------------------------------------------------------------------------
# Its whole correctness rests on ONE rule: **every input used to price a game
# must predate that game.** `asof_cutoff_for` takes the latest cutoff STRICTLY
# before the game date; a cutoff equal to it would include the game itself, and
# that failure is invisible — the model simply looks good.
#
# What is still season-final is REPORTED rather than hidden: Savant OAA and
# framing, the insidethepen pen, and the fitted constants. See §3c.

# --- historic prices -------------------------------------------------------
# The right surface is the SEASON RESULTS ARCHIVE: one feed returns a page of
# finished games WITH moneylines, ~90 pages a season instead of 2,400 per-game
# resolutions. **The page embeds the feed path as `"ajaxUrl"` — extract it
# rather than constructing it**, because it carries a season token and a 41-word
# bookmaker bitmask that are not derivable. **From a US IP it returns 200 with
# ZERO rows**; the mask is not the gate, so this needs `ODDSPORTAL_PROXIES`.
# Dead routes: `participant_matches`, `search_matches`, a bare H2H url. A.17b.
ODDS_CACHE = CLV_DIR / "historic_odds_{season}.json"

# **A long scrape must be watchable WITHOUT asking whoever started it.** A
# fixed, predictable path — not a session temp file — rewritten after every
# page, so `tail -f savedata/MLBclv/progress.log` shows live progress and an
# ETA. Any long-running job in this module should write here.
PROGRESS_LOG = CLV_DIR / "progress.log"


class Archive:
    """The historic-odds archive feed and its geo session."""

    @staticmethod
    def _progress(msg: str, path: Path = PROGRESS_LOG) -> None:
        """Append one timestamped line to the watchable progress log."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write(f"{datetime.datetime.now():%H:%M:%S}  {msg}\n")
        except OSError:
            pass

    @staticmethod
    def _archive_feed_path(ajax: str, page: int) -> str:
        """The localized ajax path, page-substituted and PROXY-PREFIXED.

        **Both halves matter.** A localized path starts `/pl/ajax-...` and the
        proxy-prefix rule only matches `/ajax-`, so the locale segment hides it
        and the reply is a 187-byte `URL:` echo that decodes as "Incorrect
        padding" and reads like a rotated AES key. It has not rotated.

        **Pagination is the QUERY parameter `?page=N`, not the path.** The path's
        `/1/0/` pair looks exactly like page/offset and is silently ignored, so a
        scrape that trusts it re-downloads the first 50 games fifty times and
        looks like it worked. Use `total` and `pagination.pageCount`.
        """
        base = ajax.split("?")[0].rstrip("/")
        return "/proxy/" + base.lstrip("/") + f"/?page={page}"

    @staticmethod
    def _archive_session(season: int, proxy: Optional[str], verbose: bool,
                         tries: int = 6):
        """(client, ajaxUrl, page_url) for a geo whose archive returns rows.

        **Every network call in here is guarded.** The first version fetched the
        results page OUTSIDE the retry loop, so one read timeout on a free proxy —
        which happens constantly — killed the whole scrape with a traceback after
        the geo probe had already succeeded.
        """
        page_url = season_archive_url(season)
        for _ in range(tries):
            c, label = _archive_geo_client(proxy, verbose=verbose, season=season)
            if c is None:
                continue
            try:
                html = c._get(page_url).text
                mm = OddsPortal.AJAX_URL_RE.search(
                    html.replace('\\"', '"').replace("\\/", "/"))
                if mm:
                    return c, mm.group(1), page_url, label
                if verbose:
                    print(f"[odds] geo {label}: no ajaxUrl for {season}")
            except Exception as e:
                if verbose:
                    print(f"[odds] geo {label}: {type(e).__name__} "
                          f"fetching the {season} page")
        return None, None, page_url, None




def season_archive_url(season: int = 2025, sport: str = "baseball",
                       country: str = "usa", league: str = "mlb") -> str:
    """Results-archive path for a season.

    **The CURRENT season has no year suffix.** `/baseball/usa/mlb-2026/results/`
    returns a 254 KB page with no `ajaxUrl` in it at all — which reads exactly
    like the archive having moved rather than like the wrong URL — while
    `/baseball/usa/mlb/results/` serves the live season (token `ELceBHcR`) in
    994 KB. A finished season keeps the suffix. So this is not a cosmetic
    difference: without it there are no 2026 prices, and the CLV harness has
    nothing to score against.
    """
    if season >= datetime.date.today().year:
        return f"/{sport}/{country}/{league}/results/"
    return f"/{sport}/{country}/{league}-{season}/results/"


def _archive_geo_client(proxy: Optional[str] = None, verbose: bool = True,
                        season: int = 2025):
    """An OddsPortal client on a geo whose archive actually returns rows.

    The US geo returns an empty feed, so this tries the Webshare proxies the
    live-scores widget discovers. Returns (client, label) or (None, None).

    **Probe the SEASON being fetched.** This used to probe a hardcoded 2025
    regardless, and a geo can serve a FINISHED season while returning an empty
    feed for the live one — `gb` does exactly that. It passed the probe, served
    nothing for 2026, and because page 1 was empty the run never read the feed's
    own `total` and so reported "FINISHED ... complete" on zero new rows.
    """
    cands: List[Tuple[str, Optional[str]]] = [("direct", proxy)] if proxy else []
    if not proxy:
        try:
            sys.path.insert(0, str(_APP_ROOT))
            from live_scores_widget import _webshare_locations
            cands = [("direct", None)] + sorted(_webshare_locations().items())
        except Exception as e:
            if verbose:
                print(f"[odds] no proxy discovery ({e}); trying direct only")
            cands = [("direct", None)]
    # **Probe the FEED, not the page.** The US results page carries a perfectly
    # good `ajaxUrl` and then serves an empty feed, so a page-level check picks
    # the one geo that cannot work and reports success.
    for label, px in cands:
        try:
            c = Clv._op_client(px)
            page = season_archive_url(season)
            html = c._get(page).text
            mm = OddsPortal.AJAX_URL_RE.search(html.replace('\\"', '"').replace("\\/", "/"))
            if not mm:
                if verbose:
                    print(f"[odds] geo {label}: no ajaxUrl ({len(html)}B)")
                continue
            r = c._get(Archive._archive_feed_path(mm.group(1), 1), referer=page)
            d = c.decode_feed(r.text)
            dd = d.get("d") if isinstance(d, dict) else d
            n = len((dd or {}).get("rows") or [])
            if verbose:
                print(f"[odds] geo {label}: feed returned {n} rows")
            if n:
                return c, label
        except Exception as e:
            if verbose:
                print(f"[odds] geo {label}: {type(e).__name__} {str(e)[:60]}")
    return None, None


def fetch_historic_odds(season: int = 2025, pages: int = 60,
                        proxy: Optional[str] = None,
                        save_dir: Path = SAVE_DIR,
                        verbose: bool = True) -> Dict[str, dict]:
    """A season of finished games with per-book odds, off the results archive.

    ~50 games a page, so a season is ~48 pages. Keyed by event id, cached, and
    re-runnable — it stops at the first empty page.
    """
    path = Path(str(ODDS_CACHE).format(season=season))
    cache: Dict[str, dict] = {}
    if path.exists():
        try:
            with open(path) as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}

    c, ajax, page_url, label = Archive._archive_session(season, proxy, verbose)
    if c is None:
        if verbose:
            print(f"[odds] {season}: no geo served the archive. The US geo "
                  f"returns an empty feed; set WEBSHARE_API_KEY or pass proxy=.")
        return cache

    found = 0
    expected = None
    t0 = time.time()
    Archive._progress(f"{season}  starting (geo {label})")
    # An explicit counter, not `for page in range(...)`: an empty page 1 has to
    # RETRY page 1 on a different geo, and `continue` in a for-loop would skip
    # to page 2 and quietly lose the first fifty games.
    page, geo_swaps = 1, 0
    while page <= pages:
        # Free proxies drop mid-run, so a single failure must not end the
        # scrape — re-select a geo and retry the SAME page.
        data = None
        for attempt in range(3):
            try:
                r = c._get(Archive._archive_feed_path(ajax, page), referer=page_url)
                data = c.decode_feed(r.text)
                break
            except Exception as e:
                if verbose:
                    print(f"[odds] page {page} attempt {attempt + 1}: "
                          f"{str(e)[:60]}")
                # Re-acquire BOTH client and ajaxUrl: the path carries the
                # geo's locale segment AND its bookmaker bitmask, so reusing
                # the old one returns an EMPTY feed — indistinguishable from
                # "end of results". That silently truncated 2024 at 50 games
                # of 2,473 and reported success.
                c2, ajax2, _, lbl2 = Archive._archive_session(season, None,
                                                      verbose=False, tries=3)
                if c2 is None:
                    break
                c, ajax, label = c2, ajax2, lbl2
                if verbose:
                    print(f"[odds]   switched geo -> {label} (ajaxUrl re-read)")
        if data is None:
            if verbose:
                print(f"[odds] page {page}: giving up after retries")
            break
        d = data.get("d") if isinstance(data, dict) else data
        rows = (d or {}).get("rows") or []
        if not rows:
            # An empty page is ambiguous: past the end, or a geo/mask mismatch.
            # Believe it only if we have most of what the feed said it had.
            # **An empty PAGE 1 is never "past the end"** — and because `total`
            # is only read from page 1, the run then has no expectation to
            # compare against and reports success on zero rows. That is how a
            # backfill silently did nothing while printing "FINISHED".
            if page == 1 and geo_swaps < 4:
                geo_swaps += 1
                if verbose:
                    print(f"[odds] page 1 EMPTY on geo {label} — this geo "
                          f"cannot serve {season}; re-selecting")
                Archive._progress(f"{season}  page 1 empty on geo {label}, re-selecting")
                c2, ajax2, _, lbl2 = Archive._archive_session(season, None,
                                                      verbose=verbose, tries=3)
                if c2 is None:
                    if verbose:
                        print(f"[odds] no geo serves the {season} archive")
                    break
                c, ajax, label = c2, ajax2, lbl2
                continue                     # retry page 1, not page 2
            if verbose:
                print(f"[odds] page {page}: empty ({len(cache)} cached of "
                      f"{expected or '?'} expected)")
            break
        pag = (d or {}).get("pagination") or {}
        if page == 1:
            expected = (d or {}).get("total")
            if verbose:
                print(f"[odds] season {season}: {expected} games, "
                      f"{pag.get('pageCount')} pages")
        if pag.get("activePage") not in (None, page):
            if verbose:
                print(f"[odds] page {page}: server returned page "
                      f"{pag.get('activePage')} — pagination broke, stopping "
                      f"rather than re-caching page 1")
            break
        for row in rows:
            ev = row.get("encodeEventId") or row.get("url")
            if not ev:
                continue
            odds = row.get("odds") or []
            cache[str(ev)] = {
                "event_id": row.get("eventId"),
                "url": row.get("url"),
                "start_ts": row.get("date-start-timestamp"),
                "home": row.get("home-name"), "away": row.get("away-name"),
                "home_score": row.get("homeResult"),
                "away_score": row.get("awayResult"),
                # avgOdds per outcome, in the feed's own order
                "avg_odds": [o.get("avgOdds") for o in odds],
                "max_odds": [o.get("maxOdds") for o in odds],
                "n_books": max((o.get("cntActive") or 0) for o in odds) if odds else 0,
            }
            found += 1
        if verbose:
            print(f"[odds] page {page}: {len(rows)} games "
                  f"({len(cache)} cached)", flush=True)
        pct = (len(cache) / expected) if expected else 0.0
        rate = (time.time() - t0) / max(page, 1)
        left = max((pag.get("pageCount") or pages) - page, 0)
        Archive._progress(f"{season}  page {page}/{pag.get('pageCount') or '?'}  "
                  f"{len(cache)}/{expected or '?'} games ({pct:.0%})  "
                  f"~{left * rate / 60:.0f} min left")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(cache, fh)
        page += 1

    if verbose:
        print(f"[odds] season {season}: {found} rows this run, "
              f"{len(cache)} cached at {path}")
    # `expected` is None when page 1 never returned rows, and "no expectation"
    # must not read as "met the expectation" — that is how a backfill that did
    # nothing printed "complete".
    short = (expected is None) or len(cache) < expected * 0.98
    Archive._progress(f"{season}  FINISHED {len(cache)}/{expected or '?'} games"
              + ("  ** SHORT — re-run to backfill **" if short else "  complete"))
    return cache


# ---------------------------------------------------------------------------
# PER-EVENT odds — opening prices, TOTALS, run lines, and the period markets
# ---------------------------------------------------------------------------
# The season archive feed carries the aggregate CLOSING moneyline and nothing
# else. What it cannot give is the OPENING price and any market beyond the
# moneyline; both live on the per-EVENT endpoint, which `OddsPortalClient`
# already parses in full (including the `opened_at`/`changed_at` time axis,
# without which "opening" and "closing" are two numbers with no clock).
#
# **Three things are non-obvious and each silently returns nothing:**
#   1. **The `#encodedId` fragment is mandatory** — a bare H2H url serves the
#      LATEST meeting and answers with a full, plausible page; the tell is home
#      and away coming back SWAPPED.
#   2. **A US egress IP returns zero outcomes.** The event PAGE resolves, so it
#      looks like a parse failure rather than a geo block.
#   3. **The `/pl/` locale prefix must be stripped** — the proxy rule only
#      matches `/ajax-`, so a localized path comes back as a 187-byte `URL:`
#      echo that decodes as "Incorrect padding", reading like a rotated AES key.
#
# ~1 hour a season at 8 workers, resumable by event id. sim_state.md A.17b.
EVENT_ODDS_PATH_FMT = "MLBclv/event_odds_{season}.json"
EVENT_ODDS_MARKETS = (3, 2, 5)      # moneyline, totals, run line
EVENT_ODDS_GEOS = ("pl", "jp")      # measured: `direct`/`gb`/`es` give nothing


class EventOdds:
    """Per-event odds: opening prices, totals, run lines, period markets."""

    @staticmethod
    def event_odds_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
        return Path(save_dir) / EVENT_ODDS_PATH_FMT.format(season=season)

    @staticmethod
    def _oddsportal_geos() -> Dict[str, Optional[str]]:
        """{label: proxy} for the geos that answer. Discovered, not hardcoded —
        the free proxy IPs rotate, so a pinned list goes stale silently."""
        try:
            import live_scores_widget as _lsw
            locs = _lsw._webshare_locations() or {}
        except Exception:
            locs = {}
        out = {g: locs.get(g) for g in EVENT_ODDS_GEOS if locs.get(g)}
        if not out:
            raise RuntimeError(
                "mlb_sim: no OddsPortal proxy available. A US egress IP returns "
                "ZERO outcomes on this endpoint (the event page still resolves, so "
                "it reads like a parse failure). Set ODDSPORTAL_PROXIES or "
                "Creds.ODDSPORTAL_PROXIES.")
        return out

    @staticmethod
    def _pack_outcome(o) -> dict:
        """One outcome, compact. Short keys because this is ~64 lines x ~1,900
        games and the long-key version is several times the size for nothing."""
        d = {"n": o.name, "a": o.avg_odds, "o": o.opening_avg,
             "x": o.max_odds, "b": o.n_books}
        # the time axis: when the price was first hung and last moved. Averaged
        # across books, because per-book detail is not what a CLV study needs and
        # it is 20x the bytes.
        if o.opened_at:
            d["t0"] = int(statistics.median(o.opened_at.values()))
        if o.changed_at:
            d["t1"] = int(statistics.median(o.changed_at.values()))
        return {k: v for k, v in d.items() if v is not None}

    @staticmethod
    def _pack_event(eo) -> dict:
        lines = getattr(eo, "markets", None)
        lines = lines() if callable(lines) else lines
        out = {"home": eo.home, "away": eo.away,
               "start_ts": getattr(eo, "start_ts", None), "lines": []}
        for m in (lines or []):
            outs = [EventOdds._pack_outcome(o) for o in (m.outcomes or [])]
            if not outs:
                continue
            out["lines"].append({"bt": m.betting_type_id,
                                 "sc": getattr(m, "scope_id", 1),
                                 "h": m.handicap, "o": outs})
        return out

    @staticmethod
    def fetch_event_odds(season: Optional[int] = None, limit: Optional[int] = None,
                         workers: int = 8, timeout: float = 25.0,
                         save_dir: Path = SAVE_DIR,
                         verbose: bool = True) -> Dict[str, dict]:
        """Opening + closing prices for every market, per event. RESUMABLE.

        Reads the event list (and the `#encodedId` fragments) off the archive cache
        `fetch_historic_odds` already built, so it adds no discovery cost.

        **Reports cached-against-expected and shouts when SHORT.** On this source
        "empty" and "done" are identical — that is the sentence behind five of the
        seven bugs in section 3d — so a run that fetched nothing must not look like
        a run that finished.
        """
        season = CURRENT_SEASON if season is None else int(season)
        archive = load_historic_odds(season, save_dir)
        if not archive:
            raise FileNotFoundError(
                f"mlb_sim: no historic_odds_{season}.json — run "
                f"`fetch_historic_odds({season})` first; this reads its event ids "
                f"and #encodedId fragments.")
        geos = EventOdds._oddsportal_geos()
        proxies = [geos[g] for g in sorted(geos)]
        cache = load_event_odds(season, save_dir)
        todo = [(k, v["url"]) for k, v in archive.items()
                if v.get("url") and "#" in v["url"] and k not in cache]
        no_frag = sum(1 for v in archive.values()
                      if v.get("url") and "#" not in v["url"])
        # `is not None`, not truthiness: `--limit 0` read as "no limit" and quietly
        # started a full 1,870-event run instead of fetching nothing.
        if limit is not None:
            todo = todo[:max(0, limit)]
        if verbose:
            print(f"[eventodds] {season}: {len(archive)} games in the archive, "
                  f"{len(cache)} already cached, {len(todo)} to fetch"
                  + (f", {no_frag} have NO #encodedId and are unfetchable"
                     if no_frag else ""))
            print(f"[eventodds] geos {sorted(geos)}, {workers} workers "
                  f"(~17s/event each)")
        Archive._progress(f"eventodds {season}: {len(todo)} to fetch, {len(cache)} cached")
        if not todo:
            return cache

        path = EventOdds.event_odds_path(season, save_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        jobs = [(k, u, proxies, timeout) for k, u in todo]
        done = fail = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for ev_id, packed in ex.map(_event_odds_worker, jobs):
                if packed:
                    cache[ev_id] = packed
                    done += 1
                else:
                    fail += 1
                n = done + fail
                if n % 25 == 0 or n == len(jobs):
                    rate = (time.time() - t0) / n
                    left = (len(jobs) - n) * rate
                    msg = (f"{season}  {n}/{len(jobs)}  ok {done} fail {fail}  "
                           f"{rate:.1f}s/event  ~{left/60:.0f} min left")
                    Archive._progress(f"eventodds {msg}")
                    if verbose:
                        print(f"[eventodds] {msg}", flush=True)
                    with open(path, "w") as fh:
                        json.dump(cache, fh)
        with open(path, "w") as fh:
            json.dump(cache, fh)
        have, want = len(cache), len(archive) - no_frag
        if verbose:
            print(f"\n[eventodds] {season}: {have}/{want} events cached "
                  f"({fail} failed this run) -> {path}")
            if have < want:
                print(f"** SHORT by {want - have} — re-run to backfill. Re-runs "
                      f"merge by event id, so it is idempotent. **")
        Archive._progress(f"eventodds {season} FINISHED {have}/{want}")
        return cache

    @staticmethod
    def event_totals(packed: dict, scope: int = 1) -> Dict[float, dict]:
        """{line: {"over": {...}, "under": {...}}} from a packed event."""
        out: Dict[float, dict] = {}
        for ln in packed.get("lines", []):
            if ln.get("bt") != 2 or ln.get("sc", 1) != scope:
                continue
            h = ln.get("h")
            if h is None:
                continue
            side = {}
            for o in ln.get("o", []):
                side[str(o.get("n", "")).lower()] = o
            if "over" in side and "under" in side:
                out[float(h)] = side
        return out


def load_event_odds(season: int, save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
    """Cached per-event odds, keyed by the archive's event id. {} when absent."""
    path = EventOdds.event_odds_path(season, save_dir)
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _event_odds_worker(job):
    """One event through one geo. Returns (event_id, packed) or (id, None)."""
    import OddsPortalClient as _OP
    ev_id, url, proxies, timeout = job
    # the locale prefix has to go: the client's /proxy/ rule only matches paths
    # starting with /ajax-, so /pl/... slips past and decodes as garbage
    path = re.sub(r"^/[a-z]{2}/", "/", url)
    for proxy in proxies:
        try:
            cl = _OP.OddsPortalClient(verbose=False, timeout=timeout,
                                      proxy=proxy)
            eo = cl.get_event_odds(path, markets=EVENT_ODDS_MARKETS)
            packed = EventOdds._pack_event(eo)
            if packed["lines"]:
                return ev_id, packed
        except Exception:
            continue
    return ev_id, None


def market_total(packed: dict, scope: int = 1,
                 min_books: int = 3, price: str = "close",
                 both_ends: bool = False) -> Optional[float]:
    """The market's own expected total: the line whose over and under sit
    closest to even money.

    That line is the MEDIAN of the market's predictive distribution, not its mean
    — measured 0.40 (2025) and 0.47 (2026) BELOW the actual mean total. Comparing
    it against a model MEAN manufactures a half-run market bias that does not
    exist (the trap §8 records twice).

    `min_books` matters: the extreme lines are quoted by 2-4 books at wide
    prices, so an unfiltered "closest to even" can land on a thin outlier.
    `price` selects the end read, and `both_ends` requires the line to be priced
    at BOTH — what a CLV comparison needs. **Both default to the old behaviour on
    purpose**: every number in 3d.11 came from the closing-only form.
    """
    key = "o" if price == "open" else "a"
    best = None
    for line, side in EventOdds.event_totals(packed, scope).items():
        over, under = side["over"].get(key), side["under"].get(key)
        if not over or not under:
            continue
        if both_ends and not all(side[s].get(k) for s in ("over", "under")
                                 for k in ("o", "a")):
            continue
        if min(side["over"].get("b", 0), side["under"].get("b", 0)) < min_books:
            continue
        skew = abs(1.0 / over - 1.0 / under)
        if best is None or skew < best[0]:
            best = (skew, float(line))
    return best[1] if best else None


def score_totals_vs_market(bt: dict, season: Optional[int] = None,
                           save_dir: Path = SAVE_DIR) -> dict:
    """The model's totals against the CLOSING TOTAL, not against results.

    **This is the sharper instrument and the reason `eventodds` exists.** Against
    realised totals the ceiling is ~4-5% R2 — the closing total itself manages
    3.99% / 4.66% — so a real defect takes thousands of games to see. The market's
    line has ~2x our correlation with the outcome, which makes it a far better
    reference for the same sample.

    Both references are reported: `vs_actual` is the model against baseball,
    `vs_market` against the close, where the calibration SLOPE is the number to
    watch — 1.0 means our deviations from the league mean are the market's size.
    """
    season = int(season or bt.get("season") or 2026)
    ev = load_event_odds(season, save_dir)
    arch = load_historic_odds(season, save_dir)
    if not ev or not arch:
        return {"n": 0, "season": season,
                "why": "no event_odds / historic_odds cached"}
    # **Reuse `odds_by_game`'s key rather than re-deriving it.** It maps club
    # names through `_team_index` and applies `_ARCHIVE_LOCAL_SHIFT`; hand-
    # rolling either gives a join that matches nothing and looks like missing
    # data. Keyed by gamePk, so a DOUBLEHEADER is priced instead of dropped.
    rows = odds_by_pk(season, save_dir)
    ev_by_url = {}
    for k, a in arch.items():
        if k in ev:
            ev_by_url[a.get("url")] = ev[k]
    book: Dict[int, float] = {}
    for key, row in rows.items():
        packed = ev_by_url.get(row.get("url"))
        if packed is None:
            continue
        line = market_total(packed)
        if line is not None:
            book[key] = line
    mm: List[float] = []
    mk: List[float] = []
    at: List[float] = []
    for g in bt["games"]:
        key = g["pk"]
        if key not in book:
            continue
        mm.append(g["model_mean"])
        mk.append(book[key])
        at.append(float(g["actual_total"]))
    out = {"n": len(mm), "season": season, "matched": len(book)}
    if len(mm) < 30:
        return out

    def _slope(x, y):
        mx, my = statistics.mean(x), statistics.mean(y)
        sxx = sum((a - mx) ** 2 for a in x)
        return (sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx
                if sxx else 0.0)

    out["model_mean"] = statistics.mean(mm)
    out["market_mean"] = statistics.mean(mk)
    out["actual_mean"] = statistics.mean(at)
    out["model_sd"] = statistics.pstdev(mm)
    out["market_sd"] = statistics.pstdev(mk)
    out["vs_actual"] = {"corr": _corr(mm, at), "slope": _slope(mm, at)}
    out["market_vs_actual"] = {"corr": _corr(mk, at), "slope": _slope(mk, at)}
    # the model against the LINE. A slope of 1 says the model's spread is the
    # market's spread; the correlation says how much of it is shared.
    out["vs_market"] = {"corr": _corr(mm, mk), "slope": _slope(mm, mk)}
    # and the disagreement, which is what a totals bet is priced off. Compared
    # against the MEDIAN-vs-mean offset so it is not read as an edge.
    out["skew_offset"] = statistics.mean(at) - statistics.mean(mk)
    diff = [a - b for a, b in zip(mm, mk)]
    out["disagreement_sd"] = statistics.pstdev(diff)
    return out


def available_asof_cutoffs(season: Optional[int] = None,
                           save_dir: Path = SAVE_DIR) -> List[str]:
    """Cutoff dates with BOTH boards cached. Sorted."""
    season = CURRENT_SEASON if season is None else int(season)
    d = Path(save_dir) / "asof"
    if not d.exists():
        return []
    bat = {p.stem.rsplit("_", 1)[1] for p in d.glob(f"fg_bat_{season}_*.json")}
    pit = {p.stem.rsplit("_", 1)[1] for p in d.glob(f"fg_pit_{season}_*.json")}
    return sorted(bat & pit)


def asof_cutoff_for(game_date: str, cutoffs: Sequence[str]) -> Optional[str]:
    """The latest cutoff STRICTLY BEFORE `game_date`, or None.

    Strictly before, not on: a board cut on the game date contains the game.
    """
    earlier = [c for c in cutoffs if c < game_date]
    return max(earlier) if earlier else None


def assert_density_inputs(season: int, save_dir: Path = SAVE_DIR) -> None:
    """Refuse to run a density arm whose weather source has no pressure.

    **This silently produced a no-op arm**: `air_density` returns None without a
    pressure, so `weather_tilt` correctly falls back to the temperature term and
    the arm comes back BYTE-IDENTICAL to its control — which reads as "air density
    is worth nothing", a conclusion rather than a missing file. Graceful
    degradation is right for ONE game with no reading and wrong for a SOURCE that
    carries none, because then the fallback is the whole arm.
    """
    if not USE_AIR_DENSITY:
        return
    lag = weather_source_lag()
    if lag is None:
        raise RuntimeError(
            "mlb_sim: USE_AIR_DENSITY needs a weather source carrying pressure "
            "and humidity, and WEATHER_SOURCE is 'observed' — the StatsAPI game "
            "feed has neither. Use a forecast source.")
    fc = load_forecast_weather(season, lag, save_dir)
    have = sum(1 for v in fc.values() if v.get("pressure_hpa") is not None)
    if have < max(50, 0.5 * len(fc)):
        raise RuntimeError(
            f"mlb_sim: USE_AIR_DENSITY is on but only {have}/{len(fc)} games in "
            f"weather_forecast_{season}_d{lag}.json carry a pressure. The arm "
            f"would fall back to the temperature term on every game and come "
            f"back identical to its control. Re-run "
            f"`python mlb_sim.py forecastwx --season {season} --lag {lag}`.")


def _backtest_worker(job):
    """One CUTOFF's games, priced on that cutoff's boards. Module level.

    A cutoff is the natural unit: `build_rates_asof` is the expensive part and
    every game under one cutoff shares it. Same forkserver rule as
    `_slate_val_worker` — calibration state travels as DATA.
    """
    (cut, rows, season, reps, seed, save_dir, overrides) = job
    globals().update(overrides)
    _BOARDS.clear(); _ASOF_BOARDS.clear()
    _PRIOR_CURVE.clear(); _PRIOR_LEAGUE.clear()
    _SLATE_TABLES.clear(); _DEPLOY.clear()
    _PIT_ROWS.clear(); _BAT_ROWS.clear()
    _PLATOON_GAPS.clear(); _CHED.clear(); _MILB_THROWS.clear()
    save_dir = Path(save_dir)

    bat_t, _ = build_rates_asof("bat", season, cut, save_dir=save_dir)
    pit_t, _ = build_rates_asof("pit", season, cut, save_dir=save_dir)
    hz = starter_hazard()
    bases = slate_sides(rows, bat_t, pit_t, season, hz, save_dir)

    out: List[dict] = []
    # **Frozen at the SAME cutoff the boards are.** A daily-updating feature
    # inside a weekly-frozen backtest measures its recency, not itself.
    # `by_cutoff` lives in `backtest()`; this is a WORKER and only receives its
    # own `cut`. The cutoff SET is what freezes the feature, and it comes from
    # the same source `backtest()` groups on.
    _cq = (Pricing.club_quality_asof(season, save_dir,
                             cutoffs=available_asof_cutoffs(season, save_dir))
           if TEAM_QUALITY_GAIN else {})
    for row in rows:
        hb, ab = bases.get(row["home"]), bases.get(row["away"])
        if hb is None or ab is None:
            continue
        home, _, _ = _game_side(hb, row["home_sp"], row["home_lineup"],
                                bat_t, pit_t, season, save_dir,
                                row.get("home_catcher"))
        away, _, _ = _game_side(ab, row["away_sp"], row["away_lineup"],
                                bat_t, pit_t, season, save_dir,
                                row.get("away_catcher"))
        # Club quality, from games strictly BEFORE this one.
        home.team_quality = _cq.get((row["date"], row["home"]), 0.0)
        away.team_quality = _cq.get((row["date"], row["away"]), 0.0)
        # Seeded per GAME, not per worker, so the answer does not depend on how
        # the cutoffs happen to be distributed across processes. **The seed is
        # identical across ARMS as well**, which is what makes `RATE_MODEL` a
        # clean A/B: same games, same form draws, same hooks, same bullpen, and
        # the only difference is the nine numbers each PA is drawn from.
        res = simulate_many(home, away, n=reps,
                            seed=(seed * 1_000_003 + row["pk"]) & ((1 << 30) - 1),
                            weather=_slate_weather(row),
                            venue=resolve_venue(row["venue"]),
                            ml=game_adjuster(season, cut, row, home, away,
                                             save_dir))
        out.append({
            "pk": row["pk"], "date": row["date"], "cutoff": cut,
            "home": row["home"], "away": row["away"],
            "model_total": implied_line(res),
            "model_mean": statistics.mean(game_totals(res)),
            "p_home": p_home_win(res),
            # The FULL joint (home,away) run histogram, "h,a" -> count. The
            # margin distribution is what prices a heavy favourite, and storing
            # only `p_home` throws it away — a win probability cannot distinguish
            # a compressed mean differential from an inflated spread, and those
            # want opposite fixes. Read it back through `joint_margins`, which
            # RAISES on an arm built before this existed rather than silently
            # reporting an empty distribution (trap 9).
            "joint": _joint_runs(res),
            # Starter vs relief attribution. The engine has always tracked
            # `PitcherLine.r`; the backtest simply discarded it, the same way
            # it discarded the margin distribution before `joint`. Read the
            # attribution caveat in `_staff_split` before using it.
            **Pricing._staff_split(res, home, away),
            # Runs-per-half-inning histogram, "runs" -> count, pooled over all
            # sims. Compact, and the only thing needed to test whether the
            # engine under-clusters within an inning.
            "inn_h": Pricing._half_inning_hist(res, "home"),
            "inn_a": Pricing._half_inning_hist(res, "away"),
            "actual_total": sum(row["home_innings"]) + sum(row["away_innings"]),
            "actual_home": sum(row["home_innings"]),
            "actual_away": sum(row["away_innings"]),
            "home_won": sum(row["home_innings"]) > sum(row["away_innings"]),
        })
    return out


def backtest(season: Optional[int] = None, reps: int = 60, seed: int = 17,
             limit: Optional[int] = None,
             odds: Optional[Dict[int, dict]] = None,
             workers: Optional[int] = None,
             save_dir: Path = SAVE_DIR, verbose: bool = True) -> dict:
    """Replay the season on as-of rates. Returns per-game projections.

    `odds` is an optional {game_pk: {...}} of historic prices; without it this
    produces projections and scores them against the ACTUAL results, which is
    still a real out-of-sample test of the model — just not a CLV number.
    """
    season = CURRENT_SEASON if season is None else int(season)
    slate = season_slate(season, save_dir=save_dir)
    cutoffs = available_asof_cutoffs(season, save_dir)
    if not cutoffs:
        raise FileNotFoundError(
            "mlb_sim: no as-of boards cached. Run `mlb_sim.py asof` first.")
    if limit:
        slate = slate[:limit]

    # group by cutoff so the (expensive) rate build happens once per cutoff
    by_cutoff: Dict[str, List[dict]] = {}
    skipped = 0
    for row in slate:
        cut = asof_cutoff_for(row["date"], cutoffs)
        if cut is None:
            skipped += 1          # before the first cutoff: nothing to know yet
            continue
        by_cutoff.setdefault(cut, []).append(row)
    if verbose:
        print(f"backtest {season}: {sum(len(v) for v in by_cutoff.values())} "
              f"games across {len(by_cutoff)} cutoffs "
              f"({skipped} before the first cutoff, skipped)", flush=True)

    # Deployment and reliever traits must come from the season being REPLAYED.
    # They were pinned to 2026, so a 2025 backtest was staffed by bullpens that
    # did not exist yet. Set before `_slate_overrides` so it travels to the pool.
    global DEPLOY_SEASON
    DEPLOY_SEASON = season
    assert_density_inputs(season, save_dir)
    overrides = _slate_overrides()
    jobs = [(cut, by_cutoff[cut], season, reps, seed, str(save_dir), overrides)
            for cut in sorted(by_cutoff)]
    workers = workers or max(1, min(len(jobs), (os.cpu_count() or 4) - 2))
    out: List[dict] = []
    if workers <= 1:
        for j in jobs:
            out += _backtest_worker(j)
            if verbose:
                print(f"  {j[0]}: {len(j[1])} games", flush=True)
    else:
        with multiprocessing.Pool(workers) as pool:
            for part in pool.imap_unordered(_backtest_worker, jobs):
                out += part
                if verbose:
                    print(f"  ...{len(out)} games priced", flush=True)
    out.sort(key=lambda r: (r["date"], r["pk"]))
    for r in out:
        r["odds"] = (odds or {}).get(r["pk"])
    return {"season": season, "reps": reps, "cutoffs": sorted(by_cutoff),
            "skipped": skipped, "workers": workers, "games": out}


# ---------------------------------------------------------------------------
# The RUN-DIFFERENTIAL instrument
# ---------------------------------------------------------------------------
# **The closing TOTAL is the WRONG instrument for anything that moves the two
# clubs in opposite directions.** Every question about who wins is about
# D = H - A; the total is H + A. An error that makes the favourite too weak and
# the underdog too strong by the same amount doubles D and leaves the total
# EXACTLY unchanged — which is why a defect worth 7.7 points of win probability
# survived a library of arms all scored on the total. The moneyline is the right
# QUANTITY at the wrong RESOLUTION: one bit a game, ~9% of games lopsided.
#
# `bt=5` is the Asian handicap, quoted as a LADDER — the market's implied CDF of
# D on every game, on disk unread. The same defect reads t +3.65 there against
# the moneyline's +2.79 and the total's nothing. Monotone rungs and moneyline
# bracketing are CHECKED, not assumed (`ladder_report`).
#
# **Do not read a compression factor off a per-game ladder fit without fixing
# the RUNG SET** — wider ladders are more lopsided games, so the fit measures
# the selection: "12.7% compression, t +11" against +0.998 on common rungs.
# sim_state.md A.17b, 4f.

# Asian handicaps refund the push, so an INTEGER rung prices P(D > k | D != k)
# and only the half-integer rungs are clean points of the CDF. Baseball's
# ladder starts at +-1.0 (there is no +-0.5), so the moneyline supplies the
# m = 1 rung and the half-integers supply m >= 2 and m <= -1.
LADDER_MIN_BOOKS = 3


def handicap_ladder(season: int, price: str = "a",
                    min_books: Optional[int] = None,
                    save_dir: Path = SAVE_DIR) -> Dict[tuple, dict]:
    """{(date, home, away): {"rungs": {m: P(D>=m)}, "ml": p_home, ...}}.

    `price` is "a" (close) or "o" (open), matching `line_open_close`.

    The join runs through the ARCHIVE row rather than the packed event, so the
    date, the club abbreviations and the final score all come from the surface
    that `odds_by_game` already validates — and the score is kept on the row so
    a caller can verify the join instead of trusting it.
    """
    min_books = LADDER_MIN_BOOKS if min_books is None else int(min_books)
    events = load_event_odds(season, save_dir)
    keyed = {}
    for row in load_historic_odds(season, save_dir).values():
        ev = archive_event_id(row)
        if ev:
            keyed[ev] = row
    idx = _team_index()
    out: Dict[tuple, dict] = {}
    for ev_id, packed in events.items():
        row = keyed.get(ev_id)
        if row is None:
            continue
        h = idx.get(_norm_club(row.get("home") or ""))
        a = idx.get(_norm_club(row.get("away") or ""))
        ts = row.get("start_ts")
        if not h or not a or not ts:
            continue
        d = (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
             - _ARCHIVE_LOCAL_SHIFT).date().isoformat()
        rec = {"rungs": {}, "ml": None, "home": h["abbr"], "away": a["abbr"],
               "date": d, "actual_d": None}
        try:
            rec["actual_d"] = int(row["home_score"]) - int(row["away_score"])
        except (KeyError, TypeError, ValueError):
            pass
        ln = _event_line(packed, 3, 1)
        if ln:
            oc = line_open_close(ln, min_books)
            if oc:
                hi = _home_index(oc["names"], h["abbr"])
                if hi is not None:
                    rec["ml"] = oc["close" if price == "a" else "open"][hi]
        for line in packed.get("lines", []):
            if line.get("bt") != 5 or line.get("sc", 1) != 1:
                continue
            hh = line.get("h")
            if hh is None or len(line.get("o") or []) != 2:
                continue
            hh = float(hh)
            if abs(hh - round(hh)) < 1e-9:      # integer rung: push-conditional
                continue
            oc = line_open_close(line, min_books)
            if not oc:
                continue
            hi = _home_index(oc["names"], h["abbr"])
            if hi is None:
                continue
            # handicap hh prices home+hh vs away, so q = P(D > -hh)
            rec["rungs"][int(round(-hh + 0.5))] = (
                oc["close" if price == "a" else "open"][hi])
        if rec["rungs"] or rec["ml"] is not None:
            out[(d, h["abbr"], a["abbr"])] = rec
    return out


def ladder_report(season: int, save_dir: Path = SAVE_DIR,
                  lad: Optional[Dict[tuple, dict]] = None) -> dict:
    """Prove the ladder decode rather than trusting it.

    Monotonicity and moneyline-bracketing are the two things a sign error or a
    bad de-vig would break, and both are cheap. A silent sign flip here would
    reverse every conclusion drawn from the instrument.

    `lad` lets a caller that has ALREADY decoded the ladder hand it over.
    `handicap_ladder` is uncached and takes ~31s on a season, and the two
    invariant tests plus this function were decoding the same 2025 ladder three
    times — 95s of a 260s suite for one answer.
    """
    if lad is None:
        lad = handicap_ladder(season, save_dir=save_dir)
    mono_bad = mono_tot = brk_bad = brk_tot = 0
    for v in lad.values():
        r = sorted(v["rungs"].items())
        for (_, q1), (_, q2) in zip(r, r[1:]):
            mono_tot += 1
            if q2 > q1 + 1e-9:
                mono_bad += 1
        if v["ml"] is not None and 2 in v["rungs"] and -1 in v["rungs"]:
            brk_tot += 1
            if not (v["rungs"][2] < v["ml"] < v["rungs"][-1]):
                brk_bad += 1
    return {"games": len(lad), "monotone_pairs": mono_tot,
            "monotone_violations": mono_bad, "bracket_checked": brk_tot,
            "bracket_violations": brk_bad,
            "rung_coverage": dict(sorted(collections.Counter(
                m for v in lad.values() for m in v["rungs"]).items()))}


class Differential:
    """The run-differential instrument."""

    @staticmethod
    def differential_rows(bt: dict, season: int,
                          save_dir: Path = SAVE_DIR) -> List[dict]:
        """One row a game: the model's D distribution, the market's, the result.

        `bt` must be an arm carrying the `joint` run histogram — `joint_margins`
        raises otherwise, because an arm cached before that field existed would
        report a distribution of nothing and read as a clean null.
        """
        lad = handicap_ladder(season, save_dir=save_dir)
        out = []
        for g in bt["games"]:
            marg = joint_margins(g)
            n = sum(marg.values())
            mu = sum(d * c for d, c in marg.items()) / n
            var = sum((d - mu) ** 2 * c for d, c in marg.items()) / n
            mh = sum(int(k.split(",")[0]) * c for k, c in g["joint"].items()) / n
            ma = sum(int(k.split(",")[1]) * c for k, c in g["joint"].items()) / n
            ad = g["actual_home"] - g["actual_away"]
            row = {"pk": g["pk"], "date": g["date"], "season": season,
                   "home": g["home"], "away": g["away"],
                   "marg": marg, "n": n, "model_d": mu, "model_sd": math.sqrt(var),
                   "model_home_runs": mh, "model_away_runs": ma,
                   "p_home": g["p_home"], "actual_d": ad,
                   "actual_home": g["actual_home"], "actual_away": g["actual_away"],
                   "home_won": g["home_won"], "mkt_ml": None, "rungs": {}}
            v = lad.get((g["date"], g["home"], g["away"]))
            # Verified join: clubs play three-game series, so a date and two names
            # agreeing is not proof. The score is.
            if v is not None and v["actual_d"] == ad:
                row["mkt_ml"] = v["ml"]
                row["rungs"] = v["rungs"]
            out.append(row)
        return out

    @staticmethod
    def _slope(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float]:
        """OLS slope of y on x with its standard error."""
        xm, ym = statistics.mean(xs), statistics.mean(ys)
        sxx = sum((x - xm) ** 2 for x in xs)
        b = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / sxx
        res = [y - (ym + b * (x - xm)) for x, y in zip(xs, ys)]
        return b, math.sqrt(sum(r * r for r in res) / (len(xs) - 2) / sxx)

    @staticmethod
    def score_differential(rows: Sequence[dict]) -> dict:
        """Score an arm on the run differential — the instrument the total cannot see.

        Everything here is against ACTUAL results; the market only ever enters as
        a bucketing variable, never as ground truth.
        """
        got = {"n": len(rows)}
        got["model_mean_d"] = statistics.mean(r["model_d"] for r in rows)
        got["actual_mean_d"] = statistics.mean(r["actual_d"] for r in rows)
        got["model_sd_d"] = statistics.stdev([r["model_d"] for r in rows])
        b, se = Differential._slope([r["model_d"] for r in rows], [r["actual_d"] for r in rows])
        got["calib_slope"] = b
        got["calib_slope_se"] = se
        # conditional spread: the sim's own Var(D) against the real residual. The
        # real residual also carries the model's ERROR, so it is an UPPER bound on
        # the truth — the sim exceeding it is a contradiction, matching it is
        # already suspicious.
        got["sim_var_d"] = statistics.mean(r["model_sd"] ** 2 for r in rows)
        got["resid_var_d"] = statistics.mean(
            (r["actual_d"] - r["model_d"]) ** 2 for r in rows)
        # tail calibration of D, both directions
        tails = {}
        for m in range(-7, 9):
            ps = [sum(c for d, c in r["marg"].items() if d >= m) / r["n"]
                  for r in rows]
            ys = [1.0 if r["actual_d"] >= m else 0.0 for r in rows]
            mp = statistics.mean(ps)
            if mp < 0.01 or mp > 0.99:
                continue
            se_ = math.sqrt(sum(p * (1 - p) for p in ps)) / len(ps)
            tails[m] = {"model": mp, "actual": statistics.mean(ys),
                        "t": (statistics.mean(ys) - mp) / se_}
        got["tails"] = tails
        # favourite buckets, folded so the favourite is always the positive side
        priced = [r for r in rows if r["mkt_ml"] is not None]
        got["n_priced"] = len(priced)
        buckets = {}
        for lo, hi in ((.50, .55), (.55, .60), (.60, .65), (.65, 1.01)):
            sub = [r for r in priced if lo <= max(r["mkt_ml"], 1 - r["mkt_ml"]) < hi]
            if len(sub) < 25:
                continue
            sgn = [1 if r["mkt_ml"] >= 0.5 else -1 for r in sub]
            md = [s * r["model_d"] for s, r in zip(sgn, sub)]
            adl = [s * r["actual_d"] for s, r in zip(sgn, sub)]
            pm = [(r["p_home"] if s > 0 else 1 - r["p_home"])
                  for s, r in zip(sgn, sub)]
            won = [1.0 * ((r["home_won"]) if s > 0 else (not r["home_won"]))
                   for s, r in zip(sgn, sub)]
            sed = statistics.stdev([a - m for a, m in zip(adl, md)]) / math.sqrt(len(sub))
            sew = math.sqrt(sum(p * (1 - p) for p in pm)) / len(sub)
            buckets[f"{lo:.2f}-{hi:.2f}"] = {
                "n": len(sub), "model_d": statistics.mean(md),
                "actual_d": statistics.mean(adl),
                "gap": statistics.mean(adl) - statistics.mean(md),
                "t": (statistics.mean(adl) - statistics.mean(md)) / sed,
                "model_p": statistics.mean(pm), "actual_p": statistics.mean(won),
                "t_p": (statistics.mean(won) - statistics.mean(pm)) / sew}
        got["fav_buckets"] = buckets
        return got

    @staticmethod
    def print_differential(sc: dict, label: str = "") -> None:
        print(f"\nRUN DIFFERENTIAL{(' — ' + label) if label else ''}   "
              f"n {sc['n']} ({sc['n_priced']} priced)")
        print(f"  mean D      model {sc['model_mean_d']:+.4f}   "
              f"actual {sc['actual_mean_d']:+.4f}   "
              f"bias {sc['model_mean_d'] - sc['actual_mean_d']:+.4f}")
        print(f"  sd of model E[D] across games {sc['model_sd_d']:.4f}")
        print(f"  calibration slope actual~model {sc['calib_slope']:+.4f} "
              f"+- {sc['calib_slope_se']:.4f}  "
              f"(t vs 1 = {(sc['calib_slope'] - 1) / sc['calib_slope_se']:+.2f})")
        print(f"  sim Var(D) {sc['sim_var_d']:.3f}  vs real residual "
              f"{sc['resid_var_d']:.3f}   ratio {sc['sim_var_d'] / sc['resid_var_d']:.4f}")
        print(f"\n  {'rung':>10} {'model':>8} {'actual':>8} {'t':>7}")
        for m, v in sorted(sc["tails"].items()):
            print(f"  P(D>={m:+d}) {v['model']:8.4f} {v['actual']:8.4f} {v['t']:+7.2f}")
        print(f"\n  {'fav bucket':>12} {'n':>5} {'modelE[D]':>10} {'actual':>9} "
              f"{'gap':>8} {'t':>7} | {'modelP':>7} {'actualP':>8} {'t':>7}")
        for k, v in sc["fav_buckets"].items():
            print(f"  {k:>12} {v['n']:5d} {v['model_d']:+10.3f} {v['actual_d']:+9.3f} "
                  f"{v['gap']:+8.3f} {v['t']:+7.2f} | {v['model_p']:7.4f} "
                  f"{v['actual_p']:8.4f} {v['t_p']:+7.2f}")


def score_backtest(bt: dict) -> dict:
    """Level, correlation and win-rate calibration for a backtest run.

    **The LEVEL must be scored on the model's MEAN and never on its implied
    line.** Runs per game are right-skewed by about +0.58 here, so the total where
    P(over) = 0.5 sits that far below the mean, and comparing it with an actual
    MEAN manufactures a level bias of exactly the skew: it reported -0.58 while
    the mean was -0.004, and the per-cutoff profile still looked like a real
    defect because the skew is roughly constant.

    Both are kept because they answer different questions — `total_bias` is the
    model against baseball, `line_bias` the model against a BOOK, whose total is
    itself a median. That distinction has now cost two diagnostic passes.
    """
    g = bt["games"]
    if len(g) < 3:
        return {"n": len(g)}
    mm = [x["model_mean"] for x in g]
    mt = [x["model_total"] for x in g]
    at = [float(x["actual_total"]) for x in g]
    won = [1.0 if x["home_won"] else 0.0 for x in g]
    ph = [x["p_home"] for x in g]
    return {
        "n": len(g),
        "model_mean_total": statistics.mean(mm),
        "model_implied_line": statistics.mean(mt),
        "skew": statistics.mean(mm) - statistics.mean(mt),
        "actual_mean_total": statistics.mean(at),
        "total_bias": statistics.mean(mm) - statistics.mean(at),
        "line_bias": statistics.mean(mt) - statistics.mean(at),
        "total_corr": _corr(mm, at),
        "total_rmse": statistics.mean((a - b) ** 2
                                      for a, b in zip(mm, at)) ** 0.5,
        "model_home_win": statistics.mean(ph),
        "actual_home_win": statistics.mean(won),
        "ml_bias": statistics.mean(ph) - statistics.mean(won),
        "ml_corr": _corr(ph, won),
    }


# ---------------------------------------------------------------------------
# The model against the CLOSING line — sim_state.md 3d
# ---------------------------------------------------------------------------
# The backtest scores against RESULTS, which proves the model is not biased but
# says nothing about edge. This scores it against the CLOSING price, de-vigged,
# on games the model never saw.
#
# **Read the model's BIAS before its edge.** A model half a run high takes the
# over in three games of four and reports the bias as edge. The moneyline
# equivalent is a standing home/away tilt — `score_backtest`'s `ml_bias`.

def load_historic_odds(season: int, save_dir: Path = SAVE_DIR
                       ) -> Dict[str, dict]:
    path = Path(str(ODDS_CACHE).format(season=season))
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# `start_ts` is UTC and StatsAPI's `officialDate` is the BALLPARK's local date.
# Shifting back 8 hours recovers the local day for every MLB start time without
# a timezone per park.
#
# **Do NOT index both candidate dates instead.** Clubs play three- and four-game
# SERIES, so the neighbouring day is usually the same two teams — a two-date
# index silently attaches Wednesday's closing price to Tuesday's game. It showed
# as consecutive dates carrying identical odds, which is the only reason it was
# caught.
_ARCHIVE_LOCAL_SHIFT = datetime.timedelta(hours=8)


def _archive_groups(season: int, save_dir: Path = SAVE_DIR
                    ) -> Dict[tuple, List[dict]]:
    """{(local date, home abbr, away abbr): [archive rows]} for a season.

    A key with two rows is a doubleheader. `odds_by_game` drops those because
    its triple cannot tell the games apart; `odds_by_pk` resolves them against
    the slate. Both built this identical grouping, including the
    `_ARCHIVE_LOCAL_SHIFT` correction that turns the archive's UTC timestamp
    into the game's LOCAL date.
    """
    idx = _team_index()
    groups: Dict[tuple, List[dict]] = {}
    for row in load_historic_odds(season, save_dir).values():
        h = idx.get(_norm_club(row.get("home") or ""))
        a = idx.get(_norm_club(row.get("away") or ""))
        ts = row.get("start_ts")
        if not h or not a or not ts:
            continue
        d = (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
             - _ARCHIVE_LOCAL_SHIFT).date()
        groups.setdefault((d.isoformat(), h["abbr"], a["abbr"]), []).append(row)
    return groups


def odds_by_game(season: int, save_dir: Path = SAVE_DIR
                 ) -> Dict[tuple, dict]:
    """{(date, home_abbr, away_abbr): odds row} from the results archive.

    Ambiguous keys are DROPPED rather than resolved: a repeated key is a
    doubleheader and this TRIPLE cannot name game one from game two. That is a
    limit of the key, not of the data — **`odds_by_pk` resolves them** on the
    final scores, which is what every consumer now joins on. This stays because
    the triple is the natural identity where there is no gamePk, and dropping is
    the right answer for a key that genuinely cannot tell the two apart.
    """
    groups = _archive_groups(season, save_dir)
    return {k: v[0] for k, v in groups.items() if len(v) == 1}


def odds_by_pk(season: int, save_dir: Path = SAVE_DIR) -> Dict[int, dict]:
    """{StatsAPI gamePk: odds row} — doubleheaders INCLUDED.

    `odds_by_game`'s triple cannot name game one from game two, so it drops both.
    The rows themselves can: every doubleheader pair carries two distinct final
    scores. Pairing on the SCORE recovered 36 games in 2026 and 56 in 2025.

    **Pair on the score, NOT on the clock, even though both sides have a
    timestamp.** The archive gives game two a real ~3-6h offset while StatsAPI
    schedules it as a PLACEHOLDER five minutes after game one, so nearest-time
    matching maps BOTH slate rows onto archive game one — the exact error
    dropping them was meant to avoid. Start time survives only as ORDER, used
    when two games of a pair ended in the same score. A pair that resolves
    neither way is still dropped.
    """
    arch = _archive_groups(season, save_dir)

    slate: Dict[tuple, List[dict]] = {}
    for r in season_slate(season, save_dir=save_dir):
        slate.setdefault((r["date"], r["home"], r["away"]), []).append(r)

    def _archive_score(r: dict) -> Optional[Tuple[int, int]]:
        h = str(r.get("home_score") or "").strip()
        a = str(r.get("away_score") or "").strip()
        return (int(h), int(a)) if h.isdigit() and a.isdigit() else None

    def _slate_score(r: dict) -> Optional[Tuple[int, int]]:
        hi, ai = r.get("home_innings"), r.get("away_innings")
        return (sum(hi), sum(ai)) if hi and ai else None

    out: Dict[int, dict] = {}
    for key, rows in arch.items():
        games = slate.get(key) or []
        if not games or len(rows) != len(games):
            continue
        if len(rows) == 1:
            out[games[0]["pk"]] = rows[0]
            continue
        av = [_archive_score(r) for r in rows]
        sv = [_slate_score(g) for g in games]
        if (all(x is not None for x in av) and all(x is not None for x in sv)
                and len(set(av)) == len(av) and len(set(sv)) == len(sv)
                and sorted(av) == sorted(sv)):
            by_score = {sc: r for sc, r in zip(av, rows)}
            for g, sc in zip(games, sv):
                out[g["pk"]] = by_score[sc]
            continue
        # Same score in both games of a pair, or a missing one. Order is all
        # that is left, and it is only ever a tie-break — see the docstring.
        for r, g in zip(sorted(rows, key=lambda r: r["start_ts"]),
                        sorted(games, key=lambda g: g["start"])):
            out[g["pk"]] = r
    return out


def odds_join_report(season: int, save_dir: Path = SAVE_DIR) -> dict:
    """How much of the slate the closing-odds join actually covers.

    A join is a place where a silent 40% loss looks exactly like a small
    sample, so this is printed rather than inferred.
    """
    raw = load_historic_odds(season, save_dir)
    book = odds_by_game(season, save_dir)
    by_pk = odds_by_pk(season, save_dir)
    slate = season_slate(season, save_dir=save_dir)
    seen = collections.Counter((r["date"], r["home"], r["away"])
                               for r in slate)
    matched = sum(1 for k, c in seen.items() if c == 1 and k in book)
    return {"archive_rows": len(raw), "unique_keys": len(book),
            "slate": len(slate),
            "slate_doubleheaders": sum(c for c in seen.values() if c > 1),
            "matched": matched,
            # what the consumers actually join on now; the gap between the two
            # is the doubleheaders `odds_by_game`'s triple cannot name
            "matched_by_pk": len(by_pk)}


class ClosingScore:
    """The model against the de-vigged CLOSING line."""

    @staticmethod
    def clv_vs_closing(bt: dict, season: Optional[int] = None, edge: float = 0.03,
                       price: str = "avg", save_dir: Path = SAVE_DIR) -> dict:
        """Score a backtest's moneylines against the de-vigged CLOSING price.

        `edge` is the probability difference that counts as a signal; `price` is
        `avg` (a fair-value test) or `max` (what could actually be bet).

        **Both the FILTERED and the ALL buckets are reported**, because a filter
        that selects nothing is the null result and looks identical to a filter
        that selects badly unless the unfiltered number is beside it.
        """
        season = CURRENT_SEASON if season is None else int(season)
        book = odds_by_pk(season, save_dir)
        rows: List[dict] = []
        matched = 0
        mismatched = 0
        for g in bt["games"]:
            row = book.get(g["pk"])
            if row is None:
                continue
            decs = row.get(f"{price}_odds") or []
            if len(decs) != 2 or not all(decs):
                continue
            p = Pricing.devig(decs)
            if p[0] is None:
                continue
            # **The archive carries its own final score, so the join can be
            # VERIFIED rather than trusted.** Team names and a date agreeing is
            # not proof: clubs play three-game series, so an off-by-one-day join
            # matches on both and attaches the neighbouring game's price. Scores
            # agreeing is proof, and it costs one comparison.
            try:
                if (int(row["home_score"]), int(row["away_score"])) != (
                        g["actual_home"], g["actual_away"]):
                    mismatched += 1
                    continue
            except (KeyError, TypeError, ValueError):
                pass
            matched += 1
            mkt_home = p[0]                      # outcome 1 is HOME — validated
            d = g["p_home"] - mkt_home
            side = "home" if d > 0 else "away"
            rows.append({
                "pk": g["pk"], "date": g["date"],
                "home": g["home"], "away": g["away"],
                "model_home": g["p_home"], "mkt_home": mkt_home,
                "edge": abs(d), "side": side,
                "dec": decs[0] if side == "home" else decs[1],
                "mkt_fair": mkt_home if side == "home" else 1.0 - mkt_home,
                "won": (g["home_won"] if side == "home" else not g["home_won"]),
                "n_books": row.get("n_books"),
            })
        # **The edge filter is applied to a Monte Carlo ESTIMATE of p_home, so
        # the rep count decides what it selects.** At 40 sims the standard error
        # on a near-even probability is 0.079, more than twice the 0.03
        # threshold — so the bucket is mostly games where the SIMULATOR got
        # lucky. Selection on a noisy score then regresses, diluting ROI toward
        # zero: the failure is quiet and it points the wrong way.
        reps = max(int(bt.get("reps") or 1), 1)
        mc_se = (0.25 / reps) ** 0.5
        return {"season": season, "edge": edge, "price": price,
                "matched": matched, "mismatched": mismatched,
                "reps": reps, "mc_se": mc_se,
                "n_games": len(bt["games"]),
                "picks": rows,
                "filtered": ClosingScore.summarize_clv_bucket([r for r in rows
                                                  if r["edge"] > edge]),
                "all": ClosingScore.summarize_clv_bucket(rows),
                "buckets": AbHarness.clv_edge_buckets(rows)}

    @staticmethod
    def summarize_clv_bucket(rows: Sequence[dict]) -> dict:
        """Hit rate and ROI for one set of picks, with a standard error.

        The se is what stops a 4% ROI on 90 picks from being read as an edge: at
        even money it is ~10 points, so anything inside ~2 se is noise. Every
        published MLB edge that turned out to be nothing looked like this first.
        """
        n = len(rows)
        if not n:
            return {"n": 0}
        won = [1.0 if r["won"] else 0.0 for r in rows]
        ret = [(r["dec"] - 1.0) if r["won"] else -1.0 for r in rows]
        mkt = [1.0 / r["dec"] for r in rows]          # WITH vig, the bettable price
        devigged = [r["mkt_fair"] for r in rows]      # the market's actual opinion
        fair = [(r["model_home"] if r["side"] == "home"
                 else 1.0 - r["model_home"]) for r in rows]
        roi = statistics.mean(ret)
        se = (statistics.pstdev(ret) / n ** 0.5) if n > 1 else float("nan")
        return {
            "n": n,
            "hit": statistics.mean(won),
            "mkt_implied": statistics.mean(mkt),
            "mkt_fair": statistics.mean(devigged),
            "model_implied": statistics.mean(fair),
            "roi": roi, "roi_se": se, "t": (roi / se) if se else float("nan"),
            "avg_dec": statistics.mean(r["dec"] for r in rows),
        }


# ---------------------------------------------------------------------------
# The model against the OPENING line — CLV. sim_state.md 0.
# ---------------------------------------------------------------------------
# Beating the CLOSE is the bar; CLV asks the more SENSITIVE question — priced
# before the market finished forming its opinion, did the price move TOWARD the
# model? It needs no result, so it converges in a season rather than a decade.
#
# **Four ways this can fake a positive, each handled explicitly:**
#   1. **The overround shrinks as a game approaches**, so differencing raw
#      implied probabilities adds a constant to every pick whichever side was
#      taken. `line_open_close` de-vigs BOTH ends; `vig_report` keeps the raw
#      pair so the de-vig is DEMONSTRATED rather than asserted.
#   2. **A line priced at only one end cannot be differenced** — `min_books` is
#      required at the open AND the close.
#   3. **Pairing by teams and date mis-attributes a doubleheader** — `odds_by_pk`
#      pairs on the FINAL SCORE, and this loop re-verifies it.
#   4. **An "opening" price hung AFTER our own board cutoff is a market that
#      already knows what we know** — `_open_after_cutoff` measures that share
#      rather than arguing about it; it decides how the result should be READ.

MIN_BOOKS_FOR_CLV_OPEN = 3


def _event_line(packed: dict, bt_id: int, scope: int = 1,
                handicap: Optional[float] = None) -> Optional[dict]:
    """The first two-way line of one market/scope in a packed event."""
    for ln in packed.get("lines", []):
        if ln.get("bt") != bt_id or ln.get("sc", 1) != scope:
            continue
        if handicap is not None:
            h = ln.get("h")
            if h is None or abs(float(h) - float(handicap)) > 1e-9:
                continue
        if len(ln.get("o") or []) == 2:
            return ln
    return None


def line_open_close(ln: Optional[dict],
                    min_books: Optional[int] = None) -> Optional[dict]:
    """De-vigged OPEN and CLOSE probabilities for a two-way line.

    Both ends de-vigged, which is the whole point — see this section's header.
    Returns None unless both ends are fully priced by `min_books` books.
    """
    min_books = MIN_BOOKS_FOR_CLV_OPEN if min_books is None else int(min_books)
    if not ln:
        return None
    outs = ln["o"]
    if min(o.get("b", 0) for o in outs) < min_books:
        return None
    od = [o.get("o") for o in outs]
    cd = [o.get("a") for o in outs]
    if not all(od) or not all(cd):
        return None
    op, cp = Pricing.devig(od), Pricing.devig(cd)
    if not all(x is not None for x in op) or not all(x is not None for x in cp):
        return None
    t0 = [o.get("t0") for o in outs if o.get("t0")]
    return {"names": [o.get("n") for o in outs],
            "open": op, "close": cp, "open_dec": od, "close_dec": cd,
            # RAW implied probabilities and their overrounds, kept so the
            # de-vig can be DEMONSTRATED rather than asserted — see
            # `vig_report`. They are not used for any headline number.
            "open_raw": [1.0 / d for d in od],
            "close_raw": [1.0 / d for d in cd],
            "open_ov": sum(1.0 / d for d in od),
            "close_ov": sum(1.0 / d for d in cd),
            "t0": min(t0) if t0 else None,
            "n_books": min(o.get("b", 0) for o in outs)}


def archive_event_id(row: dict) -> Optional[str]:
    """The per-event odds key for an archive row.

    **The archive stores this as the dict KEY, and `row["event_id"]` is None**
    — the id only survives inside the url's `#encodedId` fragment, which
    `fetch_event_odds` reads for exactly this reason. Taking the field looked
    right, joined nothing, and reported it as "no per-event odds for any game",
    which is the same shape as the data simply being absent. Both forms are
    accepted here so neither can go quiet.
    """
    got = row.get("event_id")
    if got:
        return str(got)
    url = row.get("url") or ""
    return url.rsplit("#", 1)[-1] if "#" in url else None


def _home_index(names: Sequence[str], home_abbr: str) -> Optional[int]:
    """Which outcome is the home side, by NAME rather than by position.

    The archive's outcome-1 is the home side and that is validated (3d), but a
    packed event is a different surface and its order is not something this
    module has verified — so it is resolved through the club index and the
    unresolvable ones are dropped and counted, never assumed.
    """
    idx = _team_index()
    for i, n in enumerate(names):
        got = idx.get(_norm_club(n or ""))
        if got and got["abbr"] == home_abbr:
            return i
    return None


class OpeningClv:
    """The model against the OPENING line — closing line value."""

    @staticmethod
    def _clv_summary(rows: Sequence[dict], key: str = "clv") -> dict:
        """Mean CLV with a standard error, and the share that moved our way.

        The se is the point. A 0.4-point mean CLV on 1,800 picks is a finding; the
        same number on 90 is not, and they read identically without it.
        """
        n = len(rows)
        if not n:
            return {"n": 0}
        v = [r[key] for r in rows]
        mu = statistics.mean(v)
        se = (statistics.pstdev(v) / n ** 0.5) if n > 1 else float("nan")
        return {"n": n, "clv": mu, "se": se,
                "t": (mu / se) if se else float("nan"),
                "hit": statistics.mean(1.0 if x > 0 else 0.0 for x in v),
                "moved": statistics.mean(0.0 if x == 0 else 1.0 for x in v)}

    @staticmethod
    def _open_after_cutoff(rows: Sequence[dict]) -> Optional[float]:
        """Share of picks whose OPENING price was hung after our board cutoff.

        This is the one way the number below could flatter the model without any
        bug: a line hung after the cutoff is a market that already knows everything
        we know, and one hung before it is a market that does not. It does not
        invalidate CLV either way — it decides how the result should be READ — so
        it is measured rather than argued about.
        """
        got = [r for r in rows if r.get("open_ts") and r.get("cutoff")]
        if not got:
            return None
        n = 0
        for r in got:
            try:
                cut = datetime.date.fromisoformat(r["cutoff"])
            except (TypeError, ValueError):
                continue
            opened = datetime.datetime.fromtimestamp(
                r["open_ts"], datetime.timezone.utc).date()
            if opened > cut:
                n += 1
        return n / len(got)

    @staticmethod
    def vig_report(rows: Sequence[dict]) -> dict:
        """What the de-vig is worth, measured rather than asserted.

        A market's overround shrinks between open and close, so RAW implied
        probabilities rise on BOTH sides. `clv_raw` is CLV computed on those raw
        numbers — the result this test would have reported without the correction.
        If it is materially above the de-vigged figure, the correction is carrying
        that drift and reporting the raw one would have been a fake positive on
        every pick regardless of which side was backed.
        """
        got = [r for r in rows if r.get("clv_raw") is not None]
        if not got:
            return {"n": 0}
        out = {"n": len(got),
               "open_overround": statistics.mean(r["open_ov"] for r in got),
               "close_overround": statistics.mean(r["close_ov"] for r in got)}
        out["raw"] = OpeningClv._clv_summary(got, "clv_raw")
        out["devigged"] = OpeningClv._clv_summary(got, "clv")
        return out

    @staticmethod
    def clv_open_buckets(rows: Sequence[dict],
                         edges: Sequence[float] = (0.0, 0.02, 0.04, 0.06, 0.09)
                         ) -> List[dict]:
        """CLV by how far the model disagreed with the OPEN.

        The SHAPE is the finding, not any single bucket: if the model carries real
        information, the games it disagreed with most should be the ones the market
        moved furthest toward. A flat profile is noise however good the top bucket
        looks — the same reading that killed the edge curve in 3d.1.
        """
        out = []
        for i, lo in enumerate(edges):
            hi = edges[i + 1] if i + 1 < len(edges) else float("inf")
            got = [r for r in rows if lo <= r["edge"] < hi]
            s = OpeningClv._clv_summary(got)
            s["lo"], s["hi"] = lo, hi
            out.append(s)
        return out


def clv_vs_opening(bt: dict, season: Optional[int] = None,
                   min_books: Optional[int] = None,
                   save_dir: Path = SAVE_DIR) -> dict:
    """Score a backtest against the market's OPEN -> CLOSE movement.

    The model is compared to the de-vigged OPENING price, the side it prefers is
    backed, and CLV is how far the de-vigged CLOSE moved toward that side —
    moneyline in probability, totals in both probability and RUNS. Positive CLV
    means the market ended up agreeing with the model more than it did at the
    open, which is evidence of information the opening price did not have, and it
    needs no game result.
    """
    min_books = MIN_BOOKS_FOR_CLV_OPEN if min_books is None else int(min_books)
    season = CURRENT_SEASON if season is None else int(season)
    events = load_event_odds(season, save_dir)
    book = odds_by_pk(season, save_dir)
    if not events:
        raise FileNotFoundError(
            f"mlb_sim: no event_odds_{season}.json — run "
            f"`python mlb_sim.py eventodds --season {season}` first. The "
            f"season results archive carries the CLOSING moneyline only, so "
            f"there is no opening price to score against without it.")

    ml_rows: List[dict] = []
    tot_rows: List[dict] = []
    matched = mismatched = no_event = no_home = 0
    lags: List[float] = []

    for g in bt["games"]:
        row = book.get(g["pk"])
        if row is None:
            continue
        # Verified by SCORE, not by teams and a date (see the header).
        try:
            if (int(row["home_score"]), int(row["away_score"])) != (
                    g["actual_home"], g["actual_away"]):
                mismatched += 1
                continue
        except (KeyError, TypeError, ValueError):
            pass
        packed = events.get(archive_event_id(row) or "")
        if not packed:
            no_event += 1
            continue
        matched += 1

        ml = line_open_close(_event_line(packed, 3, 1), min_books)
        if ml:
            hi = _home_index(ml["names"], g["home"])
            if hi is None:
                no_home += 1
            else:
                open_home = ml["open"][hi]
                close_home = ml["close"][hi]
                d = g["p_home"] - open_home
                side = "home" if d > 0 else "away"
                s = hi if side == "home" else 1 - hi
                if ml["t0"]:
                    # how many days BEFORE first pitch the price was hung
                    lags.append((row["start_ts"] - ml["t0"]) / 86400.0)
                ml_rows.append({
                    "pk": g["pk"], "date": g["date"], "cutoff": g.get("cutoff"),
                    "home": g["home"], "away": g["away"], "side": side,
                    "model_home": g["p_home"], "open_home": open_home,
                    "close_home": close_home,
                    "edge": abs(d),
                    "clv": ml["close"][s] - ml["open"][s],
                    # the same difference on RAW implied probabilities, kept
                    # only so `vig_report` can show what the de-vig removed
                    "clv_raw": ml["close_raw"][s] - ml["open_raw"][s],
                    "open_ov": ml["open_ov"], "close_ov": ml["close_ov"],
                    "open_ts": ml["t0"], "n_books": ml["n_books"],
                })

        open_line = market_total(packed, 1, min_books, "open", both_ends=True)
        close_line = market_total(packed, 1, min_books, "close", both_ends=True)
        if open_line is not None and close_line is not None:
            # Price-space CLV at the line the market OPENED at, so the model's
            # side and the price it moved to are read at the same number.
            tl = line_open_close(_event_line(packed, 2, 1, open_line), min_books)
            d = g["model_total"] - open_line
            side = "over" if d > 0 else "under"
            clv_p = clv_raw = None
            ov = (None, None)
            if tl:
                names = [str(x or "").lower() for x in tl["names"]]
                if side in names:
                    s = names.index(side)
                    clv_p = tl["close"][s] - tl["open"][s]
                    clv_raw = tl["close_raw"][s] - tl["open_raw"][s]
                    ov = (tl["open_ov"], tl["close_ov"])
            tot_rows.append({
                "pk": g["pk"], "date": g["date"], "cutoff": g.get("cutoff"),
                "home": g["home"], "away": g["away"], "side": side,
                "model_total": g["model_total"],
                "open_line": open_line, "close_line": close_line,
                "edge": abs(d),
                # RUNS the line moved toward the model's side. The line is a
                # median and so is `model_total`, which is what makes these
                # comparable at all (section 8's mean-vs-line trap).
                "clv_runs": (close_line - open_line) * (1.0 if d > 0 else -1.0),
                "clv": clv_p if clv_p is not None else 0.0,
                "clv_raw": clv_raw,
                "open_ov": ov[0], "close_ov": ov[1],
                "priced": clv_p is not None,
            })

    return {
        "season": season, "matched": matched, "mismatched": mismatched,
        "no_event": no_event, "no_home_side": no_home,
        "n_games": len(bt["games"]),
        "open_lag_days": (statistics.median(lags) if lags else None),
        "open_after_cutoff": OpeningClv._open_after_cutoff(ml_rows),
        "moneyline": ml_rows, "totals": tot_rows,
    }


# ---------------------------------------------------------------------------
# The A/B HARNESS — one rate-layer change against the closing line
# ---------------------------------------------------------------------------
# **This lived in throwaway scripts and produced every headline number in §3d** —
# a harness cited by results and not present in the code. Three properties make
# its output mean anything, each pinned by a test: the arms must actually DIFFER
# (two byte-identical result blocks are a variant compared against itself, not a
# null), the comparison is PAIRED on the games present in every arm with
# identical seeds, and every arm runs the leak-free configuration.

AB_DIR = SAVE_DIR / "ab"

# (name, {module constant: value}). The empty dict is "as shipped", and `base`
# is the incumbent — named first and named explicitly, because 3d.6 measured
# against strawmen and lost 12 points of apparent win when the real incumbent
# was named.
# `base` is the SHIPPED model and the pairing reference; every other arm is ONE
# change against it. Keeping a decided change in here as a permanent arm just
# re-measures it — the stuff prior lived here while it was a candidate and came
# out when it shipped (§3d.8).
AB_ARMS: Dict[str, Dict[str, object]] = {
    "base": {},
    # The 3-season park window. One season is mostly noise (§8), so averaging
    # is arithmetic rather than a fit — the slope of the target season's factor
    # on the window mean nearly DOUBLES from w=1 to w=3, which is where both
    # target seasons agree; w=4 splits between them. It is also what Savant
    # publishes.
    # The arsenal stuff prior, ISOLATED at its shipped configuration. §3d.8's
    # +1.14 was measured by `ab_ars.py`, which set STUFF_RELIABILITY at RUNTIME
    # while `stuff_stabilize` captured it as a frozen default — so that run used
    # arsenal FEATURES against five-column RELIABILITIES. A hybrid, not what
    # ships.
    "nostuff": {"USE_STUFF_PRIOR": False, "USE_CHED_PRIOR": False},
    # CHED against the incumbent stuff prior, and against neither.
    "stuffprior": {"USE_STUFF_PRIOR": True, "USE_CHED_PRIOR": False},
    "ched": {"USE_CHED_PRIOR": True, "USE_STUFF_PRIOR": False},
    "ched-full": {"USE_CHED_PRIOR": True, "USE_STUFF_PRIOR": False,
                  "CHED_PRIOR_SCALE": 1.0},
    # --- BMIELKE, the gated thin-sample contact prior (17e) ---------------
    # The metric applied where it is validated: hitters between
    # `BMIELKE_MIN_BBE` (25) and `BMIELKE_MAX_BBE` (175) balls in play, level
    # from BMIELKE and shape from §17d's contact map. §3d.6 ran the same metric
    # UNGATED as a single proportional multiplier and lost the moneyline at
    # paired t -2.62; this arm differs from that one in both respects.
    #
    # **Read on `mlb_sim.py diff`, not on the total.** §3d.6 IMPROVED totals
    # (+0.1527 -> +0.1582) while costing the moneyline, because a total is the
    # SUM of two offences and a moneyline their DIFFERENCE — so the total is
    # exactly the instrument that cannot see this term's known failure mode.
    # **`bmielke` is now the SHIPPED state and overrides nothing** — kept so the
    # cached `bt*_bmielke_2000.json` from the 2026-08-27 ladder run still
    # resolves. `nobmielke` is the ablation, and `snap0827` is the run made
    # immediately before the flag flipped, i.e. the same thing on that day.
    "bmielke": {},
    "nobmielke": {"USE_BMIELKE_PRIOR": False},
    # The level at face value. 0.83 is the MINIMUM of two seasons' measured
    # forecast attenuations (0.918 and 0.830); this brackets what the
    # conservative choice costs.
    "bmielke-full": {"USE_BMIELKE_PRIOR": True, "BMIELKE_PRIOR_SCALE": 1.0},
    # **The two halves of the fix, separated**, because §3d.6 got two things
    # wrong at once and an arm that changes both cannot say which mattered.
    # `bmielke-ungated` restores the every-hitter application at today's
    # shape; `bmielke-flat` restores the proportional shape at today's gate.
    # If neither loses, the §3d.6 diagnosis was wrong and that is worth knowing.
    "bmielke-ungated": {"USE_BMIELKE_PRIOR": True, "BMIELKE_GATE_BBE": 100000},
    "bmielke-flat": {"USE_BMIELKE_PRIOR": True, "CONTACT_SHRINK_BBE": 1e9},
    # The gate one step either side of the shipped 100. See BMIELKE_GATE_BBE
    # for the five-value sweep both of these bracket.
    "bmielke80": {"USE_BMIELKE_PRIOR": True, "BMIELKE_GATE_BBE": 80},
    # 175 — the METRIC's crossover against xwOBAcon, which is where this shipped
    # before 2026-08-27 and which measured WORST of five values tested in both
    # seasons on both measures. Kept as the bracket.
    "bmielke175": {"USE_BMIELKE_PRIOR": True, "BMIELKE_GATE_BBE": 175},
    # The SHAPE at the OLD shrinkage. 600 now ships (see `CONTACT_SHRINK_BBE`);
    # this is 120, the value §5 flagged as ~5x too small, kept as the bracket so
    # the change is priced rather than asserted.
    "bmielke-shrink120": {"USE_BMIELKE_PRIOR": True,
                          "CONTACT_SHRINK_BBE": 120.0},
    # BMIELKE against the Triple-A ladder it hands off to. The two cover
    # adjacent regimes by design (`milb_prior` under 25 balls in play, BMIELKE
    # from 25 to the gate), so this prices the HANDOFF: if they are fighting
    # than composing it shows up here and nowhere else.
    "bmielke-noaaa": {"USE_BMIELKE_PRIOR": True, "USE_MILB_PRIOR": False},
    # The LEAGUE anchor — BMIELKE overriding the prior underneath instead of
    # refining it. This is what shipped first and what let a neutral reading
    # pull a marked-down callup up to the gated average.
    "bmielke-lgbase": {"USE_BMIELKE_PRIOR": True,
                       "BMIELKE_LEVEL_BASE": "league"},
    # Anchored AFTER the shape step, so the level multiplies the contact map's
    # own read of his quality rather than the level he inherited.
    "bmielke-shaped": {"USE_BMIELKE_PRIOR": True,
                       "BMIELKE_LEVEL_BASE": "shaped"},
    # §3d.7's contact map on its OWN — the SHAPE with no BMIELKE level, at the
    # shipped shrinkage. The control that says how much of any `bmielke` result
    # is the LEVEL rather than the re-shaping. On prediction the split is
    # unambiguous: shape alone moves run-value correlation +0.4496 -> +0.4592
    # (2025) and the level takes it to +0.4730.
    "contactmap": {"USE_CONTACT_PRIOR": True},
    # --- the PITCHER playing-time prior, CENTRED (5.21) -------------------
    # The shipped prior's PA-weighted target sits -0.4115 runs/team-game off
    # league in April and -0.0197 in August; `PIT_PRIOR_CENTRED` solves the
    # same tilt the hitter side has always solved. Expect a LEVEL move (~+0.46
    # runs a game of seasonal ramp removed, concentrated in April), so read the
    # TOTAL here as well as the ladder — unlike 4e's amplitude levers this one
    # is a location change on the run environment, not a spread change.
    #
    # `pitcentre-nowx` is the control that matters: weather already contributes
    # +0.402 of the same ramp, so if the two are doing one job the pair scores
    # much better than either and this should not ship at full strength.
    "pitcentre": {"PIT_PRIOR_CENTRED": True},
    # The centring solved over the arms that PITCH rather than the whole board.
    # Better-reasoned, measured WORSE — head-to-head t -4.40 (2026), t -1.93
    # (2025), and against `base` it is t -1.32 / +0.29 where `pitcentre` is
    # +3.46 / +2.18. Kept as the bracket so the argument is not re-derived and
    # re-run from scratch. `PIT_PRIOR_CENTRE_POP` carries the reasoning.
    "pitcentre-enginepop": {"PIT_PRIOR_CENTRED": True,
                            "PIT_PRIOR_CENTRE_POP": "engine"},
    "pitcentre-nowx": {"PIT_PRIOR_CENTRED": True,
                       "WEATHER_TEMP_RUNS_PER_F": 0.0,
                       "WEATHER_WIND_OUT_RUNS_PER_MPH": 0.0},
    # The prior deleted outright rather than centred — the bound on what the
    # centring can buy, since it is what flattened the ramp in the diagnosis.
    "nopitprior": {"PRIOR_SIDES": ()},
    # --- the HITTER playing-time prior (4e) -------------------------------
    # 4e localises the whole heavy-favourite gap to games where the UNDERDOG's
    # posted nine is thin (+1.271 runs, t +3.65 at a market price of 0.65+,
    # against +0.216 / t +0.60 when it is established). `batprior` is the
    # CENTRED version; `batprior-raw` is the naive flip that was rejected
    # before, kept so the centring can be shown to be the whole difference
    # rather than asserted.
    #
    # Read these on `mlb_sim.py diff`, NOT on the total: the defect moves the
    # two clubs in opposite directions and cancels exactly in H+A (4f).
    "batprior": {"USE_BAT_PRIOR": True, "BAT_PRIOR_CENTRED": True},
    "batprior-raw": {"USE_BAT_PRIOR": True, "BAT_PRIOR_CENTRED": False},
    # --- the hitter prior AGAINST the Triple-A prior ----------------------
    # `batprior` closes only ~15% of the gap in the subset it was built for, and
    # the suspect is that the two priors are fighting over the SAME players:
    # `MILB_MLB_PA_GATE` fires the Triple-A prior on exactly the callups the
    # playing-time prior is marking down, and it DISPLACES that prior outright.
    # `-aaafit` is the refitted credit; `-noaaa` is the BRACKET, the most that
    # removing the interaction could possibly be worth.
    "batprior-aaafit": {"USE_BAT_PRIOR": True, "BAT_PRIOR_CENTRED": True,
                        "MILB_CREDIT_SPEC": "applied"},
    "batprior-noaaa": {"USE_BAT_PRIOR": True, "BAT_PRIOR_CENTRED": True,
                       "USE_MILB_PRIOR": False},
    # --- the OTHER half of the heavy-favourite gap (4e) -------------------
    # The gap is the underdog's OFFENCE being over-projected (`batprior`) AND
    # the favourite's being UNDER-projected — i.e. the underdog's RUN PREVENTION
    # is over-rated. Over 8,050 team-games, OAA (t -2.83) and bullpen SIERA
    # (t +2.58) are nearly uncorrelated and both survive; the true OAA swing is
    # ~1.0 runs, so both 0.00022 and the shipped 0.00015 were too low.
    #
    # **The same-season OAA this was fitted on is partly endogenous** and the
    # LAGGED slope is a null, so a lag-1 backtest cannot validate it and the
    # fitted size is an upper bound. Still right for ORIGINATION, where lag is 0.
    "oaa2x": {"OAA_TO_BIP_SHIFT": 0.00030},
    # Club quality — the residual loading the bottom-up build does not carry.
    # `teamq` is the fitted 0.089; the others bracket it, because 0.089 came
    # off a t +1.24 all-games regression and the signal lives in the tail.
    "noteamq": {"TEAM_QUALITY_GAIN": 0.0},
    # --- pricing the 4i bullpen RAKING, after the fact ---------------------
    # The raking shipped UNFLAGGED (a sign-error repair, not a modelling choice),
    # so the reference was RECOVERED rather than rebuilt: **cached `teamq` IS the
    # incumbent**, run at today's shipped gain hours BEFORE the raking existed
    # and carrying the `joint` histogram. `armrake` overrides NOTHING; it is
    # today's configuration under its own filename, so a fresh run does not
    # overwrite `bt*_base_2000.json`. **Never --fresh an arm that is itself
    # somebody else's reference.**
    "armrake": {},
    # A named SNAPSHOT of the shipped configuration as of 2026-08-24, after the
    # park/weather/start-length repairs of 4j. Overrides NOTHING — it exists so
    # today's code can be priced against `armrake`, which is the same snapshot
    # taken BEFORE those repairs. Never `--fresh` either of them once scored.
    "parkfix": {},
    # A named SNAPSHOT of the shipped configuration as of 2026-08-27, after
    # §17e, `hold_bip_rate` and `CONTACT_SHRINK_BBE` 120 -> 600. Overrides
    # NOTHING — every one of those is inert while both contact flags are off,
    # which is exactly the claim it exists to let somebody CHECK rather than
    # take on faith. **`base` at 2000 reps is dated 2026-08-22** and predates
    # both the 4j park repairs and the 2c switch-hitter fixes, so it is not a
    # valid reference for today's code and must not be `--fresh`ed either — it
    # is the incumbent for `teamq` and others.
    "snap0827": {},
    "teamq2": {"TEAM_QUALITY_GAIN": 0.18},
    # The matchup-function gain (4e). Targeted at mismatches by construction —
    # see `LOG5_GAIN`. Probed at three sizes because nothing derives the
    # magnitude; the test is whether it moves the 0.65+ bucket while leaving
    # 0.55-0.60 alone, which is what every rate-level term failed.
    "log5g08": {"LOG5_GAIN": 1.08},
    "log5g15": {"LOG5_GAIN": 1.15},
    "batprior-oaa2x": {"USE_BAT_PRIOR": True, "BAT_PRIOR_CENTRED": True,
                       "OAA_TO_BIP_SHIFT": 0.00030},
    # --- the 3d.12 LOOK-AHEAD ablation ------------------------------------
    # Two of the model's inputs postdate the OPENING price, so a CLV number
    # measured against the open is partly measuring them. Both are ablated to
    # the state a genuine pre-lineup, pre-weather projection would be in, and
    # each separately so the two can be attributed. Zeroing the weather
    # coefficients puts every game at its own park's REFERENCE conditions, which
    # is "we have no weather information" rather than "the weather was average".
    # `nolook` is the honest pre-market configuration and is the arm the CLV
    # claim should be read off.
    "nowx": {"WEATHER_TEMP_RUNS_PER_F": 0.0,
             "WEATHER_WIND_OUT_RUNS_PER_MPH": 0.0},
    "nolineup": {"USE_POSTED_LINEUP": False},
    "nolook": {"WEATHER_TEMP_RUNS_PER_F": 0.0,
               "WEATHER_WIND_OUT_RUNS_PER_MPH": 0.0,
               "USE_POSTED_LINEUP": False},
    # **The PROPER fix rather than the bound.** `nowx` asks what the model is
    # worth with NO weather; this asks what it is worth with the SAME weather
    # the market had — the archived day-1 forecast instead of the game-time
    # observation. Any CLV that survives here was earned on the market's own
    # information set. Needs `mlb_sim.py forecastwx` to have been run.
    # day 0 = Open-Meteo's ANALYSIS. Still a look-ahead, like the shipped
    # observation, but through the same continuous-bearing path as the
    # forecast — so `omwx0` vs `fcstwx` is the pure INFORMATION effect and
    # `base` vs `omwx0` is the representation change.
    "omwx0": {"WEATHER_SOURCE": "forecast_d0"},
    "fcstwx": {"WEATHER_SOURCE": "forecast_d1"},
    # air density in place of the bare temperature term. Needs a weather source
    # that carries pressure and humidity, so it rides on the forecast path.
    "density": {"WEATHER_SOURCE": "forecast_d1", "USE_AIR_DENSITY": True},
    # per-start hook frailty — buys the deep-start tail the marginal hazard
    # cannot reach (5.6b). Fidelity fix; the price has not been measured.
    "frailty": {"HOOK_FRAILTY_SD": 0.40},
    # **The multi-season rate blend, per side, scored for the first time.** It
    # has shipped on since the module was written and nobody has ever asked what
    # it is worth. Per SIDE and not one combined arm, because a pitcher's season
    # stabilises 2-6x slower than a hitter's and has far more to gain from
    # another year — if both moved together a combined arm could not say which.
    # The Triple-A prior (9c/5.11) ships OFF, so that arm is the "after".
    "aaa": {"USE_MILB_PRIOR": True, "MILB_MLB_PA_GATE": 0.0},
    # The Triple-A prior GATED on MLB sample, which is what the published
    # systems do and what the out-of-sample split says (see MILB_MLB_PA_GATE).
    # `aaa` itself is the ungated version and is kept so the gate is what the
    # two arms differ by.
    "aaagate": {"USE_MILB_PRIOR": True, "MILB_MLB_PA_GATE": 150.0},
    "aaagate400": {"USE_MILB_PRIOR": True, "MILB_MLB_PA_GATE": 400.0},
    # The gate AND the credit refitted under the specification it is used in.
    "aaafit": {"USE_MILB_PRIOR": True, "MILB_MLB_PA_GATE": 150.0,
               "MILB_CREDIT_SPEC": "applied"},
    # Framing, from the PITCH-LEVEL series, lagged to the prior season by
    # `TEAM_CONTEXT_LAG`. **The first arm that can test framing at all** — the
    # Savant board ignores `year`, so until now every backtest ran with
    # framing ablated. Against `base` this measures framing EXISTING, not
    # pitch-level framing against Savant framing; the latter is not available,
    # because the CSV is the thing that cannot be lagged.
    "pitchframe": {"USE_PITCH_FRAMING": True},
    "bat1yr": {"USE_SEASON_BLEND_BAT": False},
    "pit1yr": {"USE_SEASON_BLEND_PIT": False},
    # The base-running constants as they were HAND-SET, against the MEASURED
    # values that now ship (5.6c). This arm is the "before", so a positive
    # reading for `base` over `handrun` is what the measurement bought.
    #
    # Note what it does NOT price: `ab_configure` ablates framing, so
    # `FRAMING_K_SHARE` — measured in the same pass and the largest single
    # correction — cannot move a number here. It sets K and BB PROPS, which
    # this harness does not score at all.
    "handrun": {"P_SAC_FLY": 0.50, "P_GIDP": 0.30, "P_GB_ADVANCE": 0.45,
                "P_GB_SCORES": 0.45, "P_STEAL_SUCCESS": 0.78},
    "fcstwx-nolineup": {"WEATHER_SOURCE": "forecast_d1",
                        "USE_POSTED_LINEUP": False},
    # --- the ML state-vector experiment (mlb_ml.py) -----------------------
    # A residual on the nine-outcome vector, trained on 630,420 real plate
    # appearances against the vector THIS ENGINE would have produced. It beat the
    # incumbent at PA level on two unseen test seasons — which is why these arms
    # exist and NOT a reason to ship anything. `ML_MODEL_FOLD` is walk-forward:
    # pricing 2026 with a model that saw 2026 is the leak this harness exists to
    # prevent.
    "mlrate": {"RATE_MODEL": "ml", "ML_MODEL_TAG": "C"},
    "mlblend25": {"RATE_MODEL": "blend", "ML_MODEL_TAG": "C",
                  "ML_BLEND_ALPHA": 0.25},
    "mlblend50": {"RATE_MODEL": "blend", "ML_MODEL_TAG": "C",
                  "ML_BLEND_ALPHA": 0.50},
    # The level-drift follow-up. `mlrate` was worse than the incumbent on the
    # moneyline in BOTH seasons and worse on totals-vs-line in both; the
    # diagnosis was a run-level shift that flipped sign between them. These
    # strip the level on the population actually being priced, leaving only
    # the row-varying part Level 1 measured at +0.0030 nats.
    "mlrate-sc": {"RATE_MODEL": "ml", "ML_MODEL_TAG": "C",
                  "ML_SELF_CENTRE": True},
    "mlblend50-sc": {"RATE_MODEL": "blend", "ML_MODEL_TAG": "C",
                     "ML_BLEND_ALPHA": 0.50, "ML_SELF_CENTRE": True},
    # alpha = 0.25 is the interesting weight, not 0.50. On the totals-vs-
    # closing-line correlation — the ONLY measure in this harness with the
    # resolving power to separate a rate-layer change (the moneyline cannot
    # reach |t| = 2 on DELETING THE POSTED LINEUP) — `mlblend25` is the only
    # arm in the whole library that beats `base`, and it does so in both
    # seasons: +0.0042 on 2025, +0.0052 on 2026, disagreement sd down in both.
    "mlblend25-sc": {"RATE_MODEL": "blend", "ML_MODEL_TAG": "C",
                     "ML_BLEND_ALPHA": 0.25, "ML_SELF_CENTRE": True},
    # --- the HIERARCHY (fixes memo section 1) -----------------------------
    # The flat model puts 80% of its Brier gain into strikeouts and the two
    # out types and 3.3% into home runs, so it spends its accuracy where the
    # run value is not. These model the conditional structure instead, one
    # binary residual per node. `hier25` is every node; `hierrun25` is the
    # three that carry run value, which the PA-level ablation says is where
    # the K node takes the run-level error from +0.070 to +0.021 while the HR
    # node takes it to +0.127 — a split log loss cannot see.
    "hier25": {"RATE_MODEL": "blend", "ML_HIER_NODES": "all",
               "ML_BLEND_ALPHA": 0.25},
    "hierrun25": {"RATE_MODEL": "blend", "ML_HIER_NODES": "K,BB,HR",
                  "ML_BLEND_ALPHA": 0.25},
    # --- the SEARCHED node configuration (mlb_ml section 5b) --------------
    # `LGB_NODE_PARAMS` was chosen and never searched, and one parameter set
    # served six nodes whose training sets span 8,028 to 325,841 rows —
    # `min_data_in_leaf = 500` is 6.2% of the 3B node's entire training set. A
    # hand probe moved every one of the six, all toward SMALLER trees and a
    # LOWER learning rate, which is what a residual on a strong prior should
    # want. Selected on fold f25's VALIDATION season and nothing else, so it
    # leaks into neither test season, and it is the ONLY difference from
    # `hier25` — anything it moves is the fit, not the architecture.
    "hier25tuned": {"RATE_MODEL": "blend", "ML_HIER_NODES": "all",
                    "ML_BLEND_ALPHA": 0.25, "ML_NODE_PARAMS": "tuned"},
    # --- the GAME-STATE residual (4b.5) -----------------------------------
    # `hier25v2` is the CONTROL: the hierarchy retrained on the current
    # baseline, no state. The cached `hier25` cannot serve as one — it predates
    # the joint histogram, the bullpen raking AND the park-decontam baseline, so
    # three things differ at once. `hier25state` is the same model with BASE-OUT
    # served: +33% on the residual's whole contribution, every f26 node
    # improved, BB nearly TRIPLED — walk rate is strongly base-out dependent,
    # which is exactly what a base-out-blind rate layer cannot express.
    "hier25v2": {"RATE_MODEL": "blend", "ML_HIER_NODES": "all",
                 "ML_BLEND_ALPHA": 0.25, "ML_STATE_COLS": ""},
    "hier25state": {"RATE_MODEL": "blend", "ML_HIER_NODES": "all",
                    "ML_BLEND_ALPHA": 0.25, "ML_STATE_COLS": "baseout"},
    # alpha = 1.0. `hier25state` runs at 0.25 because that is what is
    # comparable to the recorded `hier25`, but 0.25 is a LOWER BOUND for the
    # state model: the PA optimum is 0.8-1.0, and base-out adds signal WITHOUT
    # worsening the level bias that forced alpha down (2025 +0.1220 -> +0.1047,
    # 2026 unchanged). alpha is applied at inference in logit space, so this is
    # the same models — no retrain. If the ladder is flat here too, game state
    # is a genuine null at the price rather than a weight artifact.
    "hier100state": {"RATE_MODEL": "blend", "ML_HIER_NODES": "all",
                     "ML_BLEND_ALPHA": 1.0, "ML_STATE_COLS": "baseout"},
}

# Which fold's model prices which season. The rule is that a season may only
# be priced by a model whose TRAINING and VALIDATION both end before it.
ML_FOLD_FOR_SEASON: Dict[int, str] = {2025: "f25", 2026: "f26"}


class AbHarness:
    """The A/B harness — one rate-layer change against the close."""

    @staticmethod
    def ml_fold_span(fold: str) -> str:
        """Human description of what a fold saw, so a live run says it out loud."""
        import mlb_ml
        tr, va, _ = mlb_ml.FOLDS[fold]
        return f"{'+'.join(str(s) for s in tr)}, validated {va}"

    @staticmethod
    def _ab_ll(p: float, y: float) -> float:
        return -(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9)))

    @staticmethod
    def _ab_paired(d: Sequence[float]) -> Tuple[float, float, float]:
        n = len(d)
        if n < 2:
            return 0.0, float("nan"), float("nan")
        mu = statistics.mean(d)
        se = statistics.pstdev(d) / n ** 0.5
        return mu, se, (mu / se if se else float("nan"))

    @staticmethod
    def clv_edge_buckets(rows: Sequence[dict],
                         edges: Sequence[float] = (0.0, 0.02, 0.04, 0.06, 0.09)
                         ) -> List[dict]:
        """Hit rate by how far the model disagreed with the close.

        The shape matters more than any single bucket: a real edge grows with
        disagreement. A flat profile with one good bucket is the signature of
        noise, and it is the failure mode this table exists to expose.
        """
        out = []
        for lo, hi in zip(edges, list(edges[1:]) + [1.0]):
            sel = [r for r in rows if lo <= r["edge"] < hi]
            if sel:
                s = ClosingScore.summarize_clv_bucket(sel)
                s["lo"], s["hi"] = lo, hi
                out.append(s)
        return out


# **HISTORICAL arms: scored, never re-run.** Some changes are CODE rather than
# a constant — the posted-lineup fallback fix lives inside `_game_side` and no
# flag can toggle it — so the only "before" that exists is a run made while the
# old code was present. Regenerating one with today's code would produce TODAY's
# model under a name claiming to be the old one, and the result would look like
# a clean null. `--fresh` must not touch them and `ab_run_arm` refuses.
AB_REFERENCE: Dict[str, str] = {
    "prelineupfix": (
        "arsenal prior ON, before the posted-lineup fallback fix. A callup "
        "with no board row made _game_side reject all nine hitters and fall "
        "back to the board's best-nine-by-PA (positively selected), on "
        "1.8-9% of games. seed 17, 2000 reps, leak-free."),
    "prelineupfix-nostuff": (
        "the same run with USE_STUFF_PRIOR off — the pre-fix incumbent."),
    "teamq": (
        "TEAM_QUALITY_GAIN = 0.089 — today's SHIPPED value — run 2026-08-22 at "
        "17:42/17:51, a few hours BEFORE the margin RAKING existed in "
        "`deployment_score`. It is therefore exactly 'shipped config minus the "
        "raking', and it is the ONLY reference the raking can be priced "
        "against, because that change shipped unflagged (4i). It moved from "
        "AB_ARMS to here rather than being deleted: the arm's value now comes "
        "entirely from WHEN it was run, so re-running it would produce today's "
        "model — raking included — under a name claiming to be the incumbent, "
        "and the comparison would read as a clean null."),
    "preparkfix": (
        "arsenal prior ON and lineups FIXED, but before park_run_reliability() "
        "attenuated the LAGGED park factor. PARK_RUN_RELIABILITY = 0.699 was "
        "solved for a contemporaneous factor and the backtest reads the prior "
        "season's, so the park term ran 2-4x too strong. seed 17, 2000 reps, "
        "leak-free."),
}


# Arms that override NOTHING on purpose. `armrake` is a named SNAPSHOT of the
# shipped configuration so a fresh run does not overwrite `bt*_base_2000.json`,
# the only surviving reference for `TEAM_QUALITY_GAIN`. Listed here rather than
# silently exempted from `test_ab_base_arm_IS_the_shipped_model`, which is
# otherwise right that an empty arm is base under another name.
AB_SNAPSHOT_ARMS: frozenset = frozenset({"armrake", "parkfix",
                                         "snap0827", "bmielke"})

_AB_SHIPPED: Dict[str, object] = {}


def _ab_shipped_defaults() -> Dict[str, object]:
    """The SHIPPED value of every constant any arm overrides.

    Snapshotted on first call, before any arm has run, so it records what the
    module actually ships rather than whatever the last arm left behind.
    """
    if not _AB_SHIPPED:
        for arm in AB_ARMS.values():
            for k in arm:
                _AB_SHIPPED[k] = globals()[k]
    return _AB_SHIPPED


def ab_configure(overrides: Dict[str, object], season: int) -> None:
    """The leak-free baseline, plus this arm's overrides.

    `TEAM_CONTEXT_LAG = 1` takes OAA and the park run factor from the PRIOR
    season; framing is ABLATED rather than lagged because Savant's board returns
    the current season for every `year`, so a "lagged" framing file is a leak
    wearing the label of the fix for it (§3d.2). The stuff-model cache is cleared
    per arm — its feature WIDTH changes with `STUFF_USE_ARSENAL`.

    **Every constant ANY arm touches is restored to its shipped value first.**
    Otherwise the result is order-dependent: `base` sets `USE_STUFF_PRIOR =
    False`, then `shipped` inherits it and runs the same model twice. That is
    precisely the failure this harness exists to detect, and it shipped inside
    the detector itself — caught by a smoke run whose arms agreed to the last
    digit. Treat exact agreement as a bug report, never as a null.
    """
    global TEAM_CONTEXT_LAG, FRAMING_TILT_SCALE, PARK_RUN_SEASON, DEPLOY_SEASON
    TEAM_CONTEXT_LAG = 1
    PARK_RUN_SEASON = season
    # **Set here as well as in `backtest`, or the FINGERPRINT is order-dependent
    # (5.22).** `backtest` assigns it and nothing restores it — no arm overrides
    # it, so `_ab_shipped_defaults` never sees it — and the digest is taken
    # BEFORE the simulation. So arm #1 of a run is stamped at the module default
    # and arm #2 at whatever season #1 replayed, a value no read-only process
    # can reproduce. Every 2025 arm after the first read STALE for this alone.
    # No simulation changes: `backtest` still assigns the same value before any
    # game is priced. This only makes the season EXPLICIT at configure time
    # instead of a leftover from whatever ran last.
    DEPLOY_SEASON = season
    for k, v in _ab_shipped_defaults().items():
        globals()[k] = v
    for k, v in overrides.items():
        globals()[k] = v
    # **Framing is ablated only because SAVANT'S board cannot be lagged**, and
    # that reason expires the moment a lagged series exists — the pitch-level
    # model is date-aware, so framing can finally be MEASURED rather than
    # switched off.
    #
    # Decided AFTER the overrides, and deliberately NOT expressed as an arm
    # override: `_ab_shipped_defaults` snapshots every constant ANY arm touches
    # and restores it for EVERY arm, so one arm naming `FRAMING_TILT_SCALE`
    # would hand `base` its shipped 0.6394 and silently turn framing on for the
    # baseline.
    FRAMING_TILT_SCALE = FRAMING_TILT_SHIPPED if USE_PITCH_FRAMING else 0.0
    # `fit_stuff_model` regresses against `playing_time_prior`, so an arm that
    # touches `PRIOR_SIDES` changes what it was fitted ON. A pool worker
    # re-imports into an empty dict, but this loop runs in the PARENT, where a
    # cache does survive from one arm to the next. `_BM_CACHE` is keyed on
    # (pid, season, as_of) and holds a metric no arm can reconfigure, so it is
    # deliberately NOT cleared — see `bmielke_asof`.
    _STUFF_MODEL.clear()


def ab_run_arm(season: int, name: str, reps: int, fresh: bool = False,
               workers: Optional[int] = None, verbose: bool = True) -> dict:
    """One arm, cached by (season, arm, reps).

    **`reps` is in the cache key on purpose.** At 40 sims the Monte Carlo se on
    `p_home` is 0.079, more than twice a 3% edge threshold, so scoring a 40-sim
    arm against a 2,000-sim one measures the rep count and reports it as the
    change.
    """
    AB_DIR.mkdir(parents=True, exist_ok=True)
    path = AB_DIR / f"bt{season}_{name}_{reps}.json"
    if name in AB_REFERENCE:
        # Rebuilding one of these with today's code would produce TODAY's model
        # under a name claiming to be the old one — and the A/B would read as a
        # clean null. They are artifacts, not configurations.
        if not path.exists():
            raise FileNotFoundError(
                f"mlb_sim: reference arm {name!r} for {season} at {reps} reps "
                f"is not on disk ({path}), and it CANNOT be regenerated — it "
                f"was produced by code that no longer exists. "
                f"{AB_REFERENCE[name]}")
        with open(path) as fh:
            return json.load(fh)
    ab_configure(AB_ARMS[name], season)
    # Walk-forward, set AFTER the arm's overrides and BEFORE the fingerprint,
    # so it is part of the arm's identity. An arm priced by the wrong fold is
    # a leak; an arm priced by the right one but fingerprinted without it
    # would collide on disk with the other season's.
    global ML_MODEL_FOLD
    ML_MODEL_FOLD = (ML_FOLD_FOR_SEASON.get(season, "")
                     if RATE_MODEL != "baseline" else "")
    if RATE_MODEL != "baseline" and not ML_MODEL_FOLD:
        raise ValueError(
            f"mlb_sim: arm {name!r} needs a trained ML fold for {season} and "
            f"ML_FOLD_FOR_SEASON has none. Pricing a season with a model that "
            f"saw it is a leak; refusing rather than guessing.")
    fp = _ab_fingerprint()
    if path.exists() and not fresh:
        with open(path) as fh:
            got = json.load(fh)
        stale = got.get("_constants") != fp
        if verbose:
            note = ""
            if stale:
                # **A digest DETECTS but cannot LOCALISE** — 5.22. Reporting
                # "something moved" against 244 constants sent one real
                # investigation down a ten-minute rebuild to learn the arm was
                # byte-identical. The dict is a few KB against a 5 MB arm.
                delta = _ab_constants_delta(got.get("_constants_dict"))
                note = ("  ** STALE: built under different constants; "
                        "re-run with --fresh before reading it against a "
                        "freshly built arm **")
                if delta is None:
                    note += ("\n      (arm predates _constants_dict — rebuild "
                             "it once to get a diagnosable fingerprint)")
                elif not delta:
                    # Every captured constant agrees, so the DIGEST is what
                    # moved, not the model. Not a reason to re-run.
                    stale = False
                    note = ("  cached, digest mismatch but ALL 244 constants "
                            "agree — not stale (5.22)")
                else:
                    note += "\n      differs on: " + ", ".join(
                        f"{k}: {a!r} -> {b!r}" for k, (a, b) in
                        sorted(delta.items())[:12])
            print(f"  {season} {name:8s} cached  ({path.name}){note}",
                  flush=True)
        return got
    Archive._progress(f"ab: {season} {name} starting, {reps} sims/game")
    t = time.time()
    # verbose=True so the per-cutoff progress reaches the terminal — a silent
    # eight-minute arm is indistinguishable from a hung one.
    bt = backtest(season, reps=reps, seed=17, workers=workers, verbose=verbose)
    sc = score_backtest(bt)
    line = (f"{season} {name:8s} n {sc['n']:4d}  "
            f"total {sc['model_mean_total']:.3f} vs "
            f"{sc['actual_mean_total']:.3f} (bias {sc['total_bias']:+.3f})  "
            f"corr {sc['total_corr']:+.4f}  "
            f"home {sc['model_home_win']:.4f} (bias {sc['ml_bias']:+.4f})  "
            f"[{time.time() - t:.0f}s]")
    if verbose:
        print(f"  {line}", flush=True)
    Archive._progress(f"ab: {line}")
    bt["_constants"] = fp
    bt["_constants_dict"] = _ab_constants_snapshot()
    with open(path, "w") as fh:
        json.dump(bt, fh)
    return bt


def _ab_constants_snapshot() -> Dict[str, str]:
    """Every captured constant as the STRING the fingerprint hashes.

    Stored beside the digest so a mismatch can be read rather than guessed at.
    Strings, not raw values, because that is exactly what `_ab_fingerprint`
    digests — a snapshot that round-trips differently from the hash input would
    report "no difference" on the one thing that actually moved.
    """
    o = _slate_overrides()
    return {k: json.dumps(o[k], default=str, sort_keys=True) for k in sorted(o)}


def _ab_constants_delta(stored: Optional[Dict[str, str]]
                        ) -> Optional[Dict[str, Tuple[str, str]]]:
    """{constant: (stored, now)} for everything that moved, or None if unknown.

    An EMPTY dict is the interesting answer: the digest disagreed while every
    constant it hashes agrees, which means the mismatch is in the digest's own
    inputs (capture order, an unstable `str()`) and not in the model. 5.22.
    """
    if not isinstance(stored, dict):
        return None
    now = _ab_constants_snapshot()
    return {k: (stored.get(k, "<absent>"), now.get(k, "<absent>"))
            for k in set(stored) | set(now)
            if stored.get(k, "<absent>") != now.get(k, "<absent>")}


def _ab_fingerprint() -> str:
    """A digest of every constant an arm's model is made of.

    **A cached arm is only comparable to one built from the same code.** That is
    what `--fresh` is for, but it is a thing a person has to remember, and
    forgetting it does not fail — it produces two clean-looking result blocks
    whose difference is partly the change under test and partly whatever else
    moved in between. Measuring the base-running constants moved five at once.

    Stamped into the arm file and checked on every cache hit, AFTER
    `ab_configure`, so an arm's own overrides are part of its identity.
    """
    o = _slate_overrides()
    blob = json.dumps({k: o[k] for k in sorted(o)},
                      default=str, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def ab_score(by_season: Dict[int, Dict[str, dict]],
             save_dir: Path = SAVE_DIR) -> None:
    """Closing-line comparison per season, then pooled."""
    arms = [a for a in list(AB_ARMS) + list(AB_REFERENCE)
            if all(a in g for g in by_season.values())]
    if "base" not in arms:
        raise ValueError("mlb_sim: ab_score needs a 'base' arm to pair against")
    pool = {a: {"m": [], "x": [], "y": [], "ret": []} for a in arms}
    for season, got in sorted(by_season.items()):
        got = {a: got[a] for a in arms}
        # **TOTALS against the CLOSING LINE, which is ~7x the instrument that
        # scoring against results is.** se on corr(model, market) is ~0.013
        # against ~0.099 on the slope of actual-on-model, because the line is a
        # low-noise target and a realised total is not. Every effect this file
        # failed to resolve was fighting that 0.099.
        tm = {a: score_totals_vs_market(bt, season, save_dir)
              for a, bt in got.items()}
        if any(t.get("n", 0) >= 30 for t in tm.values()):
            ref = next(t for t in tm.values() if t.get("n", 0) >= 30)
            print(f"\n  {season} TOTALS vs the closing total  (n {ref['n']}; "
                  f"the market itself: corr "
                  f"{ref['market_vs_actual']['corr']:+.4f}, slope "
                  f"{ref['market_vs_actual']['slope']:.3f})")
            print(f"    {'arm':9s} {'corr w/ line':>12s} {'disagree sd':>12s} "
                  f"{'corr w/ actual':>14s} {'slope':>7s}")
            for a in arms:
                t = tm[a]
                if t.get("n", 0) < 30:
                    continue
                print(f"    {a:9s} {t['vs_market']['corr']:+12.4f} "
                      f"{t['disagreement_sd']:12.3f} "
                      f"{t['vs_actual']['corr']:+14.4f} "
                      f"{t['vs_actual']['slope']:7.3f}")
        picks = {a: ClosingScore.clv_vs_closing(bt, season, edge=0.03, price="avg",
                                  save_dir=save_dir)
                 for a, bt in got.items()}
        rows = {a: {(p["pk"], p["date"]): p for p in picks[a]["picks"]}
                for a in picks}
        # PAIRED means paired: only games every arm matched to a closing line
        shared = sorted(set.intersection(*(set(r) for r in rows.values())))
        print(f"\n  {season}: {len(shared)} games matched to a closing "
              f"moneyline in every arm")
        if len(shared) < 30:
            print("    too few to score")
            continue
        # **Two arms that agree to the last digit did not run.** A static test
        # cannot catch every way this happens — a constant that does not travel
        # to a pool worker, a cached arm reused across a code change, an arm
        # whose override was undone — so it is checked on the DATA every time.
        for a in arms[1:]:
            if all(rows[a][k]["model_home"] == rows["base"][k]["model_home"]
                   for k in shared):
                print(f"    ** {a} is IDENTICAL to base on every game. The A/B "
                      f"did not run. **\n    Check: does the override reach a "
                      f"pool worker (_slate_overrides), and were these arms "
                      f"cached\n    before the change? --fresh re-runs them.")
        base = rows["base"]
        y = [1.0 if (base[k]["won"] if base[k]["side"] == "home"
                     else not base[k]["won"]) else 0.0 for k in shared]
        mkt = [base[k]["mkt_home"] for k in shared]
        lm = [AbHarness._ab_ll(p, o) for p, o in zip(mkt, y)]
        print(f"    {'arm':9s} {'log-loss':>10s} {'vs mkt t':>9s} "
              f"{'vs base':>9s} {'ROI':>8s}")
        print(f"    {'market':9s} {statistics.mean(lm):10.5f}")
        lb = None
        for a in arms:
            xp = [rows[a][k]["model_home"] for k in shared]
            lx = [AbHarness._ab_ll(p, o) for p, o in zip(xp, y)]
            t_mkt = AbHarness._ab_paired([u - v for u, v in zip(lm, lx)])[2]
            t_base = (float("nan") if lb is None else
                      AbHarness._ab_paired([u - v for u, v in zip(lb, lx)])[2])
            ret = [(rows[a][k]["dec"] - 1.0) if rows[a][k]["won"] else -1.0
                   for k in shared]
            print(f"    {a:9s} {statistics.mean(lx):10.5f} {t_mkt:+9.2f} "
                  f"{t_base:+9.2f} {statistics.mean(ret):+8.4f}")
            if lb is None:
                lb = lx
            pool[a]["m"] += mkt
            pool[a]["x"] += xp
            pool[a]["y"] += y
            pool[a]["ret"] += ret

    if len(by_season) < 2 or not pool["base"]["y"]:
        return
    print(f"\n  POOLED  n {len(pool['base']['y'])}")
    lm = [AbHarness._ab_ll(p, o) for p, o in zip(pool["base"]["m"], pool["base"]["y"])]
    print(f"    {'arm':9s} {'log-loss':>10s} {'vs mkt t':>9s} "
          f"{'vs base':>9s} {'ROI':>8s}")
    print(f"    {'market':9s} {statistics.mean(lm):10.5f}")
    lb = None
    for a in arms:
        d = pool[a]
        lx = [AbHarness._ab_ll(p, o) for p, o in zip(d["x"], d["y"])]
        t_mkt = AbHarness._ab_paired([u - v for u, v in zip(lm, lx)])[2]
        t_base = (float("nan") if lb is None else
                  AbHarness._ab_paired([u - v for u, v in zip(lb, lx)])[2])
        print(f"    {a:9s} {statistics.mean(lx):10.5f} {t_mkt:+9.2f} "
              f"{t_base:+9.2f} {statistics.mean(d['ret']):+8.4f}")
        if lb is None:
            lb = lx
    print("\n  A pooled t inside ~2 is not an edge, and an arm that helps one "
          "season while\n  hurting the other is noise however good the pooled "
          "number looks (3d.1, 3d.3).\n  Same sign in BOTH seasons is the bar.")


def _corr(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    if len(a) < 3 or len(a) != len(b):
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return num / (da * db) if da and db else None


# ===========================================================================
# 17c. THE HITTER PITCH-DETAIL CACHE — what every contact model reads from
# ===========================================================================
# `PRIOR_SIDES` is pitchers only for a reason that is still right (§5.9) — but
# "no playing-time prior" was silently taken to mean "no prior at all", and
# league average is a poor description of a hitter we have 40 PAs of.
#
# **It matters more than the rookie count suggests**: only 4.8% of lineup slots
# carry under 50 effective PA, but the MEDIAN is 423, and a 423-PA hitter is
# still 85% league on doubles and 63% on home runs. Bat speed, attack angle,
# intercept depth and whiff are measured on SWINGS, so they stabilise far
# faster. **As-of costs one fetch per player-season, not one per cutoff** — the
# detail CSV carries `game_date`, so ~40 MB a season rather than ~1.3 GB.
#
# **This section is now the DATA layer only.** It used to also hold §3d.6's
# BMIELKE-index-as-a-multiplier (`bmielke_relative`, `bmielke_asof`,
# `bmielke_prior`, `league_wobacon` and their constants). That wiring was
# measured at moneyline paired t -2.62, retired, and then sat uncalled for
# weeks with one test keeping it alive — DELETED 2026-08-26. Two consumers
# read this cache now: §17d's contact map and §17e's swing prior. The metric
# itself still lives in `bmielke_core`, which both this file and EffortMLB
# import, and §17e reads its swing-description sets from there so the two
# cannot drift.

# What `bmielke()` reads, PLUS launch angle and the realised event, which the
# contact->outcome mapping needs (§3d.7). Everything else in that CSV is ~90% of
# its bytes and none of its information here.
#
# **The cache directory is VERSIONED.** Adding a field to a cache that already
# holds thousands of files is the classic silent corruption: the old files parse
# fine, the new field reads None everywhere, and the model quietly runs on a
# constant.
_BM_FIELDS = ("date", "desc", "bat_speed", "attack_angle", "icept_y",
              "ev", "la", "hc_x", "hc_y", "xwoba", "event")
BM_CACHE_VERSION = "v2"


class Bmielke:
    """The per-hitter Savant pitch-detail cache. Fetch, path, nothing else."""

    @staticmethod
    def bmielke_detail_path(pid: int, season: int,
                            save_dir: Path = SAVE_DIR) -> Path:
        return (Path(save_dir) / "bmielke" / BM_CACHE_VERSION / f"{season}"
                / f"{pid}.json.gz")

    @staticmethod
    def fetch_bmielke_season(pids: Sequence[int], season: int, workers: int = 10,
                             save_dir: Path = SAVE_DIR,
                             verbose: bool = True,
                             max_age_days: Optional[float] = None) -> int:
        """Cache the detail for a list of hitters.

        **`max_age_days` exists because skipping on EXISTENCE alone is a
        staleness bug, and this file has already been bitten by it twice.**
        §2d records `load_reliever_traits` / `load_team_framing` /
        `load_team_defense` rebuilding only when the file is ABSENT, so
        `reliever_traits_2026.csv` sat 11 days old at 279 arms against a real
        pen population of 521. This cache had the identical shape: on
        2026-08-27 every one of the 159 gated hitters was being scored on
        detail that ended 2026-08-15, because all 624 files existed and were
        therefore all skipped.
        Twelve days is ~40-50 swings — material for a metric whose whole
        premise is that swing evidence accumulates faster than outcomes — and
        it silently moves hitters across `BMIELKE_GATE_BBE` in the wrong
        direction, since balls in play only ever accumulate.

        Pass `max_age_days` before a slate. None keeps the old
        skip-if-present behaviour for a first fill.
        """
        cutoff = (time.time() - max_age_days * 86400.0
                  if max_age_days is not None else None)

        def _needs(p: int) -> bool:
            path = Bmielke.bmielke_detail_path(p, season, save_dir)
            if not path.exists():
                return True
            if cutoff is None:
                return False
            try:
                return path.stat().st_mtime < cutoff
            except OSError:
                return True

        todo = [p for p in pids if _needs(p)]
        if verbose:
            print(f"[bmielke] {season}: {len(todo)} of {len(pids)} to fetch",
                  flush=True)
        got = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, rows in enumerate(
                    ex.map(lambda p: fetch_bmielke_detail(
                        p, season, save_dir, allow_fetch=True,
                        force=cutoff is not None), todo), 1):
                got += bool(rows)
                if verbose and i % 25 == 0:
                    Archive._progress(f"bmielke {season}  {i}/{len(todo)}  ({got} with rows)")
        if verbose:
            print(f"[bmielke] {season}: {got}/{len(todo)} returned rows")
        return got


_BM_DETAIL: Dict[tuple, List[dict]] = {}


def fetch_bmielke_detail(pid: int, season: int, save_dir: Path = SAVE_DIR,
                         timeout: float = 60.0,
                         allow_fetch: bool = False,
                         force: bool = False) -> List[dict]:
    """One hitter's season of pitch detail, trimmed and cached gzipped.

    Memoised in process as well as on disk: a backtest asks for the same
    hitter once per CUTOFF, and re-reading plus re-inflating a 60 KB gzip
    twenty times per player is most of the cost of the whole prior.
    """
    key = (int(pid), int(season))
    got = _BM_DETAIL.get(key)
    if got is not None and not force:
        return got
    path = Bmielke.bmielke_detail_path(pid, season, save_dir)
    # `force` is the REFRESH path — see `fetch_bmielke_season`'s note on why
    # skipping a present-but-stale file is a bug rather than an economy.
    if path.exists() and not force:
        try:
            with gzip.open(path, "rt") as fh:
                data = json.load(fh)
            _BM_DETAIL[key] = data
            return data
        except (OSError, ValueError):
            pass

    # **A cache MISS must not become a network call here.** Every consumer
    # (`bmielke_asof`, `Contact.hitter_contact_profile`) runs inside
    # `build_rates`, which runs inside a pool worker, so an un-cached player
    # would trigger a live 8-second Savant fetch mid-backtest: measured, ONE
    # cutoff's rate table took 420s against 0.3s. Pre-fetching is an explicit
    # step (`fetch_bmielke_season`).
    if not allow_fetch:
        _BM_DETAIL[key] = []
        return []

    rows: List[dict] = []
    try:
        r = requests.get(SAVANT_DETAIL_URL.format(season=season, pid=pid),
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if r.status_code == 200 and r.text.strip():
            for row in csv.DictReader(io.StringIO(r.text)):
                if not (row.get("pitch_name") or ""):
                    continue
                rows.append({
                    "date": row.get("game_date") or "",
                    "desc": row.get("description") or "",
                    "bat_speed": _fnum(row.get("bat_speed")),
                    "attack_angle": _fnum(row.get("attack_angle")),
                    "icept_y": _fnum(row.get(
                        "intercept_ball_minus_batter_pos_y_inches")),
                    "ev": _fnum(row.get("launch_speed")),
                    "la": _fnum(row.get("launch_angle")),
                    "event": row.get("events") or "",
                    "hc_x": _fnum(row.get("hc_x")),
                    "hc_y": _fnum(row.get("hc_y")),
                    "xwoba": _fnum(row.get("estimated_woba_using_speedangle")),
                })
    except Exception:
        return []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as fh:
            json.dump(rows, fh)
    except OSError:
        pass
    _BM_DETAIL[key] = rows
    return rows


# Off until the A/B says otherwise. Uppercase, so `_slate_overrides` ships it
# to the pool. Named for what it is: the per-hitter CONTACT profile of §3d.7,
# not §3d.6's single BMIELKE multiplier, which is retired.
USE_CONTACT_PRIOR = False


# ===========================================================================
# 17d. CONTACT -> OUTCOME — the league mapping (sim_state.md 3d.7)
# ===========================================================================
# §3d.6 put contact quality into the prior as ONE multiplier over 1B/2B/3B/HR
# and made the moneyline worse while making totals better: one multiplier says a
# hitter whose extra quality is singles and one whose extra quality is home runs
# are the same hitter, so the DIFFERENCE between two teams picks up noise even
# as the SUM improves. The fix is to stop guessing the split and read it — what
# a ball hit that hard, at that angle, in that direction actually BECOMES
# league-wide. BallparkPal's "C-Only" model in substance.
#
# **Definitions are matched to `outcome_counts`, deliberately and exactly**,
# because a prior on one definition blended with observations on another is
# silently wrong: the board's outs INCLUDE ROE, sac flies, sac bunts and
# fielder's choice, and outs split ground/air by Savant's `bb_type` — the same
# taxonomy FanGraphs uses, NOT a launch-angle threshold of our own, which would
# be a third convention. **Nothing park-dependent may enter**:
# `hit_distance_sc` encodes the park and weather, which the engine applies
# separately.

CONTACT_EV_LO, CONTACT_EV_HI, CONTACT_EV_STEP = 40.0, 120.0, 5.0
CONTACT_LA_LO, CONTACT_LA_HI, CONTACT_LA_STEP = -60.0, 60.0, 6.0
CONTACT_SPRAY_BINS = 6            # over the 0-90 degree fair field
# How far outside the foul lines a computed spray angle may sit and still be
# treated as a line-hugging fair ball rather than bad coordinates.
CONTACT_POLAR_TOL = 15.0
# Shrinkage at each level of the hierarchy: a cell toward its (EV, LA) parent,
# that toward its LA grandparent, that toward the global rate. Counts, so a
# well-populated cell keeps its own answer and a thin one borrows.
CONTACT_SHRINK_K = 40.0

CONTACT_CLASSES = (S1B, S2B, S3B, HR, GB_OUT, AIR_OUT)


class Contact:
    """Contact quality -> outcome vector: the league EV/LA/spray map (sim_state.md 3d.7)."""

    @staticmethod
    def _contact_bins(ev: float, la: float, hc_x: float, hc_y: float):
        """(ev_bin, la_bin, spray_bin) or None when the ball is unusable."""
        if ev is None or la is None or hc_x is None or hc_y is None:
            return None
        e = int((min(max(ev, CONTACT_EV_LO), CONTACT_EV_HI - 1e-9)
                 - CONTACT_EV_LO) // CONTACT_EV_STEP)
        a = int((min(max(la, CONTACT_LA_LO), CONTACT_LA_HI - 1e-9)
                 - CONTACT_LA_LO) // CONTACT_LA_STEP)
        hla = spray_to_hla(hc_x, hc_y)
        if hla is None:
            return None
        # `spray_to_hla` gives the physics convention (0 = centre, +45 = RF line);
        # shift to the stadium polar 0-90 the rest of the module uses.
        polar = hla + 45.0
        # **CLAMP into the fair field, do not reject.** Rejecting everything
        # outside [0, 90] threw away 8.3% of batted balls NON-RANDOMLY — 17.8%
        # of the discards were doubles against 5.3% of those kept, because a
        # ball down the line is both the most likely to compute slightly foul and
        # the most likely to go for extra bases. It dragged the league doubles
        # rate from 6.2% to 5.3%, a bias built straight into the mapping. A ball
        # at polar -8 is a line drive whose coordinates are a degree off; one at
        # -45 is behind the plate and is bad data.
        if polar < -CONTACT_POLAR_TOL or polar > 90.0 + CONTACT_POLAR_TOL:
            return None
        polar = min(max(polar, 0.0), 90.0 - 1e-9)
        sbin = min(int(polar / (90.0 / CONTACT_SPRAY_BINS)),
                   CONTACT_SPRAY_BINS - 1)
        return e, a, sbin

    @staticmethod
    def _contact_class(event: str, bb_type: str) -> Optional[int]:
        """A realised batted ball -> one of the six outcome classes."""
        e = (event or "").strip()
        if e == "single":
            return S1B
        if e == "double":
            return S2B
        if e == "triple":
            return S3B
        if e == "home_run":
            return HR
        # Everything else that reached this function is a ball in play that did
        # not go for a hit: outs, fielder's choices, sacrifices AND errors, which
        # the board counts inside its outs (see the module note on ROE).
        return GB_OUT if (bb_type or "").strip() == "ground_ball" else AIR_OUT

    @staticmethod
    def build_contact_map(seasons: Sequence[int],
                          save_dir: Path = SAVE_DIR,
                          bbe_dir: Optional[Path] = None) -> dict:
        """League P(outcome | EV, launch angle, spray) from realised batted balls.

        `seasons` MUST predate the season being scored — the whole point is a
        league mapping that could have been known beforehand. A 2025 backtest gets
        2024; a 2026 backtest gets 2024+2025.

        Three nested tallies are kept, not one: the full cell, its (EV, LA) parent
        and its LA grandparent. A batted ball at 118 mph and 41 degrees down the
        line has a handful of league-wide examples a season, and its own cell is
        noise; its parents are not.
        """
        root = Path(bbe_dir) if bbe_dir else _APP_ROOT
        cell: Dict[tuple, List[float]] = {}
        pair: Dict[tuple, List[float]] = {}
        la_only: Dict[int, List[float]] = {}
        glob = [0.0] * N_OUTCOMES
        n_rows = n_used = 0

        def add(acc, key, cls):
            v = acc.get(key)
            if v is None:
                v = acc[key] = [0.0] * N_OUTCOMES
            v[cls] += 1.0

        for season in seasons:
            path = root / f"savant_bbe_{season}.csv"
            if not path.exists():
                continue
            with open(path) as fh:
                for row in csv.DictReader(fh):
                    n_rows += 1
                    b = Contact._contact_bins(_fnum(row.get("launch_speed")),
                                      _fnum(row.get("launch_angle")),
                                      _fnum(row.get("hc_x")),
                                      _fnum(row.get("hc_y")))
                    if b is None:
                        continue
                    cls = Contact._contact_class(row.get("events"), row.get("bb_type"))
                    if cls is None:
                        continue
                    n_used += 1
                    add(cell, b, cls)
                    add(pair, (b[0], b[1]), cls)
                    add(la_only, b[1], cls)
                    glob[cls] += 1.0

        if n_used == 0:
            raise RuntimeError(
                f"mlb_sim: no batted balls for {list(seasons)} under {root}")

        def norm(v):
            t = sum(v)
            return [x / t for x in v] if t > 0 else None

        g = norm(glob)

        def blended(counts, parent):
            n = sum(counts)
            w = n / (n + CONTACT_SHRINK_K)
            own = [c / n for c in counts]
            return [w * o + (1.0 - w) * p for o, p in zip(own, parent)]

        la_p = {k: blended(v, g) for k, v in la_only.items()}
        pair_p = {k: blended(v, la_p.get(k[1], g)) for k, v in pair.items()}
        cell_p = {k: blended(v, pair_p.get((k[0], k[1]), g)) for k, v in cell.items()}
        return {"seasons": list(seasons), "n_rows": n_rows, "n_used": n_used,
                "cell": cell_p, "pair": pair_p, "la": la_p, "global": g}

    @staticmethod
    def contact_lookup(cmap: dict, ev, la, hc_x, hc_y) -> Optional[List[float]]:
        """The outcome distribution for one batted ball, most specific first."""
        b = Contact._contact_bins(ev, la, hc_x, hc_y)
        if b is None:
            return None
        v = cmap["cell"].get(b)
        if v is not None:
            return v
        v = cmap["pair"].get((b[0], b[1]))
        if v is not None:
            return v
        return cmap["la"].get(b[1], cmap["global"])

    @staticmethod
    def hitter_contact_profile(rows: Sequence[dict], cmap: dict,
                               as_of: Optional[str] = None
                               ) -> Optional[Tuple[List[float], int]]:
        """(expected contact distribution, n balls) for one hitter's batted balls.

        Each of HIS balls is looked up in the LEAGUE map and the results averaged,
        so the answer is "what does a league-average defence in a league-average
        park do with the contact this hitter makes" — his profile, not his luck.
        """
        acc = [0.0] * N_OUTCOMES
        n = 0
        for r in rows:
            if as_of and (not r.get("date") or r["date"] >= as_of):
                continue
            v = Contact.contact_lookup(cmap, r.get("ev"), r.get("la"),
                               r.get("hc_x"), r.get("hc_y"))
            if v is None:
                continue
            n += 1
            for i in CONTACT_CLASSES:
                acc[i] += v[i]
        if n < CONTACT_MIN_BBE:
            return None
        return [a / n for a in acc], n

    @staticmethod
    def contact_prior(league: Sequence[float], profile: Sequence[float],
                      n: int, lg_profile: Sequence[float]) -> List[float]:
        """`league`, with its BALL-IN-PLAY mass redistributed by a hitter's profile.

        K, BB and HBP are untouched — a contact model has nothing to say about
        them. The in-play mass is held exactly constant and only its SHAPE moves,
        so this cannot shift a hitter's contact RATE, only what his contact turns
        into. That is the whole difference from §3d.6's single multiplier, which
        moved shape and rate together and could not tell a singles hitter from a
        slugger. `lg_profile` makes the ratio relative, so a year in which batted
        balls simply carry further cannot leak in as everyone being better.
        """
        w = n / (n + CONTACT_SHRINK_BBE)
        bip_lg = sum(league[i] for i in CONTACT_CLASSES)
        if bip_lg <= 0:
            return list(league)
        out = list(league)
        tot = 0.0
        shape = []
        for i in CONTACT_CLASSES:
            base = lg_profile[i]
            rel = (profile[i] / base) if base > 0 else 1.0
            v = league[i] * (1.0 + (rel - 1.0) * w)
            shape.append(v)
            tot += v
        if tot <= 0:
            return list(league)
        # renormalise the in-play block to exactly the mass it started with
        for i, v in zip(CONTACT_CLASSES, shape):
            out[i] = v * bip_lg / tot
        return _normalize(out)

    @staticmethod
    def contact_map_for(season: int, save_dir: Path = SAVE_DIR) -> Optional[dict]:
        """The league map a run scoring `season` is allowed to use.

        Strictly earlier seasons only — the map is league knowledge that could
        have been had before the season started, and fitting it on the season
        being scored would be the same leak as a season-final board.
        """
        key = (int(season), str(save_dir))
        if key in _CMAP_CACHE:
            return _CMAP_CACHE[key]
        root = _APP_ROOT
        have = sorted(int(p.stem.rsplit("_", 1)[1]) for p in
                      root.glob("savant_bbe_*.csv"))
        use = [y for y in have if y < season]
        out = None
        if use:
            try:
                out = Contact.build_contact_map(use, save_dir)
            except RuntimeError:
                out = None
        _CMAP_CACHE[key] = out
        return out

    @staticmethod
    def contact_profiles(pids: Sequence[int], season: int,
                         as_of: Optional[str] = None,
                         save_dir: Path = SAVE_DIR
                         ) -> Tuple[Dict[int, Tuple[List[float], int]], List[float]]:
        """{pid: (profile, n)} and the POPULATION's own average profile.

        The population average is what every hitter is compared against, so a
        season in which batted balls simply carry further shows up as nobody being
        better rather than everybody. Out of sample that is worth +2.65% of
        wOBAcon, which would otherwise land straight on the run level.
        """
        cmap = Contact.contact_map_for(season, save_dir)
        if cmap is None:
            return {}, []
        out: Dict[int, Tuple[List[float], int]] = {}
        for pid in pids:
            rows = fetch_bmielke_detail(int(pid), season, save_dir)
            if not rows:
                continue
            got = Contact.hitter_contact_profile(rows, cmap, as_of)
            if got is not None:
                out[int(pid)] = got
        if not out:
            return {}, []
        # Weighted by the balls behind each profile: the population mean is meant
        # to be the league's contact, and a 30-ball hitter is not a thirtieth of
        # the league's evidence for that.
        tot = float(sum(n for _, n in out.values())) or 1.0
        lg = [sum(prof[i] * n for prof, n in out.values()) / tot
              if i in CONTACT_CLASSES else 0.0 for i in range(N_OUTCOMES)]
        return out, lg


def _fnum(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Balls in play a hitter needs before his mapped profile is used at all, and
# the count at which it is trusted half against the league profile. Contact
# TYPE is a much more stable thing than a rate — every ball contributes to it
# — so this is far below the 2,335 the doubles RATE needs (§3d.5).
CONTACT_MIN_BBE = 25
# **600, not 120 — MEASURED, and 120 was ~5x too small.** It is a SIX-CLASS
# shape, so 25 balls in play is ~4 events per class. Out of sample at 120 the
# §3d.7 prior was +0.000176 worse pooled and worst on THIN hitters (+0.000359);
# at 600 it is -0.000024 and better in every band but the thinnest. Confirmed
# independently 2026-08-26 on §17e's gated population: run-value RMSE 0.03117 ->
# 0.03111 (2025) and 0.03409 -> 0.03392 (2026), better on 1B and 2B in both.
# `AB_ARMS["bmielke-shrink120"]` is the old value, kept as the bracket.
CONTACT_SHRINK_BBE = 600.0


_CMAP_CACHE: Dict[tuple, dict] = {}


# ===========================================================================
# 17e. BMIELKE — the GATED thin-sample contact prior for hitters
# ===========================================================================
# **What this is for, stated first, because two previous attempts got it wrong
# by forgetting it: projecting AAA callups and fringe bats.** A hitter with 40
# major-league plate appearances is shrunk 86% of the way to league on home
# runs and 95% on doubles, and league is a poor description of a man whose bat
# speed we have measured 150 times. BMIELKE reads bat speed, attack angle,
# intercept depth and whiff — all recorded on SWINGS, which arrive at ~2.7 per
# ball in play — and it is validated to beat a hitter's own xwOBAcon precisely
# in that regime.
#
# **THE GATE IS THE DESIGN, not a safety rail.** BMIELKE against plain
# xwOBAcon, frozen v9 trained on 2025 and applied unchanged to 2026,
# predicting rest-of-season xwOBAcon:
#
#     first N BBE     xwOBAcon    BMIELKE      verdict
#            25        +0.482     +0.746       BMIELKE, decisively
#            50        +0.572     +0.753       BMIELKE
#           120        +0.705     +0.772       BMIELKE, narrowing
#           180        +0.778     +0.765       **xwOBAcon wins**
#
# The crossover sits between 120 and 180 and `bmielke_core.BMIELKE_MAX_BBE`
# (175) is where it is drawn. **Above the gate this prior does nothing at
# all**, because above the gate the hitter's own line is the better number and
# the rate layer already has it. §3d.6 applied the metric to EVERY hitter and
# lost the moneyline at paired t -2.62; running a term outside the regime it
# was validated in is most of what that bought.
#
# For a debut hitter with fewer than `BMIELKE_MIN_BBE` (25) balls in play there
# is nothing to gate on, and the Triple-A ladder already covers him —
# `milb_prior` under `MILB_MLB_PA_GATE = 150`:
#
#     MLB BBE  < 25      milb_prior — his Triple-A line, level-translated
#     MLB BBE 25-100     BMIELKE, REFINING whatever prior survived
#     MLB BBE  > 175     his own outcomes; this prior stands aside
#
# **They are NOT disjoint in the middle band, and an earlier draft of this note
# claimed they were.** A callup can carry a Triple-A prior AND clear 25 balls in
# play, and with `BMIELKE_LEVEL_BASE = "league"` BMIELKE simply overwrote the
# Triple-A level — Osleivis Basabe read level 1.002 with 28% fast swings and a
# .3243 xwOBAcon and still gained +0.024 runs/PA, because a neutral reading
# against a league anchor pulls a marked-down hitter UP to the gated average.
# `BMIELKE_LEVEL_BASE = "prior"` fixes that: the reading REFINES the level he
# already had rather than replacing it.
#
# LEVEL FROM BMIELKE, SHAPE FROM HIS CONTACT MAP. The metric predicts ONE
# number — wOBA on contact — and the rate layer needs six. §3d.6 spread that
# one number PROPORTIONALLY across 1B/2B/3B/HR, which says a hitter whose extra
# quality is singles and one whose extra quality is home runs are the same
# hitter. They are worth very different runs, so the team-strength DIFFERENCE
# picked up noise even as the SUM improved — and a moneyline is a difference.
# That was the diagnosis and it is not fixed by gating.
#
# So the two halves come from the two things that are actually good at them:
#
#     shape      = Contact.hitter_contact_profile(his batted balls)  -> §17d
#     level      = bmielke(his swings + batted balls).raw            -> validated
#     direction  = contact_quality_direction(earlier seasons)        -> measured
#
# and the shape is moved ALONG THE MEASURED DIRECTION until its implied
# wOBA-on-contact equals the level. **The direction is the third thing and
# assuming it is what cost the first two attempts.** Better contact turns
# GROUND BALLS into HOME RUNS and produces FEWER singles, not more: the 1B
# slope is -0.118 (2025) and -0.110 (2026) against a proportional +0.213 — the
# wrong SIGN — while home runs carry +0.450/+0.471 against a proportional
# +0.046. Scored on the gated population, the proportional version moved 1B
# RMSE 0.02392 -> 0.02568 while improving home runs: the assumption showing up
# as an error, in the outcome that carries none of the signal. See
# `contact_quality_direction` for the full table and for why the FORECAST
# direction is the one that ships.
#
# **BMIELKE SETS the level rather than multiplying the shape's.** Both encode
# quality, and multiplying them counts a hitter's contact twice — which is
# §3d.7's recorded failure mode exactly (better correlation, worse RMSE). The
# premise of the gate is that below it BMIELKE beats his own batted balls, so
# below it BMIELKE is the level and the map is only the mix.
# Contact TYPE is far more stable than contact RATE — every ball contributes to
# it, which is why `CONTACT_MIN_BBE` is 25 against the 2,335 plate appearances
# the doubles RATE needs — so the shape survives a thin sample even where the
# rate does not. Nothing here is re-fitted: both pieces already exist and are
# already tested.
#
# **The two shrinkages are separate and must stay separate.** The SHAPE is
# noisy at 30 batted balls and is shrunk toward the population shape at
# `CONTACT_SHRINK_BBE`. The LEVEL is not shrunk here at all, because BMIELKE
# already shrinks it TWICE internally — his own prior season toward league by
# `n_prev/(n_prev+150)`, then this season toward that by `n/(n+150)`. Shrinking
# it a third time on a batted-ball count would erase the estimate at exactly
# the sample sizes it was built for: at 50 balls in play a second
# `n/(n+600)` weight keeps 7.7% of it.

# SCORED 2026-08-26 on the population the gate ADMITS (`mlb_sim.py bmielke`),
# incumbent = the shipped shrunk blend with the Triple-A ladder live:
#
#                     2025 (n=2,920)          2026 (n=2,527)
#                  incumbent  BMIELKE      incumbent  BMIELKE
#   RV corr          +0.4496  +0.4730        +0.2645  +0.2934
#   1B rmse          0.02392  0.02371        0.02562  0.02537
#   HR rmse          0.01354  0.01298        0.01375  0.01357
#   2B rmse          0.01225  0.01289        0.01460  0.01496
#   RV rmse          0.03113  0.03111        0.03316  0.03392
#
# **It RANKS hitters better in both seasons** — +0.023 and +0.029 of run-value
# correlation, on singles and home runs together — and the ENTIRE residual is
# DOUBLES. That is not a coincidence: `STABILIZE_PA_BAT[2B]` is 2,335 and a
# hitter's own doubles rate correlates +0.05 with his future one, so league is
# very nearly the optimal point estimate for 2B and ANY term that moves it pays
# in RMSE. The direction loads +0.13 to +0.17 there.
#
# So the honest reading: the ranking is real and the level is where the gain
# is (shape alone gets +0.4496 -> +0.4592; the level takes it to +0.4730), and
# the term is not yet RMSE-neutral. **Named next step**: attenuate the
# direction per outcome by that outcome's own predictability, the way
# `STUFF_RELIABILITY` attenuates the stuff delta — 2B would go to nearly zero
# by construction rather than by hand. Not built; do not ship this without it
# or without a ladder result that survives the 2B cost.
#
# **AUDIT IT PER SLATE — `mlb_sim.py bmaudit`.** `bmielke()` mixes two KINDS of
# evidence and only one of them makes a contact prior admissible. The SWING
# block (`fastsw`, `whiff`, `aa`, `depth`) is disjoint from the outcomes the
# rate layer is shrinking; `wshrunk` and `evmax` are the hitter's OWN contact,
# which the rate layer is already shrinking, so a reading driven by that half
# is partly asking a hitter's results to vouch for themselves. `wshrunk`
# carries weight n/(n+150), so the OWN half grows with balls in play BY
# CONSTRUCTION — which is why the top of the gate is where a boost most wants
# checking. On the 2026 board:
#
#   hitter              BBE   swing%   fast%   EV98
#   Zach Cole            30      81%     58%   111.8   earned
#   Spencer Jones        80      77%     71%   111.6   earned
#   Giancarlo Stanton    61      61%     85%   115.0   earned — biggest move
#   Griffin Conine      104      40%     42%   113.1   marginal
#   Aaron Judge         143      23%     59%   112.4   OWN CONTACT
#   Oneil Cruz          149      22%     70%   114.9   OWN CONTACT
#
# 48 of 275 gated hitters move more than 0.010 runs/PA on a reading the swing
# does not mostly back. `Bm.bmielke_support` is the decomposition.
#
# wOBA weights, linear-weights scale. Only RATIOS of these matter, because
# every vector they touch is renormalised.
WOBA_W = {"1B": 0.883, "2B": 1.244, "3B": 1.569, "HR": 2.004}
BMIELKE_LG_WOBACON_FALLBACK = 0.3807


def league_wobacon(rates: Sequence[float]) -> float:
    """Expected wOBA per ball in play, from a nine-outcome vector."""
    bip = sum(rates[i] for i in CONTACT_CLASSES)
    if bip <= 0:
        return BMIELKE_LG_WOBACON_FALLBACK
    return (WOBA_W["1B"] * rates[S1B] + WOBA_W["2B"] * rates[S2B]
            + WOBA_W["3B"] * rates[S3B] + WOBA_W["HR"] * rates[HR]) / bip


_BM_CACHE: Dict[tuple, Optional[dict]] = {}


def bmielke_asof(pid: int, season: int, as_of: Optional[str] = None,
                 save_dir: Path = SAVE_DIR) -> Optional[dict]:
    """BMIELKE v9 for one hitter, using only pitches STRICTLY BEFORE `as_of`.

    The metric itself is `bmielke_core.bmielke` and is NOT re-implemented or
    re-fitted here — EffortMLB renders the same function, and a second copy of
    a fitted model is a divergence waiting to happen. This wrapper does two
    things the engine needs and the chip does not: it cuts the pitch list at a
    date, and it supplies LAST season's xwOBAcon as the player prior.

    That prior is what makes the early-season read work (v9's largest single
    gain) and it leaks nothing: the season before finished before any game
    being priced. Omitting it degrades gracefully to the league prior, which is
    exactly what v8 did.
    """
    key = (int(pid), int(season), as_of or "")
    if key in _BM_CACHE:
        return _BM_CACHE[key]
    rows = fetch_bmielke_detail(pid, season, save_dir)
    if as_of:
        rows = [r for r in rows if r.get("date") and r["date"] < as_of]
    prior_w = prior_n = None
    prev = fetch_bmielke_detail(pid, season - 1, save_dir)
    if prev:
        # BALLS IN PLAY only, matching what `bmielke()` averages this season.
        # An exit velocity alone is not enough: fouls carry one too, and
        # counting them dragged xwOBAcon from .38 to .28.
        xw = [r["xwoba"] for r in prev
              if r.get("ev") is not None and r.get("hc_x") is not None
              and r.get("xwoba") is not None]
        if xw:
            prior_w, prior_n = sum(xw) / len(xw), len(xw)
    out = bmielke_core.bmielke(rows, prior_w, prior_n)
    _BM_CACHE[key] = out
    return out


# **THE SIM'S GATE IS TIGHTER THAN THE METRIC'S OWN CROSSOVER, and the two are
# answering different questions.** `bmielke_core.BMIELKE_MAX_BBE` (175) is where
# BMIELKE stops beating a hitter's own xwOBAcon — the right line for the chip,
# which is asked "how good is his contact". The engine's incumbent is not his
# xwOBAcon: it is `shrink_rates`' blend of his realised outcomes, a different and
# noisier quantity, so the line moves.
#
# MEASURED 2026-08-27, each gate scored against ITS OWN gated population's
# incumbent (the population grows with the gate, so raw levels do not compare):
#
#            2025                    2026
#   gate   d(RVrmse)  d(RVcorr)    d(RVrmse)  d(RVcorr)
#     80    -0.00047    +0.0603     +0.00075    +0.0495
#    100    -0.00058    +0.0495     +0.00078    +0.0468
#    120    -0.00045    +0.0393     +0.00105    +0.0366
#    150    -0.00030    +0.0289     +0.00122    +0.0326
#    175    -0.00030    +0.0286     +0.00127    +0.0266
#
# **Monotone in both seasons on both measures: 175 was the worst value tested.**
# The mechanism is in `Bm.bmielke_support` — `wshrunk` carries weight n/(n+150),
# so the share of the reading that is the hitter's OWN contact grows with balls
# in play, and past ~100 the prior is increasingly asking his results to vouch
# for themselves. Aaron Judge at 143 balls in play reads 23% swing-backed.
#
# **100, not the 80 argmin.** 80 and 100 are inside each other's noise (2026:
# +0.00075 against +0.00078, +0.0495 against +0.0468) and 100 covers 50% more
# hitters; picking the argmin of two seasons fits the noise between them, which
# is the same reasoning that put `STUFF_SHRINK_TBF` at the conservative end of
# its flat region. `BMIELKE_MIN_BBE` (25) is the floor `bmielke()` enforces.
BMIELKE_GATE_BBE = 100
# The metric's own crossover, kept so the divergence above is explicit rather
# than accidental — if this moves, the note above needs re-measuring.
BMIELKE_MAX_BBE = bmielke_core.BMIELKE_MAX_BBE
# How far a reading is allowed to move the level, blending from the SHAPE's own
# implied quality (0.0) to BMIELKE's assertion in full (1.0).
#
# **0.83 is measured, not a safety rail.** BMIELKE is DESCRIPTIVE of the swings
# already taken and the prior wants a FORECAST — the same argument that puts
# `CHED_PRIOR_SCALE` at 0.787. Regressing what a hitter's contact ACTUALLY
# became after a cutoff on the level asserted at it, one unit of asserted level
# buys 0.918 of a unit in 2025 and 0.830 in 2026. The MINIMUM ships, per this
# file's standing rule that over-trusting a new term is its demonstrated
# failure mode. `AB_ARMS["bmielke-full"]` is the other end.
#
# This is NOT a third shrinkage of the metric. `bmielke()` shrinks twice
# internally — his prior season toward league, then this season toward that —
# and both are inside the number this attenuates. What is being corrected here
# is the descriptive-to-forecast gap, which is a different quantity and is
# measured against a different target.
BMIELKE_PRIOR_SCALE = 0.83
# What BMIELKE's relative reading is applied TO. Three anchors, and the two
# obvious ones are each wrong in a different direction — this constant exists
# because BOTH failures were found by measurement, a day apart.
#
# **"league" OVERRIDES the prior underneath.** `level` is centred on the gated
# population, so a reading of 1.00 means "an average fringe bat", and against a
# league anchor that PULLS UP any hitter the rate layer had below it. Osleivis
# Basabe reads level 1.002 with 28% fast swings, a 106.8 mph EV98 and a .3243
# xwOBAcon and still gained +0.024 runs/PA, because `milb_prior` had marked him
# down and the league anchor discarded it. The Triple-A ladder and this prior
# OVERLAP in the gated band — they were documented here as handing off cleanly
# and they do not — and a league anchor makes BMIELKE win by construction.
#
# **"shaped" COMPOUNDS with the contact map.** Anchoring on the wOBAcon AFTER
# the shape step makes the level a multiplier on the map's own opinion of his
# quality, so a slugger profile and a high reading multiply: a 0.4949 shape at
# level 1.25 lands at 0.5699 against a league 0.3572. Both terms estimate the
# same thing and multiplying them is §3d.7's failure — better correlation,
# worse RMSE — in a new costume. It also collapses the whole level step to
# `f = 1 + (level-1) * BMIELKE_PRIOR_SCALE`, i.e. §3d.6's scalar, and leaves
# `lg_w` computed and unused on a per-hitter hot path. **Shipped for a few
# hours on 2026-08-27 before that was noticed.**
#
# **"prior" (SHIPPED) anchors on the level he ARRIVED with** — `milb_prior` and
# the playing-time curve, read BEFORE the shape step. The Triple-A markdown
# survives and the map is left to do the one job it is good at: the MIX. It is
# the only anchor under which this file's own claim is actually TRUE — the
# shape picks what his contact becomes, BMIELKE says how good it is.
#
# SCORED at the shipped gate, each against the same incumbent (2026-08-27):
#
#              2025 (n=1,382)          2026 (n=1,201)
#   anchor   d(RVrmse)  d(RVcorr)    d(RVrmse)  d(RVcorr)
#   prior     -0.00078    +0.0510     +0.00051    +0.0456
#   shaped    -0.00059    +0.0495     +0.00078    +0.0468
#   league    +0.00042    +0.0401     +0.00011    +0.0518
#
# **"prior" beats "shaped" on RMSE in BOTH seasons**, so removing the
# compounding is a real improvement and not only a tidier story. "prior"
# against "league" is a WASH that splits by season, and is decided by the
# structural argument above rather than by these numbers.
#
# Arms `bmielke-lgbase` and `bmielke-shaped` are the other two.
BMIELKE_LEVEL_BASE = "prior"
# **LIVE 2026-08-27.** Shipped on rate-layer evidence and judgement, exactly as
# `USE_CHED_PRIOR` was and with more behind it: CHED ships having never been
# scored against the close at all, while this has been and came back a NULL
# rather than a negative (4o). What earns it:
#
#   * it beats the incumbent at forecasting a hitter on BMIELKE's OWN validated
#     target in both seasons (+0.0639, +0.0504), and by +0.044 to +0.064 across
#     all six target x season cells — positive in 20 of 20 configurations tried;
#   * it is GATED to 25-100 balls in play, where the metric is validated to beat
#     a hitter's own xwOBAcon, unlike its two predecessors which ran everywhere;
#   * on the differential ladder it moves every rung by <= 0.10 of t, takes
#     rungs past |t|=2 from 5 to 4 and the calibration slope from +1.0338 to
#     +1.0188. Nothing significant in either direction.
#
# **It has NOT been scored positive against the close, and this line is the
# place that says so.** §3d.6 lost the moneyline at t -2.62 and §3d.7 at -1.00;
# this one does not lose, which is the difference the gate, the measured
# direction, the `prior` anchor and `hold_bip_rate` bought.
#
# **The cache must be FRESH or this is worse than off.** `bmielke_asof` reads
# cache-only inside the pool, so a stale file degrades silently — and balls in
# play only accumulate, so staleness drags hitters across the gate in one
# direction. On the day this shipped every gated hitter was being scored on
# detail 12 days old. Run before a slate:
#     Bmielke.fetch_bmielke_season(pids, season, max_age_days=1)
# Uppercase, so `_slate_overrides` ships it to the pool workers.
USE_BMIELKE_PRIOR = True


def bmielke_levels(pids: Sequence[int], season: int,
                   as_of: Optional[str] = None,
                   save_dir: Path = SAVE_DIR) -> Dict[int, Tuple[float, int]]:
    """{pid: (relative contact level, balls in play)} for hitters INSIDE the gate.

    1.0 is the applied population's average, and the three things that had to be
    right here were all got wrong on the first attempt (§3d.6), every one of them
    in the direction that quietly moves the league run level:

    1. **Use `raw`, not `wobacon`.** `bmielke()` returns both — the model's
       PREDICTION and the hitter's observed xwOBAcon. The validation scored
       `raw` (+0.70) and the first wiring shipped `wobacon` (+0.56). Validate one
       thing and ship another and the measurement means nothing.
    2. **Divide by `_bmielke_ref(n)`, not by a constant.** `raw` sits on the
       model's own scale, which VARIES WITH `n` by design — the model produces a
       tighter spread when it has less to go on, so a single reference makes a
       30-BBE and a 400-BBE hitter incommensurable. Using 0.3807 put the
       population at 0.955, every hitter 4.5% below league, and cost 0.146 runs
       a game.
    3. **CENTRE on the population it is applied to.** Even correctly referenced
       the lineup population reads 0.975, because the reference is anchored on
       2025 regulars. Uncentred, that is a league-wide tilt wearing the clothes
       of a player adjustment.

    Centring is over the PLAYERS, unweighted, and that is deliberate here rather
    than copied: the gate admits only thin-sample hitters, so there is no
    regular-versus-part-timer weighting question to get wrong — every member of
    this population is a fringe bat, and the quantity to neutralise is the
    average tilt handed to one of them.
    """
    rel: Dict[int, Tuple[float, int]] = {}
    for pid in pids:
        bm = bmielke_asof(int(pid), season, as_of, save_dir)
        if not bm:
            continue
        n = int(bm["bbe"])
        # **THE GATE.** Above it his own xwOBAcon is the better estimate and the
        # rate layer already carries it; below `BMIELKE_MIN_BBE` the metric
        # declines to return anything and `milb_prior` has him.
        if n > BMIELKE_GATE_BBE:
            continue
        ref_mean, _ = bmielke_core._bmielke_ref(n)
        if ref_mean > 0:
            rel[int(pid)] = (bm["raw"] / ref_mean, n)
    if not rel:
        return {}
    centre = statistics.mean(v for v, _ in rel.values())
    if centre <= 0:
        return {}
    return {pid: (v / centre, n) for pid, (v, n) in rel.items()}


_CQDIR_CACHE: Dict[tuple, Optional[List[float]]] = {}


def contact_quality_direction(season: int, save_dir: Path = SAVE_DIR
                              ) -> Optional[List[float]]:
    """d(FUTURE class share) / d(BMIELKE level) — where the metric's signal goes.

    **The single most important number in §17e, and assuming it is what cost the
    first two attempts their result.** §3d.6 spread a contact-quality estimate
    PROPORTIONALLY across 1B/2B/3B/HR. Per unit of wOBA on contact, that
    assumption reads +0.213 on singles. Measured, the answer is NEGATIVE:

        class     PROPORTIONAL   descriptive   FORECAST 2025   FORECAST 2026
        1B            +0.213        -0.003        -0.095          -0.087
        2B            +0.062        +0.155        +0.086          +0.072
        3B            +0.006        +0.015        -0.001          -0.003
        HR            +0.046        +0.392        +0.448          +0.410
        GB_OUT        -0.150        -0.479        -0.233          -0.247
        AIR_OUT       -0.164        -0.081        -0.204          -0.146

    **Better contact turns GROUND BALLS into HOME RUNS. It produces FEWER
    singles, not more.** That is what a hitter with more bat speed and a
    steeper attack angle does, and it is exactly what BMIELKE measures — so
    spreading its level proportionally puts the signal on the one outcome that
    carries none of it, with the wrong sign. Scored on the gated population the
    proportional version moved 1B RMSE 0.02392 -> 0.02568 while IMPROVING home
    runs, which is this table showing up as an error.

    **FORECAST, not descriptive, and the distinction is worth two columns
    above.** The descriptive direction regresses a hitter's CURRENT mix on his
    CURRENT wOBAcon; the prior needs how his FUTURE mix moves. They differ
    systematically — descriptive over-moves DOUBLES by about 2x, which is
    `STABILIZE_PA_BAT[2B] = 2335` arriving from another direction, and
    understates the singles effect by an order of magnitude. Same argument that
    puts `CHED_PRIOR_SCALE` at 0.787 rather than 1.0.

    Two properties are CHECKED rather than assumed, because a direction that
    fails either would move the league run level: `sum(direction) == 0` over the
    contact classes (in-play mass is conserved) and `sum(w_i * direction_i)`
    is recorded as the attenuation (0.918 in 2025, 0.830 in 2026) and folded
    into the step, so one unit of asserted level moves wOBAcon by one unit.

    Fitted on seasons STRICTLY EARLIER than `season`, the same rule as
    `Contact.contact_map_for` and `stuff_model_for`.
    """
    key = (int(season), str(save_dir))
    if key in _CQDIR_CACHE:
        return _CQDIR_CACHE[key]
    xs: List[float] = []
    ys: List[List[float]] = []
    for yr in range(season - 2, season):
        try:
            rows = Bm.bmielke_future_rows(yr, save_dir)
        except (OSError, ValueError):
            continue
        if not rows:
            continue
        by_cut: Dict[str, List[dict]] = {}
        for r in rows:
            by_cut.setdefault(r["cutoff"], []).append(r)
        for cut, group in by_cut.items():
            lv = bmielke_levels([r["pid"] for r in group], yr, cut, save_dir)
            for r in group:
                got = lv.get(r["pid"])
                if not got:
                    continue
                bip = sum(r["post"][i] for i in CONTACT_CLASSES)
                if bip <= 0:
                    continue
                xs.append(got[0])
                ys.append([r["post"][i] / bip if i in CONTACT_CLASSES else 0.0
                           for i in range(N_OUTCOMES)])
    out: Optional[List[float]] = None
    if len(xs) >= 200:
        mx = statistics.mean(xs)
        sxx = sum((a - mx) ** 2 for a in xs)
        if sxx > 0:
            d = [0.0] * N_OUTCOMES
            for i in CONTACT_CLASSES:
                my = statistics.mean(y[i] for y in ys)
                d[i] = sum((a - mx) * (y[i] - my)
                           for a, y in zip(xs, ys)) / sxx
            # **Re-centre so mass is conserved EXACTLY.** Six independent
            # regressions need not sum to zero, and a residual of even 1e-3
            # would leak in-play mass into K/BB/HBP on renormalisation — a
            # league-wide rate shift arriving as rounding. Absorbed in the two
            # OUT classes, which is where the model has the least to say.
            resid = sum(d[i] for i in CONTACT_CLASSES)
            for i in (GB_OUT, AIR_OUT):
                d[i] -= resid / 2.0
            # normalise so one unit of asserted level moves wOBAcon by one
            # unit; the raw slope carries the 0.83-0.92 forecast attenuation,
            # which belongs in `BMIELKE_PRIOR_SCALE` and not in the shape
            gain = (WOBA_W["1B"] * d[S1B] + WOBA_W["2B"] * d[S2B]
                    + WOBA_W["3B"] * d[S3B] + WOBA_W["HR"] * d[HR])
            if gain > 1e-6:
                d = [v / gain for v in d]
                out = d
    _CQDIR_CACHE[key] = out
    return out


def hold_bip_rate(rates: Sequence[float],
                  reference: Sequence[float]) -> List[float]:
    """`rates`, with the hitter's contact FREQUENCY taken from `reference`.

    **A contact-quality term must not change how often a hitter puts the ball
    in play.** This covers BOTH §17e's level and §3d.7's map
    (`USE_CONTACT_PRIOR`), which redistribute the in-play block identically and
    therefore leak identically. **§3d.7 was measured at moneyline paired
    t -1.00 with this defect present** — not grounds to re-open a killed
    hypothesis, but worth knowing before anyone quotes that number again. It has nothing to say about that — the whole term
    is built from what happens once he makes contact — and `bmielke_prior`
    honours this by leaving K, BB and HBP untouched and conserving the in-play
    mass of the PRIOR.

    That is not sufficient, and believing it was hid a real defect for a day.
    `shrink_rates` blends per outcome at `w_i = n/(n+stabilize[i])` and THEN
    normalises, so what reaches the divisor is the (1-w)-WEIGHTED prior mass.
    The level term moves mass from GB_OUT (stab 111) and AIR_OUT (132) toward
    HR (244) and 2B (2335) — UP the stabilisation ladder, where the prior is
    trusted more — so every unit arrives multiplied by a larger (1-w) than it
    left with, the divisor inflates, and normalisation takes the difference out
    of K, BB and HBP.

    Measured on 159 gated hitters BEFORE this: **corr(BMIELKE level, dK) =
    -0.939**, corr(level, d BIP rate) = +0.943, up to 1.11% of a league
    strikeout rate. Every hitter the metric liked struck out less for no reason
    but arithmetic. It survived the level-neutrality test because the
    population mean was -0.00006 — **a per-player bias that cancels in
    aggregate is exactly the shape an aggregate cannot see.**

    Two fixes were tried and rejected before this one. Rescaling the in-play
    block to conserve the weighted mass is undone by the closing `_normalize`.
    Projecting the direction orthogonal to the shrink weights fixes the LEVEL
    step but not the SHAPE step, cut the leak only 5.5x, and cost a third of
    the home-run delivery — and projecting the shape step too would distort the
    very profile the contact map measured.

    So it is restored here instead, exactly and after the fact: K, BB and HBP
    come from the run WITHOUT the contact prior, and the in-play block keeps
    the shape the contact prior gave it, rescaled to fill what is left. The
    invariant is then a sentence rather than an approximation — **this term
    changes what a hitter's contact BECOMES, never how often he makes it.**
    """
    non = (reference[K] + reference[BB] + reference[HBP])
    bip = sum(rates[i] for i in CONTACT_CLASSES)
    if bip <= 0 or non >= 1.0:
        return list(rates)
    scale = (1.0 - non) / bip
    out = list(rates)
    out[K], out[BB], out[HBP] = reference[K], reference[BB], reference[HBP]
    for i in CONTACT_CLASSES:
        out[i] = rates[i] * scale
    return out


def bmielke_prior(prior: Sequence[float], shape: Sequence[float],
                  n_bbe: int, lg_shape: Sequence[float], level: float,
                  direction: Optional[Sequence[float]] = None
                  ) -> List[float]:
    """`prior`, with its in-play block re-shaped by his contact and re-levelled
    by BMIELKE.

    Two inputs doing two different jobs:

    * `shape` is his contact-map profile (§17d) — WHAT his batted balls become,
      shrunk toward `lg_shape` at `CONTACT_SHRINK_BBE` because a 30-ball profile
      is noisy. This is what tells a singles hitter from a slugger and it is the
      half §3d.6 did not have.
    * `level` is BMIELKE's relative contact quality, already centred on the
      applied population by `bmielke_levels` and already shrunk twice inside the
      metric. `BMIELKE_PRIOR_SCALE` (0.83) then damps it once more for the
      DESCRIPTIVE-to-FORECAST gap, which is a different quantity from either of
      those two shrinkages and is measured against a different target.
      What it is applied TO is `BMIELKE_LEVEL_BASE` — the prior he already had,
      not league, so a Triple-A markdown survives underneath it.
    * `direction` is d(share)/d(wOBAcon) measured over real hitters. The level
      moves the mix ALONG it, not proportionally — better contact turns ground
      balls into home runs and makes almost no extra singles.

    **`level` must be a RATIO and can never be an absolute xwOBAcon.** Savant's
    scale (0.3807) and this baseline's wOBA-on-contact (~0.3575) are different
    quantities, and passing one as the other reads every hitter as 6.5% better
    than league — a run-level shift disguised as a player adjustment.

    K, BB and HBP are untouched: a contact model has nothing to say about them,
    they already stabilise correctly, and there is everything to break. The
    in-play MASS is held exactly constant, so this cannot move a hitter's
    contact RATE — only what his contact turns into, and how well.
    """
    bip = sum(prior[i] for i in CONTACT_CLASSES)
    if bip <= 0:
        return list(prior)
    # The level he ARRIVES with — `milb_prior`'s Triple-A translation and the
    # playing-time curve — read BEFORE the shape step touches the mix. Taking
    # it here rather than after is the whole difference between REFINING his
    # level and COMPOUNDING with the contact map's own opinion of it.
    pre_w = ((WOBA_W["1B"] * prior[S1B] + WOBA_W["2B"] * prior[S2B]
              + WOBA_W["3B"] * prior[S3B] + WOBA_W["HR"] * prior[HR]) / bip)

    # --- 1. the SHAPE, shrunk toward the population it is compared against ---
    w = n_bbe / (n_bbe + CONTACT_SHRINK_BBE)
    shaped = []
    for i in CONTACT_CLASSES:
        base = lg_shape[i] if i < len(lg_shape) else 0.0
        r = (shape[i] / base) if base > 0 else 1.0
        shaped.append(prior[i] * (1.0 + (r - 1.0) * w))
    tot = sum(shaped)
    if tot <= 0:
        return list(prior)
    out = list(prior)
    for i, v in zip(CONTACT_CLASSES, shaped):
        out[i] = v * bip / tot

    # --- 2. the LEVEL, from BMIELKE, moved along the MEASURED direction -----
    # `direction` is d(share)/d(wOBAcon) over real hitters
    # (`contact_quality_direction`). Without it there is nothing to do: the
    # proportional fallback is the §3d.6 wiring and is reachable only as
    # `AB_ARMS["bmielke-flat"]`, never by default.
    if direction is None:
        return _normalize(out)
    bip = sum(out[i] for i in CONTACT_CLASSES)
    if bip <= 0:
        return _normalize(out)
    sh = {i: out[i] / bip for i in CONTACT_CLASSES}
    now = (WOBA_W["1B"] * sh[S1B] + WOBA_W["2B"] * sh[S2B]
           + WOBA_W["3B"] * sh[S3B] + WOBA_W["HR"] * sh[HR])
    # **What the level is applied TO decides whether this REFINES the prior or
    # COMPOUNDS with the shape**, and the two obvious anchors each fail one of
    # those. `BMIELKE_LEVEL_BASE` carries the full argument and the numbers;
    # the short version is that "prior" — the level he arrived with, read
    # before the shape step — is the only one that keeps a Triple-A markdown
    # AND leaves the map to pick only the mix.
    #
    # **Resolved lazily: only the "league" arm needs the league wOBAcon.** This
    # runs once per hitter per rate build, and computing it unconditionally
    # left six float ops dead on the shipped path.
    if BMIELKE_LEVEL_BASE == "league":
        lg_bip = sum(lg_shape[i] for i in CONTACT_CLASSES)
        if lg_bip <= 0:
            return _normalize(out)
        anchor = (WOBA_W["1B"] * lg_shape[S1B] + WOBA_W["2B"] * lg_shape[S2B]
                  + WOBA_W["3B"] * lg_shape[S3B]
                  + WOBA_W["HR"] * lg_shape[HR]) / lg_bip
    else:
        anchor = now if BMIELKE_LEVEL_BASE == "shaped" else pre_w
    delta = (anchor * float(level) - now) * BMIELKE_PRIOR_SCALE
    step = {i: direction[i] * delta for i in CONTACT_CLASSES}
    moved = {i: sh[i] + step[i] for i in CONTACT_CLASSES}
    # **A share argued below zero means the step is longer than the simplex
    # allows — scale the WHOLE step back, never clip one class.** Clipping
    # would break both invariants the direction is built on at once: the
    # clipped class no longer sums to zero with the others (mass leaks into
    # K/BB/HBP on renormalisation) and the wOBAcon gain is no longer one, so
    # the level actually applied would silently differ from the level asserted.
    # `lim` is the longest fraction of the step every class survives.
    if any(v < 0.0 for v in moved.values()):
        lim = min(sh[i] / -step[i] for i in CONTACT_CLASSES
                  if step[i] < -1e-15)
        moved = {i: sh[i] + step[i] * lim * 0.99 for i in CONTACT_CLASSES}
    for i in CONTACT_CLASSES:
        out[i] = max(moved[i], 0.0) * bip

    # **Conserving the prior's in-play MASS is not enough, and believing it was
    # let a contact-QUALITY prior change contact FREQUENCY.** `shrink_rates`
    # blends per outcome, `out[i] = w_i*obs_i + (1-w_i)*prior_i`, and THEN
    # normalises. So what reaches the divisor is the (1-w)-WEIGHTED prior mass,
    # not the raw one — and the direction moves mass from GB_OUT (stab 111) and
    # AIR_OUT (132) toward HR (244) and 2B (2335), i.e. UP the stabilisation
    # ladder, where the prior is trusted more. Every unit moved arrives
    # multiplied by a larger (1-w) than it left with, the sum inflates, and
    # `_normalize` takes the difference out of K, BB and HBP.
    #
    # Measured before this correction, on 159 gated hitters:
    # **corr(BMIELKE level, dK) = -0.939** and corr(level, d BIP rate) = +0.943.
    # A hitter the metric liked struck out LESS for no reason but arithmetic —
    # up to 1.11% of a league strikeout rate. It survived the level-neutrality
    # test because the population mean was -0.00006: a PER-PLAYER bias that
    # cancels in aggregate is exactly the shape aggregates cannot see.
    #
    # Rescaling the in-play block so the (1-w)-weighted sum is unchanged leaves
    # `shrink_rates`' divisor untouched, so K/BB/HBP come out exactly where
    # they went in. `shrink_w` is None only in tests that call this directly.
    return _normalize(out)


class Bm:
    """BMIELKE's engine-side harnesses — gathering, scoring, and auditing it."""

    @staticmethod
    def bmielke_support(pid: int, season: int, as_of: Optional[str] = None,
                        save_dir: Path = SAVE_DIR) -> Optional[dict]:
        """Is a hitter's reading backed by his SWING, or by his own contact?

        **The audit that says whether a boost is earned.** `bmielke()` is six
        features and they are not the same KIND of evidence:

          * SWING  — `fastsw`, `whiff`, `aa`, `depth`. Measured on swings,
            disjoint from the outcomes the rate layer is shrinking. This is the
            evidence that makes a contact prior admissible at all (§17e).
          * OWN    — `wshrunk` (his own xwOBAcon, shrunk) and `evmax` (his own
            hardest contact). Real signal, but the rate layer is ALREADY
            shrinking those same batted balls, so a reading driven by this half
            is partly asking a hitter's own results to vouch for themselves.

        `wshrunk` carries weight `n/(n+150)`, so the OWN half grows with balls
        in play by construction — which is one reason the gate exists and why
        the top of the gate is where a boost most wants checking.

        Returns per-feature contributions to `raw` plus `swing_share`, the
        fraction of the total deviation the swing block accounts for. Measured
        on the 2026 board: Giancarlo Stanton 61%, Zach Cole 81%, Spencer Jones
        77% — earned; Aaron Judge 23% and Oneil Cruz 22% at 143 and 149 balls
        in play — mostly their own hot expected contact.
        """
        rows = fetch_bmielke_detail(int(pid), season, save_dir)
        if as_of:
            rows = [r for r in rows if r.get("date") and r["date"] < as_of]
        prev = fetch_bmielke_detail(int(pid), season - 1, save_dir)
        prior_w = prior_n = None
        if prev:
            xw = [r["xwoba"] for r in prev
                  if r.get("ev") is not None and r.get("hc_x") is not None
                  and r.get("xwoba") is not None]
            if xw:
                prior_w, prior_n = sum(xw) / len(xw), len(xw)
        bm = bmielke_core.bmielke(rows, prior_w, prior_n)
        if not bm:
            return None
        # Rebuild the feature vector exactly as `bmielke()` does. Values come
        # back through its own return dict where possible so the two cannot
        # drift on the parts it already exposes.
        ev, bat, aa, dep = [], [], [], []
        swings = whiffs = 0
        for r in rows:
            d = r.get("desc") or ""
            if d in bmielke_core._SWING_DESCS:
                swings += 1
                whiffs += d in bmielke_core._WHIFF_DESCS
                if r.get("attack_angle") is not None:
                    aa.append(r["attack_angle"])
                if r.get("icept_y") is not None:
                    dep.append(r["icept_y"])
            if r.get("bat_speed") is not None:
                bat.append(r["bat_speed"])
            if (r.get("ev") is not None and r.get("hc_x") is not None
                    and r.get("hc_y") is not None):
                ev.append(r["ev"])
        if not ev or not bat or not aa or not dep or not swings:
            return None
        n = len(ev)
        srt = sorted(ev)
        pos = 0.98 * (n - 1)
        lo = int(pos)
        evmax = srt[lo] + (pos - lo) * (srt[min(lo + 1, n - 1)] - srt[lo])
        vals = (sum(1 for b in bat if b >= 75.0) / len(bat), evmax,
                whiffs / swings, bm["prior"] + (bm["wobacon"] - bm["prior"])
                * bm["outcome_weight"], sum(aa) / len(aa), sum(dep) / len(dep))
        wc = bm["outcome_weight"] - bmielke_core.BMIELKE_WCENTRE
        names = ("fastsw", "evmax", "whiff", "wshrunk", "aa", "depth")
        contrib = {k: a * ((v - mu) / sd) + b * ((v * wc - mw) / sw)
                   for k, v, (a, b, mu, sd, mw, sw)
                   in zip(names, vals, bmielke_core.BMIELKE_COEF)}
        swing = sum(contrib[k] for k in ("fastsw", "whiff", "aa", "depth"))
        own = contrib["evmax"] + contrib["wshrunk"]
        tot = abs(swing) + abs(own)
        return {"contrib": contrib, "swing": swing, "own": own,
                "swing_share": (abs(swing) / tot) if tot > 0 else 0.0,
                "bbe": bm["bbe"], "index": bm["index"],
                "fastsw": vals[0], "evmax": evmax, "wobacon": bm["wobacon"]}

    @staticmethod
    def bmielke_profiles(pids: Sequence[int], season: int,
                         as_of: Optional[str] = None,
                         save_dir: Path = SAVE_DIR
                         ) -> Tuple[Dict[int, Tuple[List[float], int, float]],
                                    List[float]]:
        """{pid: (shape, n_bbe, level)} and the population's own shape.

        Joins §17d's contact map to BMIELKE's level, keeping only hitters who
        have BOTH — a shape with no level would be §3d.7 again, and a level with
        no shape would be §3d.6 again.
        """
        lv = bmielke_levels(pids, season, as_of, save_dir)
        if not lv:
            return {}, []
        cmap = Contact.contact_map_for(season, save_dir)
        if cmap is None:
            return {}, []
        out: Dict[int, Tuple[List[float], int, float]] = {}
        for pid, (level, _n) in lv.items():
            rows = fetch_bmielke_detail(int(pid), season, save_dir)
            if not rows:
                continue
            got = Contact.hitter_contact_profile(rows, cmap, as_of)
            if got is None:
                continue
            out[int(pid)] = (got[0], got[1], level)
        if not out:
            return {}, []
        # **The population shape is taken over EVERY hitter the map can see, not
        # just the gated ones.** It is the league's contact that a hitter is
        # being compared against, and the gated population is fringe bats by
        # construction — centring their shape on their own mean would define
        # away the very thing being measured.
        allprof, lg = Contact.contact_profiles(pids, season, as_of, save_dir)
        if not lg:
            return {}, []
        return out, lg

    @staticmethod
    def score_bmielke_prior(season: Optional[int] = None,
                            save_dir: Path = SAVE_DIR,
                            min_pre: float = 40.0, min_post: float = 100.0
                            ) -> dict:
        """Predict each hitter's FUTURE rates. Incumbent named, then beaten or not.

        **The incumbent is `build_rates_asof` as it ships** — the shrunk blend
        at the measured `STABILIZE_PA_BAT`, with the season rebasing and every
        prior currently live, the Triple-A ladder included. Not league and not
        his raw observed line: §3d.6 measured against both of those first and
        the win shrank from 28% to 16% once the real incumbent was named.

        `min_pre` is 40 plate appearances, not the 100 the pitcher harness uses.
        **This prior is gated to thin bats and scoring it on a 100-PA floor
        would measure it mostly outside its own regime** — which is the mistake
        §3d.6 made in the model rather than in the harness.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rows = Bm.bmielke_future_rows(season, save_dir, min_pre, min_post)
        if not rows:
            return {"n": 0, "season": season}
        by_cut: Dict[str, List[dict]] = {}
        for r in rows:
            by_cut.setdefault(r["cutoff"], []).append(r)
        global USE_BMIELKE_PRIOR
        was = USE_BMIELKE_PRIOR
        preds: Dict[str, List[List[float]]] = {k: [] for k in
                                               ("league", "own", "incumbent",
                                                "bmielke")}
        actual: List[List[float]] = []
        weights: List[float] = []
        gated: List[bool] = []
        try:
            for cut, group in sorted(by_cut.items()):
                USE_BMIELKE_PRIOR = False
                base, lg = build_rates_asof("bat", season, cut,
                                            save_dir=save_dir)
                USE_BMIELKE_PRIOR = True
                new, _ = build_rates_asof("bat", season, cut,
                                          save_dir=save_dir)
                for r in group:
                    pid = r["pid"]
                    if pid not in base or pid not in new:
                        continue
                    preds["league"].append(list(lg))
                    preds["own"].append(r["pre"])
                    preds["incumbent"].append(base[pid]["rates"])
                    preds["bmielke"].append(new[pid]["rates"])
                    actual.append(r["post"])
                    weights.append(r["n_post"])
                    gated.append(any(abs(a - b) > 1e-12 for a, b in
                                     zip(base[pid]["rates"],
                                         new[pid]["rates"])))
        finally:
            USE_BMIELKE_PRIOR = was

        out = {"n": len(actual), "season": season, "cutoffs": sorted(by_cut),
               "n_moved": sum(gated)}
        if len(actual) < 30:
            return out
        # **Scored twice: over everyone, and over the hitters the gate actually
        # ADMITTED.** Pooling them dilutes the term with rows it declined to
        # touch, and a diluted null is indistinguishable from a real one.
        for tag, keep in (("all", [True] * len(actual)), ("gated", gated)):
            sel = [i for i, k in enumerate(keep) if k]
            if len(sel) < 30:
                continue
            tot = sum(weights[i] for i in sel)
            block = {}
            for name, series in preds.items():
                rmse = []
                for i in range(N_OUTCOMES):
                    e = sum(weights[j] * (series[j][i] - actual[j][i]) ** 2
                            for j in sel)
                    rmse.append((e / tot) ** 0.5)
                rv_p = [rate_run_value(series[j]) for j in sel]
                rv_a = [rate_run_value(actual[j]) for j in sel]
                block[name] = {
                    "rmse": rmse,
                    "rv_rmse": (sum(weights[j] * (p - a) ** 2 for j, p, a
                                    in zip(sel, rv_p, rv_a)) / tot) ** 0.5,
                    "rv_corr": _corr(rv_p, rv_a),
                }
            block["n"] = len(sel)
            out[tag] = block
        return out

    @staticmethod
    def bmielke_future_rows(season: int, save_dir: Path = SAVE_DIR,
                            min_pre: float = 40.0, min_post: float = 100.0,
                            trim: int = 4) -> List[dict]:
        """Every (as-of line, what he did AFTER it) pair the season can supply.

        The bat-side twin of `Stuff.stuff_future_rows`, and differencing is the
        same trick: the season-final board minus an as-of one is the rest of
        that hitter's season, which is the only honest target for "does this
        predict him". `trim` drops the first and last few cutoffs — the earliest
        have no sample to estimate from and the latest have no future to score
        against.
        """
        full = {pid: r for r in (load_board("bat", season, save_dir) or [])
                if (pid := _row_id(r)) is not None}
        cuts = available_asof_cutoffs(season, save_dir)
        use = cuts[trim:len(cuts) - trim] if len(cuts) > 2 * trim else cuts
        out: List[dict] = []
        for cut in use:
            for row in (load_board_asof("bat", season, cut, save_dir) or []):
                pid = _row_id(row)
                if pid is None or pid not in full:
                    continue
                pre, n_pre = outcome_counts(row, "bat")
                if n_pre < min_pre:
                    continue
                post, n_all = outcome_counts(full[pid], "bat")
                n_post = n_all - n_pre
                if n_post < min_post:
                    continue
                out.append({
                    "pid": pid, "cutoff": cut,
                    "pre": [c / n_pre for c in pre], "n_pre": n_pre,
                    "post": [max(a - b, 0.0) / n_post
                             for a, b in zip(post, pre)], "n_post": n_post,
                })
        return out



# ===========================================================================
# 18. RUNNER ADVANCEMENT — per runner, from three sources
# ===========================================================================
# Taking the extra base mixes the runner's own history, his speed and his
# measured extra-base value, all three on disk: `runner_advance.json` (590
# runners), XBR (corr +0.517 with the observed rate, the best single predictor)
# and Spd (+0.462).
#
# **The raw per-runner rate is almost pure noise and must not be used
# directly** — median depth is 10 opportunities, and at a league 0.357 the
# binomial sd alone is 0.15 against an observed spread of 0.147, so essentially
# ALL the apparent spread is sampling. Regressing it properly is the whole job.

RUNNER_ADV_PATH = SAVE_DIR / "runner_advance.json"
# Opportunities at which a runner's own history is half-believed. From
# observed var 0.0216 = true var + binomial var(0.0115 at n~20) => true sd
# ~0.10, so k = p(1-p)/true_var = 0.36*0.64/0.0101 ~ 23.
STABILIZE_ADVANCE = 23.0
# Slope of advance rate on the standardised speed/extra-base composite,
# measured from the fast/slow split: (0.452-0.299) over ~2 sd = 0.077 per sd.
ADV_PER_SD = 0.077

_ADV: Optional[dict] = None


class RunnerAdvance:
    """Runner advancement, per runner, from three sources."""

    @staticmethod
    def load_runner_advance() -> dict:
        global _ADV
        if _ADV is None:
            try:
                with open(RUNNER_ADV_PATH) as fh:
                    _ADV = json.load(fh)
            except (OSError, ValueError):
                _ADV = {}
        return _ADV

    @staticmethod
    def runner_advance_rates(pid: Optional[int], xbr: float = 0.0,
                             spd: float = 4.13) -> Dict[str, float]:
        """This runner's advancement rates, blending history, XBR and speed."""
        lg = {"first_to_third": P_FIRST_TO_THIRD_ON_1B,
              "second_scores": P_SECOND_SCORES_ON_1B,
              "first_scores_2b": P_FIRST_SCORES_ON_2B}
        # Composite z: XBR is the better predictor, speed fills in when XBR is
        # thin. Board-wide sd: XBR 1.34, Spd 1.61.
        z = 0.6 * (xbr / 1.34) + 0.4 * ((spd - 4.13) / 1.61)
        prior = {k: min(max(v + ADV_PER_SD * z * (v / P_FIRST_TO_THIRD_ON_1B),
                            0.02), 0.95) for k, v in lg.items()}
        rec = RunnerAdvance.load_runner_advance().get(str(pid or ""), {})
        out = {}
        for key, tag in (("first_to_third", "1B_on1"),
                         ("second_scores", "1B_on2"),
                         ("first_scores_2b", "2B_on1")):
            n = float(rec.get(tag + "_n", 0))
            y = float(rec.get(tag + "_y", 0))
            w = n / (n + STABILIZE_ADVANCE)
            obs = (y / n) if n else prior[key]
            out[key] = w * obs + (1.0 - w) * prior[key]
        return out


# ===========================================================================
# 19. DEFENCE — team gloves and outfield arms
# ===========================================================================
# Two things the engine had NO representation of: the fielders behind the
# pitcher, and the arms that stop a runner taking the extra base — the out-vs-hit
# split came entirely from the batter's and pitcher's own rates, so a fly ball to
# a Gold Glove centre fielder and one to a statue were the same event.
#
# Sizing note: whole-team defence is about **0.2 runs per start** between the
# extremes. A real effect and a SMALL one — anything here that moves scoring by
# a run is wrong.



def _savant_csv(url: str, params: dict) -> List[dict]:
    r = requests.get(url, params=params, timeout=Savant.TIMEOUT)
    r.raise_for_status()
    text = r.text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def _savant_club(name: str, by_name: Dict[str, str]) -> Optional[str]:
    """Savant's `display_team_name` -> a board abbreviation, unambiguously.

    **This was a substring test and it corrupted every season it touched.**
    Savant sends SHORT names while the club index is keyed on full ones, so the
    old rule was `_norm_club(nm) in norm`, first match wins. Two collisions:
    `"---"`, Savant's placeholder for a player who changed clubs, normalises to
    the EMPTY STRING and matched all 30 (the whole of Oakland's -152 in 2024);
    and `"Reds"` is a substring of `"bostonredsox"`, so **Cincinnati's entire OAA
    was added to Boston and CIN disappeared from the file.**

    Neither failed loudly. The rule is now an exact match, else a UNIQUE suffix
    match, with the empty string and any ambiguity rejected outright.
    """
    key = _norm_club(name)
    if not key:
        return None
    if key in by_name:
        return by_name[key]
    hits = {a for norm, a in by_name.items() if norm.endswith(key)}
    return hits.pop() if len(hits) == 1 else None


def export_defense(season: Optional[int] = None) -> Path:
    """Team OAA and outfield arm strength -> MLBAnalytics CSV."""
    season = CURRENT_SEASON if season is None else int(season)
    idx = _team_index()
    by_name = {}
    for norm, rec in idx.items():
        by_name[norm] = rec["abbr"]

    oaa = _savant_csv(Savant.OAA_URL, {"type": "Fielder", "year": str(season),
                                       "csv": "true", "min": "10"})
    arm = _savant_csv(Savant.ARM_URL, {"type": "player", "year": str(season),
                                       "csv": "true"})

    team_oaa: Dict[str, float] = {}
    dropped: Dict[str, int] = {}
    for row in oaa:
        nm = (row.get("display_team_name") or "").strip()
        ab = _savant_club(nm, by_name)
        if not ab:
            dropped[nm or "(blank)"] = dropped.get(nm or "(blank)", 0) + 1
            continue
        try:
            team_oaa[ab] = team_oaa.get(ab, 0.0) + float(
                row.get("outs_above_average") or 0)
        except (TypeError, ValueError):
            continue

    # **A silent join failure here looks exactly like a real defensive
    # spread.** The old substring rule produced 29-31 "clubs" depending on the
    # season, so the count is checked rather than assumed.
    if len(team_oaa) != 30:
        raise RuntimeError(
            f"mlb_sim: team OAA resolved to {len(team_oaa)} clubs for {season}, "
            f"not 30. Unmatched display_team_name values: {dropped}. Refusing "
            f"to write a defence file that would silently mis-price a club.")

    # Outfield arm: mean across a club's outfielders.
    # **The arm board's `team_name` is the literal string "NA"** — it carries
    # no club at all, so it has to be joined by player id through the batting
    # board. Reading the team column returns one club for the whole league.
    pid_team: Dict[int, str] = {}
    for row in load_board("bat", season) or []:
        pid, tm = _row_id(row), row.get("TeamNameAbb")
        if pid and tm and "Tms" not in str(tm):
            # **Normalise, because the board carries the ERA-CORRECT spelling.**
            # Oakland is OAK on a 2024 board and ATH on a 2026 one, so an
            # un-normalised arm join invented a 31st club that had an outfield
            # arm and no OAA — and the OAA half of the same club had no arm.
            pid_team[pid] = normalize_club(str(tm))
    team_arm: Dict[str, List[float]] = {}
    for row in arm:
        if "field" not in (row.get("primary_position_name") or "").lower():
            continue
        try:
            pid = int(row.get("player_id") or 0)
            v = float(row.get("arm_overall") or row.get("max_arm_strength"))
        except (TypeError, ValueError):
            continue
        ab = pid_team.get(pid)
        if ab:
            team_arm.setdefault(ab, []).append(v)

    MLBA_DIR.mkdir(exist_ok=True)
    path = MLBA_DIR / f"team_defense_{season}.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["team", "season", "oaa", "of_arm"])
        w.writeheader()
        clubs = sorted(set(team_oaa) | set(team_arm))
        if len(clubs) != 30:
            raise RuntimeError(
                f"mlb_sim: defence file for {season} would carry {len(clubs)} "
                f"clubs, not 30 ({clubs}). The arm join spells clubs the way "
                f"the BOARD does for that era, so it must be normalised too.")
        for ab in clubs:
            arms = team_arm.get(ab) or []
            w.writerow({"team": ab, "season": season,
                        "oaa": round(team_oaa.get(ab, 0.0), 1),
                        "of_arm": round(sum(arms) / len(arms), 2) if arms else ""})
    print(f"[defense] wrote {path}")
    return path


_DEF: Dict[int, Dict[str, dict]] = {}          # keyed on SEASON



_FRAMING: Dict[int, Dict[str, float]] = {}     # keyed on SEASON
# Whether the 'no framing file for a lagged season' notice has been said.
# A dedicated flag, NOT `not _FRAMING`: the cache is cleared and
# repopulated by other code, so tying the notice to it made the notice
# fire again every time something else touched the cache.
_FRAMING_WARNED = False


def export_framing(season: Optional[int] = None) -> Path:
    """Per-club catcher framing runs -> MLBAnalytics/team_framing_<season>.csv.

    **One request per club, because the league-wide CSV is unusable**: its `id`
    and `name` columns come back EMPTY and there is no team column to join on.
    `type=Team` is silently ignored. Note `pitches` in that feed is FRAMING
    CHANCES (~66 per team-game), not total pitches — comparing it against a
    season's pitch count makes coverage look like 44% when it is complete.

    **This endpoint IGNORES `year` — verified, not assumed**: Toronto returns the
    same rv_tot for 2023-2026 byte for byte. Writing a "prior season" file would
    put the CURRENT season on disk under last year's name, so it raises instead.
    The thorough route is `statcast_search`, which does honour dates (§3c).
    """
    season = CURRENT_SEASON if season is None else int(season)
    if season != datetime.date.today().year:
        raise ValueError(
            f"mlb_sim: Savant's framing leaderboard ignores `year` — asking it "
            f"for {season} returns the current season. Writing "
            f"team_framing_{season}.csv would mislabel it. Rebuild from "
            f"statcast_search if a dated version is needed.")
    path = MLBA_DIR / f"team_framing_{season}.csv"
    with open(SHARED_DIR / f"mlb_roster_{season}.json") as fh:
        teams = json.load(fh)["teams"]
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    rows = []
    for t in teams:
        abbr = normalize_club(t.get("abbreviation") or "")
        r = sess.get(Savant.FRAMING_URL,
                     params={"year": season, "team": t["id"], "min": "1",
                             "type": "Cat", "csv": "true"}, timeout=60)
        got = [x for x in csv.DictReader(io.StringIO(r.text)) if x.get("rv_tot")]
        rows.append({
            "team": abbr,
            "framing_runs": round(sum(float(x["rv_tot"]) for x in got), 3),
            "chances": sum(int(x["pitches"]) for x in got),
            "catchers": len(got),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["team", "framing_runs", "chances",
                                           "catchers"])
        w.writeheader()
        w.writerows(rows)
    return path


def load_team_framing(season: Optional[int] = None) -> Dict[str, float]:
    """{club: framing runs saved over the season}. Empty when unavailable."""
    season = CURRENT_SEASON if season is None else int(season)
    # **Keyed on SEASON.** The memo was a bare global, so the first season
    # loaded was served for every later request — which would have made
    # `TEAM_CONTEXT_LAG` a no-op that reported success.
    if season in _FRAMING:
        return _FRAMING[season]
    path = MLBA_DIR / f"team_framing_{season}.csv"
    if not path.exists():
        try:
            export_framing(season)
        except Exception as e:
            # Expected whenever the season is lagged (§3d.2): Savant's framing
            # board ignores `year`, so there is no such file to BUILD.
            #
            # **Two reasons this used to spam.** The once-per-process guard was
            # coupled to a cache other code may clear, and every `backtest()`
            # starts a FRESH pool, so ~22 workers each said it once per arm. A
            # dedicated flag fixes the first; the second is fixed by not warning
            # when framing is off ON PURPOSE — missing data you asked not to use
            # is not worth a line of output.
            global _FRAMING_WARNED
            if not _FRAMING_WARNED and FRAMING_TILT_SCALE:
                _FRAMING_WARNED = True
                print(f"[framing] unavailable: {e}")
            _FRAMING[season] = {}
            return _FRAMING[season]
    out: Dict[str, float] = {}
    try:
        with open(path) as fh:
            for row in csv.DictReader(fh):
                out[row["team"]] = float(row["framing_runs"] or 0.0)
    except (OSError, ValueError, KeyError):
        out = {}
    _FRAMING[season] = out
    return out


# ---------------------------------------------------------------------------
# 9d. FRAMING REBUILT FROM PITCH LEVEL — the DATE-AWARE series
# ---------------------------------------------------------------------------
# `export_framing` refuses to write a past season because Savant's leaderboard
# IGNORES `year`. That is why `ab_configure` ABLATES framing instead of lagging
# it, and why a term worth **+0.0263 of win probability** best-to-worst catcher
# ships live having never been through an A/B. `statcast_search` DOES honour
# dates and every pitch carries `fielder_2`, so the series is rebuildable from
# pitch level (`scrape_framing.py`, kept out of model code).
#
# **Two mistakes this code exists to not make, both of which look fine:**
#   * `zone` is NOT the attack zone — 1-9 is the grid INSIDE the strike zone and
#     11-14 the quadrants ENTIRELY outside it, so "shadow = 11-14" scores a 4.3%
#     called-strike rate and means nothing.
#   * A GEOMETRIC shadow band is no better: across +/- one baseball the call runs
#     0.995 -> 0.279, so any bin averages pitches with nothing in common.
#
# So the surface is modelled CONTINUOUSLY and the credit is `actual - expected`
# per pitch, with no zone definition anywhere. Validated against Savant's own
# per-club numbers (r +0.95, slope +1.03). sim_state.md A.10.

FRAMING_RUNS_PER_STRIKE = 0.125          # Statcast's published conversion
FRAMING_PITCH_VERSION = "v2"
# **Tuned by HELD-OUT log-loss, not by eye.** Smoothing counts and strikes
# separately then dividing is Nadaraya-Watson, and at a wide bandwidth strike
# mass from the dense zone interior bleeds into sparse low-rate cells — at
# (0.10, 0.15, sigma 1.5) `expected` over-predicted by 0.0038 of strike rate,
# -130 runs across a league whose real spread is +/-15. Finer bins and a tighter
# kernel win on log-loss AND calibration at once, which is bias, not a trade.
FR_X_LO, FR_X_HI, FR_X_STEP = -2.0, 2.0, 0.05
FR_Z_LO, FR_Z_HI, FR_Z_STEP = -3.0, 3.0, 0.075
FR_SIGMA = 1.0


class Framing:
    """Framing rebuilt from pitch level — the date-aware series (9d)."""

    @staticmethod
    def _factor_codes(rows: Sequence[dict], fn):
        """(integer code per row, the ordered level values) for one factor.

        The random-effect fits need every grouping variable — catcher, pitcher,
        umpire — as dense 0..k-1 codes. Both fitters had their own nested copy
        of this.
        """
        vals = sorted({fn(r) for r in rows}, key=str)
        idx = {v: i for i, v in enumerate(vals)}
        return np.array([idx[fn(r)] for r in rows]), vals

    @staticmethod
    def framing_pitch_dir(season: int, save_dir: Path = SAVE_DIR) -> Path:
        return (Path(save_dir) / "framing_pitches" / FRAMING_PITCH_VERSION
                / str(season))

    @staticmethod
    def load_umpires(season: int, save_dir: Path = SAVE_DIR
                     ) -> Optional[Dict[int, object]]:
        """`umpires_<season>.json` keyed by gamePk, or None if there is no file.

        None rather than {}: the caller must tell "no file" (switch the umpire
        term off) from "a file with nobody in it" (a real table, leave the fit).
        """
        try:
            with open(Path(save_dir) / f"umpires_{season}.json") as fh:
                return {int(k): v for k, v in json.load(fh).items()}
        except (OSError, ValueError):
            return None

    @staticmethod
    def _fr_nx() -> int:
        return int(round((FR_X_HI - FR_X_LO) / FR_X_STEP))

    @staticmethod
    def _fr_nz() -> int:
        return int(round((FR_Z_HI - FR_Z_LO) / FR_Z_STEP))

    @staticmethod
    def load_framing_takes(season: Optional[int] = None, upto: Optional[str] = None,
                           save_dir: Path = SAVE_DIR) -> List[dict]:
        """Every taken pitch, normalised for the model.

        `plate_x` needs no normalisation — the plate is 17 inches for everyone.
        `plate_z` does, because `sz_top`/`sz_bot` are per BATTER, so it is carried
        as `(z - mid) / half`: -1..+1 inside the zone whatever the hitter's
        height. The published reference implementation skips this and says so; the
        fields are here at 100%, so there is no reason to.

        `blocked_ball` is excluded: blocking is a different skill from receiving.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rows: List[dict] = []
        d = Framing.framing_pitch_dir(season, save_dir)
        for path in sorted(d.glob("*.json.gz")):
            with gzip.open(path, "rt") as fh:
                for x in json.load(fh):
                    if x.get("description") not in ("ball", "called_strike"):
                        continue
                    if upto and x.get("game_date", "") > upto:
                        continue
                    try:
                        px, pz = float(x["plate_x"]), float(x["plate_z"])
                        top, bot = float(x["sz_top"]), float(x["sz_bot"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    if not top > bot:
                        continue
                    mid, half = (top + bot) / 2.0, (top - bot) / 2.0
                    rows.append({
                        "x": px, "zn": (pz - mid) / half,
                        "date": x.get("game_date", ""),
                        "s": 1 if x["description"] == "called_strike" else 0,
                        "c": x.get("fielder_2"), "stand": x.get("stand") or "R",
                        "pk": x.get("game_pk"), "pit": x.get("pitcher"),
                        # The FIELDING side owns the catcher: top of the inning
                        # means the away team bats, so the HOME club is catching.
                        #
                        # **NORMALISED HERE, at the source.** Savant spells seven
                        # clubs differently from the FanGraphs board this engine
                        # keys on, so a model stored under Savant's codes hands
                        # `build_side` a miss on a QUARTER of the league. It does
                        # not raise — the lookup returns 0.0 and those clubs get
                        # no framing, which is why an A/B of it read as "no
                        # effect". Applied to the stored KEY, not the query.
                        "club": normalize_club(
                            (x.get("home_team") if x.get("inning_topbot") == "Top"
                             else x.get("away_team")) or "")})
        return rows

    @staticmethod
    def _fr_smooth2d(a, sigma_bins: Optional[float] = None, radius: int = 4):
        """Separable Gaussian blur. numpy only — scipy is not a dependency."""
        sigma_bins = FR_SIGMA if sigma_bins is None else float(sigma_bins)
        k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma_bins) ** 2)
        k /= k.sum()
        out = np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 0, a)
        return np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 1, out)

    @staticmethod
    def build_framing_surface(rows: Sequence[dict], sigma: Optional[float] = None):
        """{stance: (rate_grid, count_grid)} — smoothed empirical P(called strike).

        Fitted per batter STANCE, which moves the zone edges ~2 percentage points.
        """
        sigma = FR_SIGMA if sigma is None else float(sigma)
        nx, nz = Framing._fr_nx(), Framing._fr_nz()
        out = {}
        for stand in ("L", "R"):
            n = np.zeros((nx, nz))
            st_ = np.zeros((nx, nz))
            for r in rows:
                if r["stand"] != stand:
                    continue
                i = int((r["x"] - FR_X_LO) / FR_X_STEP)
                j = int((r["zn"] - FR_Z_LO) / FR_Z_STEP)
                if 0 <= i < nx and 0 <= j < nz:
                    n[i, j] += 1
                    st_[i, j] += r["s"]
            ns, ss = Framing._fr_smooth2d(n, sigma), Framing._fr_smooth2d(st_, sigma)
            with np.errstate(invalid="ignore", divide="ignore"):
                rate = np.where(ns > 0, ss / np.maximum(ns, 1e-9), np.nan)
            out[stand] = (rate, ns)
        return out

    @staticmethod
    def measure_framing(season: Optional[int] = None, upto: Optional[str] = None,
                        save_dir: Path = SAVE_DIR, sigma: Optional[float] = None,
                        rounds: int = 8, with_pitcher: bool = True,
                        with_umpire: bool = True, verbose: bool = True,
                        out_path: Optional[Path] = None) -> dict:
        """Per-catcher and per-club framing runs, as of `upto`.

        Three layers: the location SURFACE, a CALIBRATION making it zero-sum by
        construction, then EB-shrunk random effects for umpire, pitcher and
        catcher fitted by coordinate ascent.

        **Why the pitcher and umpire effects.** `actual - expected` credits the
        catcher with everything location does not explain, including the pitcher's
        command and the umpire's zone, so a catcher who receives good-command arms
        looks good. Statcast adjusts for the pitcher; BP's CSAA adds umpire and
        batter. Umpire joins at 100% here and Statcast does not use it at all.
        """
        sigma = FR_SIGMA if sigma is None else float(sigma)
        season = CURRENT_SEASON if season is None else int(season)
        rows = Framing.load_framing_takes(season, upto, save_dir)
        if not rows:
            raise SystemExit(
                f"mlb_sim: no framing pitches for {season} under "
                f"{Framing.framing_pitch_dir(season, save_dir)}. Run "
                f"`python scrape_framing.py {season}` first.")
        ump = Framing.load_umpires(season, save_dir)
        if ump is None:
            ump = {}
            if with_umpire and verbose:
                print(f"[framing] no umpires_{season}.json — umpire effect OFF")
            with_umpire = False

        surf = Framing.build_framing_surface(rows, sigma)
        base = np.array([framing_expected(r, surf) for r in rows])
        y = np.array([r["s"] for r in rows], dtype=float)
        ca, cb = fit_framing_calibration(_fr_logit(base), y)
        eta = ca + cb * _fr_logit(base)
        if verbose:
            p0 = _fr_expit(eta)
            print(f"[framing] {season}{' thru ' + upto if upto else ''}: "
                  f"{len(rows):,} takes")
            print(f"  calibration a={ca:+.4f} b={cb:+.4f}   drift "
                  f"{(y.sum()-base.sum())*FRAMING_RUNS_PER_STRIKE:+.2f} -> "
                  f"{(y.sum()-p0.sum())*FRAMING_RUNS_PER_STRIKE:+.2f} runs")

        def codes_for(fn):
            return Framing._factor_codes(rows, fn)

        groups = []
        if with_umpire:
            groups.append(("umpire", *codes_for(
                lambda r: ump.get(int(r["pk"]), "?") if r.get("pk") else "?")))
        if with_pitcher:
            groups.append(("pitcher", *codes_for(lambda r: r["pit"])))
        groups.append(("catcher", *codes_for(lambda r: r["c"])))

        eff = {nm: np.zeros(len(vals)) for nm, _, vals in groups}
        taus: Dict[str, float] = {}
        rnd = -1
        for rnd in range(rounds):
            moved = 0.0
            for nm, codes, vals in groups:
                held = eta - eff[nm][codes]          # hold the other effects fixed
                new, tau2 = fit_framing_random_effect(held, y, codes, len(vals))
                moved = max(moved, float(np.abs(new - eff[nm]).max()))
                eff[nm] = new
                taus[nm] = tau2
                eta = held + eff[nm][codes]
            # One Newton step on the INTERCEPT each round, so the zero-sum
            # identity the calibration established survives the random effects.
            pp = _fr_expit(eta)
            eta = eta + (y.sum() - pp.sum()) / max(float(np.sum(pp * (1 - pp))),
                                                   1e-9)
            if moved < 1e-7:
                break
        if verbose:
            pp = _fr_expit(eta)
            print("  " + "  ".join(f"{nm} sd {math.sqrt(taus[nm]):.4f}"
                                   for nm, _, _ in groups)
                  + f"   rounds {rnd+1}   final drift "
                    f"{(y.sum()-pp.sum())*FRAMING_RUNS_PER_STRIKE:+.2f} runs")

        # The catcher's credit is measured against a prediction that EXCLUDES his
        # own effect but keeps the umpire's and the pitcher's.
        cat = [g for g in groups if g[0] == "catcher"][0]
        p_nc = _fr_expit(eta - eff["catcher"][cat[1]])
        per_c: Dict[str, list] = {}
        per_club: Dict[str, list] = {}
        club_games: Dict[str, set] = {}
        cat_games: Dict[str, set] = {}
        for i, r in enumerate(rows):
            if r["club"]:
                club_games.setdefault(r["club"], set()).add(r.get("pk"))
            if r["c"]:
                cat_games.setdefault(r["c"], set()).add(r.get("pk"))
            for d, k in ((per_c, r["c"]), (per_club, r["club"])):
                if k is None:
                    continue
                v = d.setdefault(k, [0.0, 0.0, 0])
                v[0] += y[i]
                v[1] += p_nc[i]
                v[2] += 1
        out = {
            "season": season, "upto": upto, "sigma": sigma,
            "calibration": {"a": ca, "b": cb},
            "tau": {k: math.sqrt(v) for k, v in taus.items()},
            "effects": {nm: {str(v): float(eff[nm][i])
                             for i, v in enumerate(vals)}
                        for nm, _, vals in groups},
            # **`games` is counted here, not looked up.** An AS-OF framing total
            # covers only the games played by that date, so dividing it by the
            # club's full-season game count — which is what `build_side` does for
            # the Savant CSV — would understate every club in April by a factor of
            # four. Distinct `game_pk` in the window is exact and free.
            "club": {k: {"runs": (v[0] - v[1]) * FRAMING_RUNS_PER_STRIKE,
                         "csaa": v[0] - v[1], "chances": v[2],
                         "games": len(club_games.get(k) or ())}
                     for k, v in per_club.items()},
            # `games` per CATCHER for the same reason as per club: framing is a
            # rate, and a backup who caught 30 games is not a tenth of a starter.
            "catcher": {str(k): {"runs": (v[0] - v[1]) * FRAMING_RUNS_PER_STRIKE,
                                 "csaa": v[0] - v[1], "chances": v[2],
                                 "games": len(cat_games.get(k) or ())}
                        for k, v in per_c.items()}}
        dest = (Path(out_path) if out_path
                else framing_model_path(season, upto, save_dir))
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w") as fh:
            json.dump(out, fh, indent=1)
        if verbose:
            print(f"  {len(out['club'])} clubs, {len(out['catcher'])} catchers, "
                  f"club sum {sum(v['runs'] for v in out['club'].values()):+.2f} "
                  f"runs -> {dest.name}")
        return out

    @staticmethod
    def framing_validate_report(season: Optional[int] = None,
                                path: Optional[Path] = None,
                                save_dir: Path = SAVE_DIR) -> dict:
        """Does the rebuild reproduce SAVANT's published per-club numbers?

        **The go/no-go.** The point is not to beat Statcast, it is to get a
        date-aware series — so the test is that the same code over the FULL season
        lands on Savant's numbers, and the as-of versions inherit that.

        Compared LIKE FOR LIKE: Savant applies a minimum-chances qualifier, so each
        club is scored on its top-N by chances with N from Savant's own `catchers`
        column, and a catcher's runs are pro-rated by his share of chances at that
        club — a traded catcher belongs to both.
        """
        season = CURRENT_SEASON if season is None else int(season)
        got = json.load(open(path or framing_model_path(season, None, save_dir)))
        ref: Dict[str, dict] = {}
        with open(MLBA_DIR / f"team_framing_{season}.csv") as fh:
            for r in csv.DictReader(fh):
                ref[normalize_club(r["team"])] = {
                    "runs": float(r["framing_runs"]), "n": int(r["catchers"])}
        rows = Framing.load_framing_takes(season, got.get("upto"), save_dir)
        tot = collections.Counter()
        by_club: Dict[str, collections.Counter] = {}
        for r in rows:
            tot[r["c"]] += 1
            by_club.setdefault(normalize_club(r["club"] or ""),
                               collections.Counter())[r["c"]] += 1
        cat = got["catcher"]
        keys = sorted(set(by_club) & set(ref))
        a, b = [], []
        for k in keys:
            top = sorted(by_club[k].items(), key=lambda z: -z[1])[:ref[k]["n"]]
            a.append(sum((cat.get(str(c)) or {}).get("runs", 0.0) * (n / tot[c])
                         for c, n in top if tot[c]))
            b.append(ref[k]["runs"])
        mx, my = statistics.mean(a), statistics.mean(b)
        sxx = sum((x - mx) ** 2 for x in a)
        syy = sum((x - my) ** 2 for x in b)
        sxy = sum((x - mx) * (y2 - my) for x, y2 in zip(a, b))
        corr = sxy / (sxx * syy) ** 0.5 if sxx and syy else 0.0
        rmse = (sum((x - y2) ** 2 for x, y2 in zip(a, b)) / len(a)) ** 0.5
        print(f"\nFRAMING vs Savant — {season}, {len(keys)} clubs, like-for-like")
        print(f"  correlation {corr:+.4f}   slope {sxy/sxx if sxx else 0:+.4f}"
              f"   RMSE {rmse:.2f} runs")
        print(f"  our sd {statistics.pstdev(a):.2f}   Savant sd "
              f"{statistics.pstdev(b):.2f}   our sum {sum(a):+.1f}")
        print(f"\n  {'club':<6s}{'ours':>9s}{'Savant':>9s}{'diff':>8s}")
        for k, x, y2 in sorted(zip(keys, a, b), key=lambda z: -z[2]):
            print(f"  {k:<6s}{x:>9.2f}{y2:>9.2f}{x-y2:>8.2f}")
        return {"corr": corr, "slope": sxy / sxx if sxx else 0.0, "rmse": rmse,
                "n": len(keys)}

    @staticmethod
    def framing_repeatability_report(season: Optional[int] = None,
                                     split: Optional[str] = None,
                                     min_chances: int = 400,
                                     save_dir: Path = SAVE_DIR) -> dict:
        """SPLIT-HALF: does adjusting for pitcher and umpire give a BETTER
        catcher estimate, or does it strip real skill?

        **Agreement with Savant cannot answer this** — Savant applies a pitcher
        adjustment and no umpire adjustment, so diverging from it is what the
        change is FOR, and "we disagree because we are better" is a story, not a
        measurement (5.11.2). So the instrument is 3d.8's: fit on the first half
        of a season, score against the second, both variants on the same held-out
        pitches with only the catcher term differing.

        > **Read the two numbers separately.** Raw predictive power can FAVOUR the
        > unadjusted estimate for a bad reason: a catcher works the same staff in
        > both halves, so a metric carrying his pitchers' command will "predict"
        > the second half by carrying it again. For THIS engine that is
        > disqualifying either way — the sim already prices the pitcher's own
        > K/BB rates, so framing containing his command double-counts it.
        """
        season = CURRENT_SEASON if season is None else int(season)
        rows = Framing.load_framing_takes(season, save_dir=save_dir)
        if not rows:
            raise SystemExit(f"mlb_sim: no framing pitches for {season}")
        dates = sorted({r["date"] for r in rows if r["date"]})
        split = split or dates[len(dates) // 2]
        h1 = [r for r in rows if r["date"] and r["date"] <= split]
        h2 = [r for r in rows if r["date"] and r["date"] > split]
        print(f"\nFRAMING split-half — {season}, split at {split}")
        print(f"  first half {len(h1):,} takes   second half {len(h2):,}")

        ump = Framing.load_umpires(season, save_dir) or {}

        # ONE surface, fitted on the first half only, used for both variants and
        # for scoring — so nothing about the location model differs between arms.
        surf = Framing.build_framing_surface(h1, FR_SIGMA)
        y1 = np.array([r["s"] for r in h1], dtype=float)
        e1 = _fr_logit(np.array([framing_expected(r, surf) for r in h1]))
        ca, cb = fit_framing_calibration(e1, y1)

        def codes(rws, fn):
            return Framing._factor_codes(rws, fn)

        c_codes, c_vals = codes(h1, lambda r: r["c"])
        variants = {}
        for name, adj in (("catcher only", False), ("+ pitcher + umpire", True)):
            eta = ca + cb * e1
            eff = {"catcher": np.zeros(len(c_vals))}
            groups = [("catcher", c_codes, c_vals)]
            if adj:
                groups = [("umpire", *codes(h1, lambda r: ump.get(int(r["pk"]), "?")
                                            if r.get("pk") else "?")),
                          ("pitcher", *codes(h1, lambda r: r["pit"]))] + groups
                for nm, _, vals in groups:
                    eff[nm] = np.zeros(len(vals))
            for _ in range(8):
                for nm, cd, vals in groups:
                    held = eta - eff[nm][cd]
                    eff[nm], _ = fit_framing_random_effect(held, y1, cd, len(vals))
                    eta = held + eff[nm][cd]
            variants[name] = dict(zip(c_vals, eff["catcher"]))

        # score the SECOND half: same surface, same calibration, catcher term only
        ch2 = collections.Counter(r["c"] for r in h2)
        ch1 = collections.Counter(r["c"] for r in h1)
        keep = {c for c in ch2 if ch2[c] >= min_chances and ch1[c] >= min_chances}
        sub = [r for r in h2 if r["c"] in keep]
        y2 = np.array([r["s"] for r in sub], dtype=float)
        base2 = ca + cb * _fr_logit(np.array([framing_expected(r, surf)
                                              for r in sub]))
        print(f"  scored on {len(sub):,} second-half takes from {len(keep)} "
              f"catchers with {min_chances}+ in BOTH halves")
        out = {}
        p0 = _fr_expit(base2)
        ll0 = float(-(y2 * np.log(np.clip(p0, 1e-9, 1)) +
                      (1 - y2) * np.log(np.clip(1 - p0, 1e-9, 1))).mean())
        print(f"\n  {'variant':<22s}{'held-out logloss':>18s}{'vs no-catcher':>15s}"
              f"{'corr w/ H2':>12s}")
        print(f"  {'no catcher term':<22s}{ll0:>18.6f}{'--':>15s}{'--':>12s}")
        # the second half's own catcher deviation, measured identically for both
        h2_dev = {}
        for c in keep:
            m_ = [i for i, r in enumerate(sub) if r["c"] == c]
            h2_dev[c] = float(y2[m_].mean() - p0[m_].mean())
        for name, eff_c in variants.items():
            adjv = np.array([eff_c.get(r["c"], 0.0) for r in sub])
            p = _fr_expit(base2 + adjv)
            ll = float(-(y2 * np.log(np.clip(p, 1e-9, 1)) +
                         (1 - y2) * np.log(np.clip(1 - p, 1e-9, 1))).mean())
            xs = [eff_c.get(c, 0.0) for c in sorted(keep)]
            ys = [h2_dev[c] for c in sorted(keep)]
            mx, my = statistics.mean(xs), statistics.mean(ys)
            sxx = sum((a - mx) ** 2 for a in xs)
            syy = sum((b - my) ** 2 for b in ys)
            r_ = (sum((a - mx) * (b - my) for a, b in zip(xs, ys))
                  / (sxx * syy) ** 0.5) if sxx and syy else 0.0
            out[name] = {"logloss": ll, "gain": ll0 - ll, "corr": r_}
            print(f"  {name:<22s}{ll:>18.6f}{ll0-ll:>+15.6f}{r_:>+12.4f}")
        return out

    @staticmethod
    def catcher_framing_per_game(catcher_id: Optional[int], season: int,
                                 save_dir: Path = SAVE_DIR) -> Optional[float]:
        """THIS catcher's framing runs per game, or None when he is not on file.

        **Framing is a PLAYER skill, and the club aggregate is the wrong object.**
        A club's number is a roster property: Patrick Bailey split CLE 3,360 / SFG
        2,053 inside one season, so last year's Cleveland figure carries the
        framing of a man now in San Francisco. Lagging THAT measures the wrong
        thing — which is what the first framing A/B did, and why it came back
        negative. Lagging a CATCHER is fine: his skill travels with him.
        """
        if catcher_id is None:
            return None
        got = load_framing_model(season, FRAMING_ASOF or None, save_dir)
        rec = (got.get("catcher") or {}).get(str(int(catcher_id)))
        if not rec or not rec.get("games"):
            return None
        return float(rec["runs"]) / float(rec["games"])


def _fr_logit(p, eps: float = 1e-6):
    """ARRAY logit. Deliberately not `_logit`, which is the scalar `math`
    version used by the Triple-A translation and would silently accept an
    array and return nonsense."""
    return np.log(np.clip(p, eps, 1 - eps) / (1 - np.clip(p, eps, 1 - eps)))


def _fr_expit(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -35, 35)))


def framing_expected(r: dict, surf: dict) -> float:
    """Off-grid takes get 0.0, and that is MEASURED rather than assumed:
    13,754 of them (5.06%) fall outside these extents and FOUR were called
    strikes, a rate of 0.00029."""
    got = surf.get(r["stand"]) or surf.get("R")
    if got is None:
        return 0.0
    rate, _ = got
    i = int((r["x"] - FR_X_LO) / FR_X_STEP)
    j = int((r["zn"] - FR_Z_LO) / FR_Z_STEP)
    if not (0 <= i < Framing._fr_nx() and 0 <= j < Framing._fr_nz()):
        return 0.0
    v = rate[i, j]
    return 0.0 if v != v else float(v)


def fit_framing_calibration(eta, y, iters: int = 25) -> Tuple[float, float]:
    """Two-parameter Platt recalibration `a + b*eta`, by Newton-Raphson.

    **This is what makes framing zero-sum, and it is not a fudge.** Framing is
    `actual - expected`, so it only sums to zero league-wide when
    `sum(expected) == sum(actual)`. The raw surface missed by -0.00043 of
    strike rate, i.e. **-14.75 runs** on a quantity whose real spread is
    +/-15. The score equation for a logistic INTERCEPT is exactly
    `sum(fitted) == sum(observed)`, so fitting one makes the identity hold by
    construction rather than imposing it afterwards — and `b` additionally
    undoes the slope compression the smoother introduces, measured at 1.14.
    """
    a, b = 0.0, 1.0
    for _ in range(iters):
        p = _fr_expit(a + b * eta)
        w = np.maximum(p * (1 - p), 1e-9)
        r = y - p
        g = np.array([r.sum(), (r * eta).sum()])
        H = np.array([[w.sum(), (w * eta).sum()],
                      [(w * eta).sum(), (w * eta * eta).sum()]])
        try:
            step = np.linalg.solve(H, g)
        except Exception:
            break
        a += float(step[0])
        b += float(step[1])
        if abs(step).max() < 1e-10:
            break
    return a, b


def fit_framing_random_effect(eta, y, codes, n_levels: int):
    """One EMPIRICAL-BAYES shrunk offset per level. Returns (offsets, tau2).

    A one-step Newton offset per level, shrunk by `tau^2 / (tau^2 + var_i)`
    with `tau^2` the between-level variance by method of moments (observed
    spread minus mean sampling variance). A catcher with 300 chances is pulled
    hard toward zero and one with 6,000 barely at all — which is the whole
    point for an AS-OF series, where April samples are tiny and the shipped
    Statcast number has no shrinkage in it at all.
    """
    p = _fr_expit(eta)
    w = np.maximum(p * (1 - p), 1e-9)
    score = np.bincount(codes, weights=(y - p), minlength=n_levels)
    hess = np.bincount(codes, weights=w, minlength=n_levels)
    raw = np.where(hess > 0, score / np.maximum(hess, 1e-9), 0.0)
    var_i = np.where(hess > 0, 1.0 / np.maximum(hess, 1e-9), np.inf)
    keep = np.isfinite(var_i) & (hess > 5)
    tau2 = (max(float(np.var(raw[keep]) - np.mean(var_i[keep])), 1e-6)
            if keep.sum() > 2 else 1e-6)
    return raw * (tau2 / (tau2 + var_i)), tau2


def framing_model_path(season: int, upto: Optional[str] = None,
                       save_dir: Path = SAVE_DIR) -> Path:
    """Keyed on the CUTOFF as well as the season. An as-of framing file
    written under the season's own name is the mislabelling `export_framing`
    refuses to do, one directory along."""
    stem = f"framing_model_{season}" + (f"_{upto}" if upto else "")
    return Path(save_dir) / f"{stem}.json"


# **OFF until an A/B says otherwise.** The pitch-level series is better measured
# (held-out logloss 0.124600 against 0.124704, held-out correlation +0.487
# against +0.447) but "better measured" is not "prices better". What it unlocks
# matters more than its accuracy: framing has been ABLATED in every backtest ever
# run, and a date-aware series can be lagged, so framing becomes A/B-able for the
# first time.
USE_PITCH_FRAMING = False
# The as-of cutoff for the pitch-level series. **A STRING, and "" rather than
# None on purpose**: `_SLATE_OVERRIDE_TYPES` is (int, float, str, bool, tuple),
# so a None-valued global is NOT captured and would silently fail to reach a
# pool worker — the exact class of defect
# `test_pool_overrides_capture_every_tunable_constant` exists for.
FRAMING_ASOF = ""

_FRAMING_MODEL: Dict[tuple, dict] = {}


def load_framing_model(season: Optional[int] = None, upto: Optional[str] = None,
                       save_dir: Path = SAVE_DIR) -> dict:
    """The cached pitch-level framing model, or {} when it was not built."""
    season = CURRENT_SEASON if season is None else int(season)
    key = (int(season), upto or "")
    if key in _FRAMING_MODEL:
        return _FRAMING_MODEL[key]
    try:
        with open(framing_model_path(season, upto, save_dir)) as fh:
            _FRAMING_MODEL[key] = json.load(fh)
    except (OSError, ValueError):
        _FRAMING_MODEL[key] = {}
    return _FRAMING_MODEL[key]


def team_framing_per_game(season: int, abbr: str, fallback_games: float,
                          save_dir: Path = SAVE_DIR) -> float:
    """A club's framing runs PER GAME, from whichever series is switched on.

    The division lives here rather than at the call site because the two
    sources have different denominators: the Savant CSV is a season total to
    be divided by the season's games, while the pitch-level model carries the
    games actually inside its own window.
    """
    if USE_PITCH_FRAMING:
        got = load_framing_model(season, FRAMING_ASOF or None, save_dir)
        # Keys are stored normalised (see `load_framing_takes`); the second
        # lookup is belt-and-braces for a model written before that fix.
        rec = ((got.get("club") or {}).get(normalize_club(abbr))
               or (got.get("club") or {}).get(abbr))
        if rec and rec.get("games"):
            return float(rec["runs"]) / float(rec["games"])
    return load_team_framing(season).get(abbr, 0.0) / fallback_games


def load_team_defense(season: Optional[int] = None) -> Dict[str, dict]:
    season = CURRENT_SEASON if season is None else int(season)
    if season in _DEF:
        return _DEF[season]
    path = MLBA_DIR / f"team_defense_{season}.csv"
    if not path.exists():
        try:
            export_defense(season)
        except Exception as e:
            print(f"[defense] unavailable: {e}")
            _DEF[season] = {}
            return _DEF[season]
    out = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            try:
                out[row["team"]] = {
                    "oaa": float(row["oaa"] or 0.0),
                    "of_arm": float(row["of_arm"]) if row.get("of_arm") else None}
            except (TypeError, ValueError):
                continue
    _DEF[season] = out
    return out

if __name__ == "__main__":
    main()
