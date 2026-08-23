"""ML state-vector experiment — can ML beat shrinkage + log5 at the PA level?

**This module does not touch the simulator.** The engine in `mlb_sim.py` owns
the nine-outcome plate-appearance vector, the base/out transitions, the bullpen
and the Monte Carlo. The only question here is whether the RATE-ESTIMATION
LAYER — the step that turns two season boards into tonight's per-PA
distribution — can be improved, with everything downstream held byte-identical.

The incumbent is the thing to beat, and it is named explicitly:

    P_base = offence_tilt(apply_hfa(apply_defense(
                 log5(platoon(batter), pitcher), oaa), home), wx + park)

and the candidate is a multiplicative residual on top of it, in log space,

    log P_ml,i = log P_base,i + f_i(X)        then renormalise

which is exactly what a LightGBM multiclass model with `init_score = log P_base`
optimises. That is not a coincidence — it is the reason LightGBM is the tool.

Three layers, and each one has to pass before the next is worth building:

  1. `pa`       real plate appearances, one row each, mapped onto the nine
                outcomes.  ACCEPTANCE: the league-wide vector this produces
                must reproduce `mlb_sim.LEAGUE_BASELINE`.  A mapping error
                here is invisible everywhere downstream.
  2. `dataset`  every PA joined to the as-of boards that were on disk BEFORE
                its game, the baseline vector the incumbent would have used,
                and ~150 features.  Time-safety is enforced by construction:
                the boards come from `build_rates_asof` at the cutoff the
                backtest itself would have picked.
  3. `train`    / `score` / `ab` — Models B, C and D from the plan, scored at
                the PA level, then through the simulator, then against the
                closing line.

Nothing here ships until the third layer says it should. Section 8 of the
experiment plan is the standing rule: a PA-level log-loss win that does not
survive aggregation into moneyline and totals is not a reason to add a model.

    python mlb_ml.py pa 2023 2024 2025 2026     # ground truth, ~40 min cold
    python mlb_ml.py pa-report 2025             # the acceptance test
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import gzip
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests

import mlb_sim as m


# ===========================================================================
# 1. PLATE APPEARANCES — the ground truth the whole experiment rests on
# ===========================================================================
# **The cache directory is VERSIONED.** Adding a field to a cache that already
# holds a season of games is the silent corruption this project has recorded
# four times: the old files parse fine, the new key reads None everywhere, and
# the model runs on a constant without erroring. A new field means a new
# directory (mlb_sim.py §8 trap 9).
PA_VERSION = "v2"
PA_DIR = m.SAVE_DIR / "pa" / PA_VERSION

# 656 KB a game becomes 47 KB with the whitelist, and 9,200 games is the
# difference between a 6 GB download and a 430 MB one. The known failure mode
# is SILENT (a consumer gains a key, the whitelist does not, and the field
# reads None forever), so every field read below appears in this string and
# `pa_report` checks the ones that carry information.
#
# `playEvents.hitData.trajectory` is the expensive part of it — it is what
# takes the payload from 38 KB to 47 KB — and it is not optional. See below.
PBP_FIELDS = (
    "allPlays,result,type,event,eventType,isOut,rbi,description,"
    "about,atBatIndex,isTopInning,inning,isComplete,"
    "count,outs,"
    "matchup,batter,id,batSide,code,pitcher,pitchHand,"
    "postOnFirst,postOnSecond,postOnThird,"
    "playEvents,hitData,trajectory"
)

# --- eventType -> one of the nine outcomes --------------------------------
# The nine are `mlb_sim`'s, and the mapping has to agree with how
# `outcome_counts` reads a FanGraphs board, NOT with what feels physically
# right — the baseline vector this trains against is built from that board, so
# a definition that differs by so much as reached-on-error puts a constant
# offset into every residual and the model dutifully learns it.
#
# The board identity is
#
#     outs_in_play = PA - SO - BB - HBP - H
#
# so ANYTHING that is not a strikeout, a walk, a hit-by-pitch or a hit counts
# as an out in play — including reached-on-error, fielder's choice and catcher
# interference, none of which retire the batter. That is the incumbent's
# convention and it is the one used here.
_K_EVENTS = {"strikeout", "strikeout_double_play", "strikeout_triple_play"}
_BB_EVENTS = {"walk", "intent_walk"}
_HBP_EVENTS = {"hit_by_pitch"}
_HIT_EVENTS = {"single": m.S1B, "double": m.S2B, "triple": m.S3B,
               "home_run": m.HR}
# Everything else that completes a plate appearance is an out in play, and the
# only remaining question is ground or air.
_IN_PLAY_EVENTS = {
    "field_out", "force_out", "grounded_into_double_play", "double_play",
    "triple_play", "sac_fly", "sac_fly_double_play", "sac_bunt",
    "sac_bunt_double_play", "fielders_choice", "fielders_choice_out",
    "field_error", "catcher_interf", "batter_interference",
    "other_out",
}

# **Ground versus air comes from `hitData.trajectory`, and the tuples are
# imported rather than restated.** `collect_baserunning` measures P_GIDP,
# P_GB_ADVANCE, P_GB_SCORES and P_SAC_FLY off exactly this field, so labelling
# balls in play any other way would put the engine's base-running constants and
# its GB_OUT population on two different definitions of a ground ball — and
# every double play the model turns is priced off the pair agreeing.
#
# The first attempt at this parsed `result.description` instead ("grounds out
# to short", "flies out to left") because the trajectory looked like it needed
# the heavier payload. It classified 1.8% of balls in play by the fielder's
# POSITION and read 0.008 of plate appearances more ground-heavy than the
# board — a difference that would have travelled into every residual as a
# constant. The real field costs 9 KB a game and has no such argument.
_BB_GROUND = frozenset(m._TRAJ_GB + m._TRAJ_BUNT_GB)
_BB_AIR = frozenset(m._TRAJ_AIR + m._TRAJ_BUNT_AIR)

# Trajectory tags, as stored. Numeric because the table is 700k rows.
BB_GROUND, BB_AIR, BB_NONE = 0, 1, 2


def play_trajectory(play: dict) -> Optional[str]:
    """`hitData.trajectory` for a play, or None if the ball was not put in play.

    Read from the LAST pitch that carries hit data. A plate appearance has at
    most one ball in play, but foul balls also carry `hitData` on some feeds,
    and the ball that ended the PA is the last one.
    """
    got = None
    for ev in (play.get("playEvents") or []):
        tr = (ev.get("hitData") or {}).get("trajectory")
        if tr:
            got = tr
    return got


def classify_pa(event_type: str, trajectory: Optional[str]
                ) -> Tuple[Optional[int], int]:
    """One completed play -> (outcome index, trajectory tag), or (None, _).

    None means the play was not a plate appearance — a caught stealing between
    pitches, a pickoff, a wild pitch that happened to be logged as its own
    play. Those are real events and the engine models them, but they are not
    rows in a PA-outcome table and counting them would dilute every rate.
    """
    et = (event_type or "").lower()
    tag = (BB_GROUND if trajectory in _BB_GROUND else
           BB_AIR if trajectory in _BB_AIR else BB_NONE)
    if et in _K_EVENTS:
        return m.K, tag
    if et in _BB_EVENTS:
        return m.BB, tag
    if et in _HBP_EVENTS:
        return m.HBP, tag
    if et in _HIT_EVENTS:
        return _HIT_EVENTS[et], tag
    if et in _IN_PLAY_EVENTS:
        if tag == BB_GROUND:
            return m.GB_OUT, tag
        if tag == BB_AIR:
            return m.AIR_OUT, tag
        # No trajectory at all: catcher's interference, a batter-interference
        # call, a play the feed never tagged. 3 in 67,000 balls in play. Booked
        # as an AIR out because that is the larger population, and counted by
        # `pa_report` so the assumption stays visible rather than becoming
        # folklore.
        return m.AIR_OUT, BB_NONE
    return None, tag


_BASE_KEYS = ("postOnFirst", "postOnSecond", "postOnThird")


def pa_rows_from_plays(plays: Sequence[dict], game_pk: int) -> List[dict]:
    """`allPlays` -> one row per plate appearance.

    Two things StatsAPI reports only as an AFTER state have to be shifted back
    by one play to become a BEFORE state: `count.outs` and the runners. Both
    reset at the half-inning boundary, which is what makes the shift safe.
    """
    out: List[dict] = []
    prev_key: Optional[tuple] = None
    outs_before = 0
    bases_before = 0
    tto: Dict[Tuple[int, int], int] = {}    # (half, pitcher) -> batters faced
    seen: Dict[Tuple[int, int, int], int] = {}   # (half, pitcher, batter) -> n
    for p in plays:
        about = p.get("about") or {}
        if not about.get("isComplete"):
            continue
        res = p.get("result") or {}
        if res.get("type") != "atBat":
            continue
        mu = p.get("matchup") or {}
        is_top = bool(about.get("isTopInning"))
        inning = about.get("inning") or 0
        key = (inning, is_top)
        if key != prev_key:
            outs_before, bases_before = 0, 0
            prev_key = key

        oc, traj = classify_pa(res.get("eventType") or "",
                               play_trajectory(p))
        bat = (mu.get("batter") or {}).get("id")
        pit = (mu.get("pitcher") or {}).get("id")
        if oc is not None and bat and pit:
            half = 0 if is_top else 1
            hk = (half, int(pit))
            bk = (half, int(pit), int(bat))
            out.append({
                "pk": int(game_pk),
                "i": int(about.get("atBatIndex") or len(out)),
                "inn": int(inning),
                "top": 1 if is_top else 0,
                "bat": int(bat),
                "bs": ((mu.get("batSide") or {}).get("code") or "")[:1],
                "pit": int(pit),
                "ph": ((mu.get("pitchHand") or {}).get("code") or "")[:1],
                "o": int(oc),
                "tj": int(traj),
                "ob": int(outs_before),
                "bb": int(bases_before),
                # Batters this pitcher has faced in this game before now, and
                # times through the order for THIS hitter against him. The
                # hook is BF-indexed and `FATIGUE_DECLINE_PER_BF` is inert by
                # design, so these are features the incumbent does NOT use —
                # which is the point of carrying them.
                "bf": int(tto.get(hk, 0)),
                "tto": int(seen.get(bk, 0)),
            })
            tto[hk] = tto.get(hk, 0) + 1
            seen[bk] = seen.get(bk, 0) + 1

        # Shift the after-state into the next play's before-state. Done for
        # EVERY complete play including the ones rejected above, because a
        # caught stealing changes the outs and the bases just as much.
        outs_before = int((p.get("count") or {}).get("outs") or 0)
        bases_before = sum(v for v, k in zip((1, 2, 4), _BASE_KEYS)
                           if mu.get(k))
    return out


def pa_path(season: int, save_dir: Path = m.SAVE_DIR) -> Path:
    return Path(save_dir) / "pa" / PA_VERSION / f"pa_{season}.json.gz"


def fetch_game_pa(game_pk: int, timeout: float = 30.0,
                  retries: int = 3) -> List[dict]:
    """One game's plate appearances. Raises on a hard failure after retries."""
    url = f"{m.STATSAPI}/game/{game_pk}/playByPlay"
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params={"fields": PBP_FIELDS},
                             timeout=timeout)
            r.raise_for_status()
            return pa_rows_from_plays(r.json().get("allPlays") or [], game_pk)
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"playByPlay {game_pk} failed: {last}")


_PA_LOCK = threading.Lock()


def collect_pa(season: int, workers: int = 10, refresh: bool = False,
               limit: Optional[int] = None,
               save_dir: Path = m.SAVE_DIR, verbose: bool = True) -> Path:
    """Every plate appearance of a season, cached gzipped.

    The game list comes from `season_slate`, not from the schedule, so the PA
    table covers EXACTLY the games the backtest prices — no game can appear in
    one and not the other, which is the join this whole experiment depends on.
    """
    dest = pa_path(season, save_dir)
    if dest.exists() and not refresh:
        if verbose:
            print(f"[pa] {season}: cached at {dest}")
        return dest
    slate = m.season_slate(season, save_dir=save_dir)
    pks = [r["pk"] for r in slate]
    if limit:
        pks = pks[:limit]
    if verbose:
        print(f"[pa] {season}: {len(pks)} games", flush=True)

    rows: List[dict] = []
    failed: List[int] = []
    done = [0]
    t0 = time.time()

    def one(pk: int):
        try:
            got = fetch_game_pa(pk)
        except Exception as e:                       # noqa: BLE001
            got, err = [], e
        else:
            err = None
        with _PA_LOCK:
            done[0] += 1
            if err is not None:
                failed.append(pk)
                print(f"[pa] {season} {pk} FAILED: {err}", flush=True)
            elif verbose and (done[0] % 250 == 0 or done[0] == len(pks)):
                el = time.time() - t0
                rate = done[0] / el if el else 0.0
                print(f"[pa] {season} {done[0]}/{len(pks)}  "
                      f"{el/60:.1f}m elapsed, "
                      f"~{(len(pks)-done[0])/rate/60 if rate else 0:.1f}m left",
                      flush=True)
        return got

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(one, pks):
            rows += got

    if failed:
        # A partial season written to the cache path is the corruption this
        # project keeps recording: it parses fine and is silently short.
        raise RuntimeError(
            f"mlb_ml: {len(failed)} games failed for {season} "
            f"({failed[:5]}...). Nothing written — re-run to retry.")
    rows.sort(key=lambda r: (r["pk"], r["i"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as fh:
        json.dump(rows, fh)
    tmp.replace(dest)                       # atomic: a kill leaves no partial
    if verbose:
        print(f"[pa] {season}: {len(rows)} plate appearances -> {dest} "
              f"({dest.stat().st_size/1e6:.1f} MB)", flush=True)
    return dest


_PA_CACHE: Dict[tuple, List[dict]] = {}


def load_pa(season: int, save_dir: Path = m.SAVE_DIR) -> List[dict]:
    key = (int(season), str(save_dir))
    got = _PA_CACHE.get(key)
    if got is None:
        path = pa_path(season, save_dir)
        if not path.exists():
            raise FileNotFoundError(
                f"mlb_ml: no PA table for {season} at {path}. "
                f"Run `python mlb_ml.py pa {season}` first.")
        with gzip.open(path, "rt") as fh:
            got = json.load(fh)
        _PA_CACHE[key] = got
    return got


# --- the acceptance test ---------------------------------------------------

# How far the seven DIRECTLY counted outcomes may sit from the board before the
# mapping is called wrong. 0.0005 of a plate appearance is ~90 events a season:
# tight enough that a single mis-mapped `eventType` trips it, loose enough to
# absorb the handful of games the board and the schedule disagree about.
PA_DIRECT_TOL = 0.0005


def pa_report(season: int = 2025, save_dir: Path = m.SAVE_DIR) -> dict:
    """Does the extracted PA table reproduce the league it came from?

    **This is the test that says the mapping is right.** Every downstream
    number in this module is a difference against a baseline vector built from
    a FanGraphs board; if the labels disagree with that board's definitions
    the residual model learns the disagreement and reports it as skill.

    The comparison is against the SEASON's own board, not against
    `LEAGUE_BASELINE` — that constant is 2026, and a 2023 table checked against
    it would flag a real run-environment difference as a mapping bug.

    **Seven of the nine are a mapping test; two are not.** K, BB, HBP, 1B, 2B,
    3B and HR are direct counts on both sides, so any disagreement there is
    this module getting an event wrong and the bound is tight. GB_OUT and
    AIR_OUT are not: the board has no ground/air OUT column at all, and
    `_split_outs_in_play` APPORTIONS the outs in play using the batted-ball
    mix. That apportionment is a model, this table is a count, and the gap
    between them is a property of the incumbent rather than an error here —
    reported, and deliberately not asserted on.
    """
    rows = load_pa(season, save_dir)
    n = len(rows)
    counts = [0] * m.N_OUTCOMES
    for r in rows:
        counts[r["o"]] += 1
    vec = [c / n for c in counts]

    games = len({r["pk"] for r in rows})
    unknown = sum(1 for r in rows if r["tj"] == BB_NONE
                  and r["o"] in (m.GB_OUT, m.AIR_OUT))
    in_play = sum(1 for r in rows if r["o"] in (m.GB_OUT, m.AIR_OUT))

    board = m.load_board("bat", season, save_dir)
    board_vec = m.league_baseline(board, "bat") if board else None

    print(f"\nPA TABLE {season}   {n:,} plate appearances over {games:,} games "
          f"({n/games:.2f} per game, {n/games/2:.2f} per team-game)")
    print(f"  untagged trajectory on {unknown:,} of {in_play:,} balls in play "
          f"({unknown/in_play:.3%}), booked as air")
    print(f"\n  {'outcome':9s} {'PA table':>9s} {'board':>9s} {'diff':>8s} "
          f"{'LEAGUE_BASELINE':>16s}")
    direct = (m.K, m.BB, m.HBP, m.S1B, m.S2B, m.S3B, m.HR)
    worst_direct = worst_split = 0.0
    for i, nm in enumerate(m.OUTCOME_NAMES):
        b = board_vec[i] if board_vec else float("nan")
        d = vec[i] - b if board_vec else float("nan")
        if board_vec:
            if i in direct:
                worst_direct = max(worst_direct, abs(d))
            else:
                worst_split = max(worst_split, abs(d))
        mark = "" if i in direct else "   <- apportioned on the board"
        print(f"  {nm:9s} {vec[i]:9.4f} {b:9.4f} {d:+8.4f} "
              f"{m.LEAGUE_BASELINE[i]:16.4f}{mark}")
    print(f"\n  worst DIRECT-count disagreement: {worst_direct:+.4f}   "
          f"(bound {PA_DIRECT_TOL:.4f})")
    if worst_direct > PA_DIRECT_TOL:
        print("  ** The mapping does NOT reproduce the board. Every residual "
              "trained on this\n     table would carry the difference as a "
              "constant and report it as skill. **")
    print(f"  ground/air OUT split, table vs `_split_outs_in_play`: "
          f"{worst_split:+.4f} of PA")

    return {"season": season, "n": n, "games": games, "vector": vec,
            "board_vector": board_vec,
            "unknown_traj": unknown, "in_play": in_play,
            "worst_direct": worst_direct, "worst_split": worst_split}


# ===========================================================================
# 2. THE DATASET — every PA joined to what was knowable before its game
# ===========================================================================
# **Time-safety is by construction, not by discipline.** Every feature for a
# game on date `t` is read off the as-of board at `asof_cutoff_for(t)` — the
# same cutoff `backtest` itself picks — so there is no path by which a later
# game's outcome can reach an earlier game's row. Games before the season's
# first cutoff are dropped, exactly as the backtest drops them.
#
# The baseline vector comes from `mlb_sim.pa_rates` with the form draw at its
# mean. It is not a re-derivation: it is the engine's own function, called with
# the engine's own objects, under the leak-free A/B configuration
# (`ab_configure({}, season)` — `TEAM_CONTEXT_LAG = 1`, framing ablated). What
# the residual is trained against and what the `base` arm simulates are
# therefore the same thing by construction rather than by agreement.
#
# One approximation is worth naming: `offence_tilt` clips, so E[tilt(r, draw+c)]
# is not exactly tilt(r, c). With `GAME_FORM_SD = 0.1134` the clip binds
# essentially never, and the residual is applied multiplicatively AFTER the
# draw at inference, so the draw survives either way.

# **Bumped to v2 when `_ARSENAL_PAIRS` gained the splitter, sweeper and
# knuckle-curve interactions.** The feature COUNT changed (228 -> 231), so a v1
# .npz read by v2 code misaligns every column after the matchup block. Trap 9:
# version the directory rather than adding to it. v1 stays on disk intact and
# its node models remain servable from it, which is what makes the two
# comparable at all.
# **`MLML_PAIRS=core` builds the SIX-pair feature set against TODAY's
# baseline**, so the three pairs added on 2026-08-22 can be A/B'd on a SHARED
# target. It is an ENVIRONMENT variable and not a module global because
# `build_dataset` runs a worker pool and forkserver RE-IMPORTS the module — a
# rebound global would not reach the workers and they would silently build the
# other feature set (sim_state trap 6). The environment IS inherited.
#
# It drives DATASET_VERSION too, because the two feature sets must never share
# a directory: a 228-column `.npz` read as 231 misaligns everything after the
# matchup block (trap 9).
# Default **core**, the SIX shipped pairs. `full` and `dense` were built and
# measured on 2026-08-22/23 against a bit-identical baseline and both scored
# WORSE pooled — core +0.327/+0.052 on the differential/total loading against
# full +0.234/-0.010 and dense +0.235/-0.065. They stay reachable so the
# result is reproducible rather than deleted, but the default must not be an
# arm that lost its own A/B.
_PAIRS_MODE = os.environ.get("MLML_PAIRS", "core").strip().lower()
if _PAIRS_MODE not in ("full", "core", "dense"):
    raise ValueError(f"mlb_ml: MLML_PAIRS must be 'core', 'full' or 'dense', "
                     f"got {_PAIRS_MODE!r}")

DATASET_VERSION = {"core": "v2core", "full": "v2", "dense": "v3"}[_PAIRS_MODE]


def dataset_path(season: int, save_dir: Path = m.SAVE_DIR) -> Path:
    return (Path(save_dir) / "mlml" / DATASET_VERSION
            / f"pa_features_{season}.npz")


def _f(row: Optional[dict], key: str) -> float:
    """A board cell as a float, or NaN. NaN is the point: LightGBM splits on
    missingness natively, and a zero would be a lie about a hitter who simply
    has no bat-tracking data yet."""
    if not row:
        return float("nan")
    v = row.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return float("nan")
    return float(v)


def _ratio(row: Optional[dict], num: str, den: str) -> float:
    d = _f(row, den)
    if not d or d != d:
        return float("nan")
    n = _f(row, num)
    return n / d if n == n else float("nan")


# --- what the rate layer already consumes, and what it THROWS AWAY ---------
# `outcome_counts` reads eleven cells off a board row: PA/TBF, SO, BB, HBP, H,
# HR, 2B, 3B, 1B, and GB/FB/LD. Everything else on a 472-column board is
# information the incumbent has never seen. That is where a residual has room
# to be anything other than noise, so the lists below lean hard on it —
# contact quality, bat tracking, plate discipline, attack zones, pitch mix and
# the two published stuff models — and carry the rate-layer inputs as well,
# because the model needs to know how much sample the shrinkage was working
# with before it can know when to disagree with it.
BAT_COLS: Tuple[str, ...] = (
    # sample and age — how much the shrinkage trusted him
    "PA", "Age", "G",
    # the rate-layer inputs themselves, in raw form
    "K%", "BB%", "GB%", "FB%", "LD%", "IFFB%", "HR/FB", "BABIP", "ISO",
    "wRC+", "wOBA", "xwOBA", "xSLG", "xAVG",
    # contact quality — none of this reaches the incumbent
    "EV", "EV90", "maxEV", "LA", "Barrel%", "HardHit%", "Hard%", "Soft%",
    "Med%", "bipCount",
    # bat tracking
    "AvgBatSpeed", "SwingLength", "AttackAngle", "AttackDirection",
    "IdealAttackAngle%", "BlastContact%", "BlastSwing%",
    "SquaredUpContact%", "SquaredUpSwing%", "FastSwing%", "CompetitiveSwings",
    "DepthInBox", "DistanceOffPlate", "Tilt",
    # plate discipline
    "O-Swing%", "Z-Swing%", "Swing%", "O-Contact%", "Z-Contact%", "Contact%",
    "Zone%", "SwStr%", "CStr%", "F-Strike%", "C+SwStr%",
    # spray and speed
    "Pull%", "Cent%", "Oppo%", "Spd", "UBR", "wSB",
    # Savant attack zones (see project memory: sc* IS Heart/Shadow/Chase/Waste)
    "scH-Swing%", "scH-Contact%", "scS-Swing%", "scS-Contact%",
    "scO-Swing%", "scO-Contact%", "scW-Swing%", "scZ-Contact%",
    # what he has SEEN, and what he has done to it — the hitter-versus-pitch
    # -type profile the experiment plan calls the highest-value area
    "pfxFA%", "pfxSI%", "pfxSL%", "pfxCU%", "pfxCH%", "pfxFC%", "pfxFS%",
    "wFB/C", "wSL/C", "wCB/C", "wCH/C", "wCT/C", "wSF/C",
)

PIT_COLS: Tuple[str, ...] = (
    "TBF", "Age", "G", "GS", "IP",
    "K%", "BB%", "GB%", "FB%", "LD%", "IFFB%", "HR/FB", "HR/9", "BABIP",
    "LOB%", "K-BB%",
    # the published estimators — each is a different opinion about which of
    # his outcomes will recur, which is precisely the shrinkage question
    "FIP", "xFIP", "SIERA", "xERA", "kwERA", "tERA", "ERA",
    # contact allowed
    "EV", "EV90", "maxEV", "LA", "Barrel%", "HardHit%", "Hard%", "Soft%",
    "Med%", "bipCount",
    # discipline induced
    "O-Swing%", "Z-Swing%", "Swing%", "O-Contact%", "Z-Contact%", "Contact%",
    "Zone%", "SwStr%", "CStr%", "F-Strike%", "C+SwStr%",
    "Pull%", "Cent%", "Oppo%",
    # the two published pitch models. `USE_STUFF_PRIOR` already puts an
    # arsenal-derived prior into the rate layer, so these are partly known —
    # but Stuff+ and PitchingBot are fitted on different targets and the
    # incumbent uses neither directly.
    "sp_stuff", "sp_location", "sp_pitching",
    "pb_stuff", "pb_command", "pb_overall", "pb_xRV100",
    # velocity, mix and movement
    "FBv", "pfxvFA", "pfxvSI", "pfxvSL", "pfxvCU", "pfxvCH", "pfxvFC",
    "pfxFA%", "pfxSI%", "pfxSL%", "pfxCU%", "pfxCH%", "pfxFC%", "pfxFS%",
    "pfxST%", "pfxKC%",
    "pfxFA-X", "pfxFA-Z", "pfxSI-X", "pfxSI-Z", "pfxSL-X", "pfxSL-Z",
    "pfxCU-X", "pfxCU-Z", "pfxCH-X", "pfxCH-Z",
    "pfxPace",
    "scH-Contact%", "scS-Contact%", "scO-Swing%", "scW-Swing%",
    "scZ-Contact%",
)

# Handedness as a single categorical, because the interaction is the point and
# a pair of booleans makes a tree spend two splits to say "lefty on lefty".
_HAND_CODE = {("L", "L"): 0, ("L", "R"): 1, ("R", "L"): 2, ("R", "R"): 3,
              ("S", "L"): 4, ("S", "R"): 5}


def _hand_code(bats: str, throws: str) -> float:
    return float(_HAND_CODE.get((bats[:1].upper(), throws[:1].upper()),
                                float("nan")))


# Named products. A gradient-boosted tree can find an interaction between two
# features it already has, but only by spending depth on it, and these four
# are the ones the experiment plan names as most likely to carry value: the
# hitter's run value against a pitch type times how often this pitcher throws
# it. Everything else is left to the trees.
# **Three usage columns had no pair and it was an oversight, not a decision.**
# Both halves were already in the feature set — the batter's run value against
# the pitch type and the pitcher's usage of it — and nobody joined them:
#
#   FS (splitter)     `wSF/C` x `pfxFS%`   — found on CHC @ SEA 2026-08-23:
#       Imanaga throws 32.8% splitters, his primary secondary, and Arozarena's
#       wSF/C is -3.139, by a distance his worst pitch (next worst +0.55). The
#       single strongest matchup signal on that board was invisible to the
#       model, while two of the six pairs that DID exist were NaN because he
#       throws neither a changeup nor a cutter.
#   ST (sweeper)      `wSL/C` x `pfxST%`   — same arm throws 15.0% sweepers.
#   KC (knuckle curve) `wCB/C` x `pfxKC%`
#
# The sweeper and knuckle-curve run values fold into the board's slider and
# curve columns respectively, so those are the right batter halves.
_ARSENAL_CORE = (("wFB/C", "pfxFA%", "fb"), ("wFB/C", "pfxSI%", "si"),
                 ("wSL/C", "pfxSL%", "sl"), ("wCB/C", "pfxCU%", "cu"),
                 ("wCH/C", "pfxCH%", "ch"), ("wCT/C", "pfxFC%", "fc"))
_ARSENAL_ADDED = (("wSF/C", "pfxFS%", "fs"), ("wSL/C", "pfxST%", "st"),
                  ("wCB/C", "pfxKC%", "kc"))

_ARSENAL_PAIRS = (_ARSENAL_CORE if _PAIRS_MODE == "core"
                  else _ARSENAL_CORE + _ARSENAL_ADDED)

# **A blank pitch-usage cell is a ZERO, not a missing value, and encoding it as
# NaN was a real bug.** Measured on the 2026 board: `pfxFS%` is present on 137
# of 801 arms and EXACTLY 0.0 appears zero times, with the smallest recorded
# value 0.0005. So the board writes a number whenever the pitch is thrown and
# leaves the cell blank otherwise — blank means "throws none of these", which
# is knowledge, not absence of it.
#
# Fed in as NaN, LightGBM routes every such row down one learned default branch
# and has to spend capacity separating "does not throw a splitter" from
# "genuinely unknown". Fed in as 0.0 the interaction `rv x usage` becomes
# exactly 0 — which is the truth: a hitter's splitter weakness is worth nothing
# against someone who throws none — and the column goes from 17% populated to
# 100%.
#
# Note this applies ONLY to the usage SHARES. `pfxvCH` (velocity) and
# `pfxCH-X/Z` (movement) are genuinely undefined for a pitch nobody throws and
# must stay NaN; a zero there would be a lie about a 0 mph changeup.
_USAGE_IS_ZERO_WHEN_BLANK = _PAIRS_MODE == "dense"

# Every distinct pitch type with its (batter run value, pitcher usage) columns.
# `fb`/`si` and `sl`/`st` and `cu`/`kc` share a batter half because the board
# carries one run-value column per FAMILY.
_ARSENAL_ALL = _ARSENAL_CORE + _ARSENAL_ADDED

MATCHUP_NAMES: Tuple[str, ...] = (
    ("hand_code", "same_hand",
     "x_barrel", "x_hardhit", "x_pull_gb", "x_la", "x_swstr", "x_chase",
     "x_zcontact", "x_batspeed_velo")
    + tuple(f"x_arsenal_{tag}" for _, _, tag in _ARSENAL_PAIRS)
    + (("x_arsenal_rv", "x_arsenal_cov") if _USAGE_IS_ZERO_WHEN_BLANK else ()))

CONTEXT_NAMES: Tuple[str, ...] = (
    "is_home", "park_tilt", "wx_tilt", "temp_f", "wind_mph", "wind_out",
    "altitude", "roof", "oaa", "of_arm", "framing", "venue_id", "day",
    "inning", "outs_before", "bases_before", "pit_bf", "tto", "is_starter",
    "lineup_slot_pa",
)

# **Six of those twenty are per-PLATE-APPEARANCE state, and a model that uses
# them cannot be deployed through the memoised adjuster.** The correction is
# looked up once per (batter, pitcher, side) and reused across 2,000 simulated
# games; if it depended on the inning and the base-out state it would have to
# be predicted per PA — 152,000 gradient-boosted evaluations a game against
# the ~200 the matchup needs.
#
# They stay in the DATASET because the question is worth asking: the engine
# ignores base-out state and times-through-order entirely when pricing a plate
# appearance, and `FATIGUE_DECLINE_PER_BF` is deliberately 0. A model that
# reads them ("Cs") is trained and scored at the PA level only. If it turns
# out to be worth much more than the deployable ones, the memo key is the
# thing to revisit, not the finding.
STATE_NAMES = frozenset((
    "inning", "outs_before", "bases_before", "pit_bf", "tto",
    "lineup_slot_pa"))

# Which per-PA state columns a variant may read. The point of naming SUBSETS
# is deployability, not accuracy: the adjuster is memoised per (batter,
# pitcher, side, is_starter), so every state column a model reads multiplies
# the number of rows that key has to enumerate.
#
#   base-out  24 states  ->  ~270 matchups x 24  =  6.5k rows/game
#   + inning  x9         ->  58k rows/game
#   pitcher-state only   ->  a handful of buckets, nearly free
#
# `bases_before` is the single highest-gain feature in the whole 219-column
# Cs model (1.19%), which is why the cheap pitcher-state-only key was never
# going to be the answer and had to be measured rather than assumed.
STATE_SPECS: Dict[str, frozenset] = {
    "none":     frozenset(),
    "all":      frozenset(STATE_NAMES),
    "baseout":  frozenset(("bases_before", "outs_before")),
    "pitstate": frozenset(("pit_bf", "tto")),
    "baseout_tto": frozenset(("bases_before", "outs_before", "tto")),
    "baseout_inn": frozenset(("bases_before", "outs_before", "inning")),
    "slot":     frozenset(("lineup_slot_pa",)),
}


FEATURE_NAMES: Tuple[str, ...] = (
    tuple(f"b_{c}" for c in BAT_COLS)
    + tuple(f"p_{c}" for c in PIT_COLS)
    + MATCHUP_NAMES
    + CONTEXT_NAMES
    # the incumbent's own opinion, as log-probabilities. Model B (flat
    # multiclass) needs them as features; Model C gets them as `init_score`
    # and does not, so the training script can drop this block by name.
    + tuple(f"base_{n}" for n in m.OUTCOME_NAMES)
    # and the two sides' shrunk vectors BEFORE they were combined, which the
    # baseline has already mixed together irreversibly
    + tuple(f"br_{n}" for n in m.OUTCOME_NAMES)
    + tuple(f"pr_{n}" for n in m.OUTCOME_NAMES)
)
N_FEATURES = len(FEATURE_NAMES)

# Feature blocks, by name, so an ablation is a list slice rather than a
# hand-counted index range.
FEATURE_BLOCKS: Dict[str, Tuple[int, int]] = {}
_o = 0
for _nm, _cols in (("bat", BAT_COLS), ("pit", PIT_COLS),
                   ("matchup", MATCHUP_NAMES), ("context", CONTEXT_NAMES),
                   ("base", m.OUTCOME_NAMES), ("bat_rates", m.OUTCOME_NAMES),
                   ("pit_rates", m.OUTCOME_NAMES)):
    FEATURE_BLOCKS[_nm] = (_o, _o + len(_cols))
    _o += len(_cols)
assert _o == N_FEATURES
del _o, _nm, _cols


def _player_features(row: Optional[dict], cols: Sequence[str]):
    import numpy as np
    return np.array([_f(row, c) for c in cols], dtype="float32")


def _matchup_features(brow: Optional[dict], prow: Optional[dict],
                      bats: str, throws: str):
    import numpy as np
    b, p = brow, prow
    same = (bats[:1].upper() == throws[:1].upper())
    vals = [
        _hand_code(bats, throws),
        1.0 if same else (0.0 if bats and throws else float("nan")),
        _f(b, "Barrel%") * _f(p, "Barrel%"),
        _f(b, "HardHit%") * _f(p, "HardHit%"),
        _f(b, "Pull%") * _f(p, "GB%"),
        _f(b, "LA") * _f(p, "LA"),
        _f(b, "SwStr%") * _f(p, "SwStr%"),
        _f(b, "O-Swing%") * _f(p, "O-Swing%"),
        _f(b, "Z-Contact%") * _f(p, "Z-Contact%"),
        _f(b, "AvgBatSpeed") * _f(p, "FBv"),
    ]
    if _USAGE_IS_ZERO_WHEN_BLANK:
        def _use(col):
            v = _f(p, col)
            return 0.0 if v != v else v          # blank -> throws none
        # usage 0 => the interaction is 0 whatever the batter half says, even
        # when his run value against that pitch is unknown: he will not see it.
        vals += [(0.0 if _use(pc) == 0.0 else _f(b, bc) * _use(pc))
                 for bc, pc, _ in _ARSENAL_PAIRS]
        # **The dense summary: the batter's expected run value per 100 against
        # THIS pitcher's actual mix.** One populated column instead of nine
        # sparse ones, which is what a 7-leaf tree with ~250 rounds can
        # actually use. Renormalised over the covered share so a pitcher whose
        # mix is only partly matched is not scored as though the rest were
        # neutral, and `cov` reports how much of the arsenal that is.
        num = den = 0.0
        for bc, pc, _ in _ARSENAL_ALL:
            u = _use(pc)
            if u <= 0.0:
                continue
            rv = _f(b, bc)
            if rv != rv:
                continue
            num += rv * u
            den += u
        vals.append(num / den if den > 0 else float("nan"))
        vals.append(den)
    else:
        vals += [_f(b, bc) * _f(p, pc) for bc, pc, _ in _ARSENAL_PAIRS]
    return np.array(vals, dtype="float32")


# --- game-level context, built ONCE and shared -----------------------------
# **The dataset builder and the live adjuster must produce the same row for
# the same plate appearance, and the only way to guarantee that is for them to
# call the same function.** Two copies of "venue, weather, park tilt, altitude,
# roof, day-of-season" would agree on the day they were written and drift the
# first time either is touched — and the failure is silent, because a model
# scored on one representation and applied to another still returns nine
# plausible probabilities. `pa_rates` was extracted out of `simulate_game` for
# exactly this reason; this is the same move one level up.
#
# `test_adjuster_matches_dataset` asserts the two rows are bit-identical on
# every non-state column, so a future divergence fails loudly.

def game_context(row: dict, save_dir: Path = m.SAVE_DIR) -> dict:
    """Everything about one game that every plate appearance in it shares."""
    wm = m._wm()
    venue = m.resolve_venue(row["venue"])
    weather = m._slate_weather(row)
    pdata = (wm.STADIUM_DATA.get(venue) or {}) if venue else {}
    return {
        "venue": venue,
        "weather": weather,
        # The deterministic half of the form axis: weather and park, shared by
        # both sides, with the game-form DRAW left at its mean of zero.
        "wx": m.weather_tilt(weather, venue),
        "park": {"home": m.park_run_tilt(venue, True),
                 "away": m.park_run_tilt(venue, False)},
        "vid": float(_venue_ids(save_dir).get(venue, -1)) if venue else -1.0,
        "alt": float(pdata.get("altitude") or 0.0),
        "roof": 1.0 if str(pdata.get("roof") or "").lower() in (
            "closed", "dome", "fixed") else 0.0,
        "temp": row.get("temp_f"),
        "wind": row.get("wind_mph"),
        "wind_out": m.wind_out_component(row.get("wind_mph"),
                                         row.get("wind_label") or ""),
        "day": float(int(row["date"][5:7]) * 31 + int(row["date"][8:10])),
        "starters": {int(row["home_sp"] or -1), int(row["away_sp"] or -1)},
    }


def context_features(gc: dict, pit_side, is_home_batting: bool,
                     is_starter: bool, state=None):
    """The 20-wide context block. `state` is the six per-PA columns, or None
    for the live adjuster, which cannot know them — they are masked out of
    every deployable model, so NaN there is dropped rather than imputed."""
    import numpy as np
    inn, ob, bb, bf, tto, slot = state if state is not None else (
        (np.nan,) * 6)
    temp, wind = gc["temp"], gc["wind"]
    return np.array([
        1.0 if is_home_batting else 0.0,
        gc["park"]["home" if is_home_batting else "away"], gc["wx"],
        float(temp) if temp is not None else np.nan,
        float(wind) if wind is not None else np.nan,
        gc["wind_out"], gc["alt"], gc["roof"],
        float(pit_side.oaa),
        float(pit_side.of_arm) if pit_side.of_arm is not None else np.nan,
        float(pit_side.framing), gc["vid"], gc["day"],
        float(inn), float(ob), float(bb), float(bf), float(tto),
        1.0 if is_starter else 0.0, float(slot),
    ], dtype="float32")


def _venue_ids(save_dir: Path = m.SAVE_DIR) -> Dict[str, int]:
    """A stable integer per park, so `venue_id` is a real categorical rather
    than an ordering. Derived from the sorted STADIUM_DATA keys, which do not
    change between runs — a hash would not survive a Python restart."""
    wm = m._wm()
    return {v: i for i, v in enumerate(sorted(wm.STADIUM_DATA))}


# --- the worker: one as-of cutoff, all its games ---------------------------
# One CUTOFF is the unit for the same reason it is in `_backtest_worker`:
# `build_rates_asof` is the expensive part and every game under a cutoff shares
# it. The forkserver rule applies identically — a worker RE-IMPORTS `mlb_sim`
# and gets the shipped constants back, so any configuration has to travel as
# DATA in the job (`_slate_overrides`), never as a rebound global in the parent.

def _dataset_worker(job):
    import numpy as np
    (cut, rows, season, save_dir, overrides, pa_by_game) = job
    m.__dict__.update(overrides)
    m._BOARDS.clear(); m._ASOF_BOARDS.clear()
    m._PRIOR_CURVE.clear(); m._PRIOR_LEAGUE.clear()
    m._SLATE_TABLES.clear(); m._DEPLOY.clear()
    m._PIT_ROWS.clear(); m._BAT_ROWS.clear()
    save_dir = Path(save_dir)

    bat_t, _ = m.build_rates_asof("bat", season, cut, save_dir=save_dir)
    pit_t, _ = m.build_rates_asof("pit", season, cut, save_dir=save_dir)
    bat_board = {i: r for r in (m.load_board_asof("bat", season, cut, save_dir)
                                or []) if (i := m._row_id(r)) is not None}
    pit_board = {i: r for r in (m.load_board_asof("pit", season, cut, save_dir)
                                or []) if (i := m._row_id(r)) is not None}
    hz = m.starter_hazard()
    bases = m.slate_sides(rows, bat_t, pit_t, season, hz, save_dir)
    venue_id = _venue_ids(save_dir)
    wm = m._wm()

    # Per-player feature vectors, built ONCE per cutoff. There are ~1,300
    # hitters and ~750 arms behind ~7,000 plate appearances, so building these
    # per PA would repeat the same 165 dictionary lookups five times over.
    bf_cache: Dict[int, "np.ndarray"] = {}
    pf_cache: Dict[int, "np.ndarray"] = {}

    def bat_feats(pid):
        got = bf_cache.get(pid)
        if got is None:
            got = _player_features(bat_board.get(pid), BAT_COLS)
            bf_cache[pid] = got
        return got

    def pit_feats(pid):
        got = pf_cache.get(pid)
        if got is None:
            got = _player_features(pit_board.get(pid), PIT_COLS)
            pf_cache[pid] = got
        return got

    X: List["np.ndarray"] = []
    Y: List[int] = []
    BASE: List["np.ndarray"] = []
    META: List[Tuple[int, int, int, int]] = []      # pk, day, batter, pitcher
    nan9 = np.full(9, np.nan, dtype="float32")
    day0 = None

    for row in rows:
        pas = pa_by_game.get(row["pk"])
        if not pas:
            continue
        hb, ab = bases.get(row["home"]), bases.get(row["away"])
        if hb is None or ab is None:
            continue
        home, _, _ = m._game_side(hb, row["home_sp"], row["home_lineup"],
                                  bat_t, pit_t, season, save_dir,
                                  row.get("home_catcher"))
        away, _, _ = m._game_side(ab, row["away_sp"], row["away_lineup"],
                                  bat_t, pit_t, season, save_dir,
                                  row.get("away_catcher"))
        gc = game_context(row, save_dir)
        starters = gc["starters"]

        # (batter, pitcher, half) -> composed vector. A hitter faces the same
        # arm two or three times a game and the vector does not change between
        # them, `faced` being inert while `FATIGUE_DECLINE_PER_BF` is 0.
        vec_cache: Dict[tuple, tuple] = {}
        slot_seen: Dict[Tuple[int, int], int] = {}

        for pa in pas:
            is_top = bool(pa["top"])
            bat_side, pit_side = (away, home) if is_top else (home, away)
            is_home_batting = not is_top
            key = (pa["bat"], pa["pit"], is_home_batting)
            got = vec_cache.get(key)
            if got is None:
                bat = (m.make_batter(pa["bat"], bat_t, season, save_dir)
                       or m.replacement_batter(season, save_dir))
                pit = m.make_pitcher(pa["pit"], pit_t,
                                     is_starter=pa["pit"] in starters)
                if pit is None:
                    pit = m.Pitcher(name="replacement",
                                    rates=m.replacement_pitcher_rates(),
                                    player_id=pa["pit"],
                                    throws=pa["ph"])
                base = m.pa_rates(
                    bat, pit, faced=pa["bf"], oaa=pit_side.oaa,
                    framing=pit_side.framing, is_home=is_home_batting,
                    tilt=(gc["wx"] + gc["park"]["home" if is_home_batting
                                              else "away"]))
                got = (bat, pit, np.asarray(base, dtype="float32"))
                vec_cache[key] = got
            bat, pit, base = got

            brow = bat_board.get(pa["bat"])
            prow = pit_board.get(pa["pit"])
            # `bat.bats` and `pit.throws` come from the RATE TABLE, not from
            # the play-by-play, because that is what `pa_rates` platoons on.
            # The hand StatsAPI reports is the side a switch-hitter actually
            # took, and it is carried as a feature rather than substituted in
            # — substituting it would make the baseline stop matching the
            # engine, which is the one thing this table must not do.
            mfeat = _matchup_features(brow, prow, bat.bats, pit.throws)
            slot_key = (pa["bat"], 1 if is_top else 0)
            slot_pa = slot_seen.get(slot_key, 0)
            slot_seen[slot_key] = slot_pa + 1
            ctx = context_features(
                gc, pit_side, is_home_batting, pit.is_starter,
                state=(pa["inn"], pa["ob"], pa["bb"], pa["bf"], pa["tto"],
                       slot_pa))

            X.append(np.concatenate((
                bat_feats(pa["bat"]), pit_feats(pa["pit"]), mfeat, ctx,
                np.log(np.maximum(base, 1e-9)),
                np.log(np.maximum(np.asarray(bat.rates, dtype="float32"),
                                  1e-9)),
                np.log(np.maximum(np.asarray(pit.rates, dtype="float32"),
                                  1e-9)),
            )))
            Y.append(pa["o"])
            BASE.append(base)
            META.append((pa["pk"], int(gc["day"]), pa["bat"], pa["pit"]))

    if not X:
        return None
    return (np.vstack(X), np.array(Y, dtype="int8"), np.vstack(BASE),
            np.array(META, dtype="int64"), cut)


def build_dataset(season: int = 2026, workers: Optional[int] = None,
                  save_dir: Path = m.SAVE_DIR, refresh: bool = False,
                  limit: Optional[int] = None, verbose: bool = True) -> Path:
    """Every plate appearance of a season as a feature row, time-safe.

    Built under `ab_configure({}, season)` — the leak-free `base` arm — so the
    baseline column IS what the `base` arm will simulate, not something that
    resembles it.
    """
    import numpy as np
    dest = dataset_path(season, save_dir)
    if dest.exists() and not refresh:
        if verbose:
            print(f"[dataset] {season}: cached at {dest}")
        return dest

    slate = m.season_slate(season, save_dir=save_dir)
    cutoffs = m.available_asof_cutoffs(season, save_dir)
    if not cutoffs:
        raise FileNotFoundError(
            f"mlb_ml: no as-of boards for {season}. Run `mlb_sim.py asof`.")
    pa = load_pa(season, save_dir)
    pa_by_game: Dict[int, List[dict]] = {}
    for r in pa:
        pa_by_game.setdefault(r["pk"], []).append(r)

    by_cutoff: Dict[str, List[dict]] = {}
    skipped = 0
    for row in slate:
        cut = m.asof_cutoff_for(row["date"], cutoffs)
        if cut is None:
            skipped += 1
            continue
        by_cutoff.setdefault(cut, []).append(row)
    if limit:
        keep = sorted(by_cutoff)[:limit]
        by_cutoff = {k: by_cutoff[k] for k in keep}

    # The same two globals `backtest` sets before capturing the overrides.
    m.DEPLOY_SEASON = season
    m.ab_configure({}, season)
    m.assert_density_inputs(season, save_dir)
    overrides = m._slate_overrides()
    jobs = [(cut, by_cutoff[cut], season, str(save_dir), overrides,
             {r["pk"]: pa_by_game.get(r["pk"], []) for r in by_cutoff[cut]})
            for cut in sorted(by_cutoff)]
    workers = workers or max(1, min(len(jobs), (m.os.cpu_count() or 4) - 2))
    if verbose:
        print(f"[dataset] {season}: {sum(len(v) for v in by_cutoff.values())} "
              f"games across {len(jobs)} cutoffs ({skipped} before the first "
              f"cutoff, skipped), {workers} workers", flush=True)

    parts = []
    t0 = time.time()
    if workers <= 1:
        for j in jobs:
            got = _dataset_worker(j)
            if got:
                parts.append(got)
    else:
        with m.multiprocessing.Pool(workers) as pool:
            for got in pool.imap_unordered(_dataset_worker, jobs):
                if got:
                    parts.append(got)
                    if verbose:
                        n = sum(len(p[1]) for p in parts)
                        print(f"[dataset] {season} {len(parts)}/{len(jobs)} "
                              f"cutoffs, {n:,} PA, {time.time()-t0:.0f}s",
                              flush=True)
    if not parts:
        raise RuntimeError(f"mlb_ml: dataset {season} produced no rows")
    parts.sort(key=lambda p: p[4])
    X = np.vstack([p[0] for p in parts])
    Y = np.concatenate([p[1] for p in parts])
    BASE = np.vstack([p[2] for p in parts])
    META = np.vstack([p[3] for p in parts])
    order = np.lexsort((META[:, 0], META[:, 1]))     # by day, then game
    X, Y, BASE, META = X[order], Y[order], BASE[order], META[order]

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, X=X, y=Y, base=BASE, meta=META,
                        names=np.array(FEATURE_NAMES),
                        season=np.array([season]))
    tmp.replace(dest)
    if verbose:
        print(f"[dataset] {season}: {X.shape[0]:,} rows x {X.shape[1]} "
              f"features -> {dest} ({dest.stat().st_size/1e6:.0f} MB, "
              f"{time.time()-t0:.0f}s)", flush=True)
    return dest


def load_dataset(season: int, save_dir: Path = m.SAVE_DIR):
    import numpy as np
    path = dataset_path(season, save_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"mlb_ml: no dataset for {season} at {path}. "
            f"Run `python mlb_ml.py dataset {season}` first.")
    d = np.load(path, allow_pickle=False)
    return {"X": d["X"], "y": d["y"], "base": d["base"], "meta": d["meta"],
            "names": [str(s) for s in d["names"]], "season": int(d["season"][0])}


def dataset_report(season: int = 2026, save_dir: Path = m.SAVE_DIR) -> dict:
    """Is the BASELINE column the incumbent, and does it beat the league?

    Two things are being checked and they are different questions:

      * the baseline's own PA-level log loss against a fixed league vector.
        If shrinkage + log5 + context does not beat the league mean, the
        column is wrong — there is no version of this model in which it does
        not;
      * per-outcome predicted-versus-observed. A rate layer can have the right
        aggregate and the wrong shape, which is trap 3 in `sim_state.md`, and
        the shape is what a residual model is supposed to fix.
    """
    import numpy as np
    d = load_dataset(season, save_dir)
    y, base = d["y"], d["base"]
    n = len(y)
    lg = np.asarray(m.LEAGUE_BASELINE, dtype="float64")
    idx = np.arange(n)
    ll_base = float(-np.log(np.maximum(base[idx, y], 1e-9)).mean())
    ll_lg = float(-np.log(np.maximum(lg[y], 1e-9)).mean())
    obs = np.bincount(y, minlength=9) / n
    pred = base.mean(axis=0)
    nan_share = float(np.isnan(d["X"]).mean())

    print(f"\nDATASET {season}   {n:,} plate appearances x "
          f"{d['X'].shape[1]} features   ({nan_share:.1%} of cells missing)")
    print(f"  PA log loss   league {ll_lg:.5f}   baseline {ll_base:.5f}   "
          f"gain {ll_lg - ll_base:+.5f}")
    print(f"\n  {'outcome':9s} {'predicted':>10s} {'observed':>10s} "
          f"{'diff':>9s} {'rel':>8s}")
    for i, nm in enumerate(m.OUTCOME_NAMES):
        rel = (pred[i] - obs[i]) / obs[i] if obs[i] else float("nan")
        print(f"  {nm:9s} {pred[i]:10.4f} {obs[i]:10.4f} "
              f"{pred[i]-obs[i]:+9.4f} {rel:+8.1%}")
    return {"season": season, "n": n, "ll_base": ll_base, "ll_league": ll_lg,
            "pred": pred.tolist(), "obs": obs.tolist()}


# ===========================================================================
# 3. THE MODELS — B (flat), C (residual), D (blend)
# ===========================================================================
# **Two LightGBM behaviours decide whether this works at all, and both fail
# SILENTLY.** They were verified on synthetic data with a known answer before
# a line of this was trained, because each produces a plausible-looking model
# rather than an error.
#
#   1. `init_score` for a multiclass model is CLASS-MAJOR — `logit.T.reshape(-1)`,
#      every row's class 0, then every row's class 1. Handing it row-major is
#      accepted without complaint. On a nine-outcome problem with our real
#      marginals, a base that IS the truth scores 1.723 class-major and 3.581
#      row-major: the wrong order does not error, it just discards the prior
#      and relearns from a scrambled one.
#   2. `predict()` does NOT add `init_score` back. The probabilities it returns
#      are softmax of the BOOSTED PART ONLY. The prediction is
#      `softmax(log(base) + predict(raw_score=True))`, and forgetting it gives
#      a model that looks trained and has thrown its prior away.
#      LightGBM's own `multi_logloss` metric, confusingly, DOES include it —
#      so the training log looks right while `predict` is wrong.
#
# The whole point of the residual architecture is that (1) makes
#
#     log P_final,i = log P_base,i + f_i(X)
#
# the literal objective LightGBM optimises, rather than something approximated
# by feeding the baseline in as nine more columns and hoping.

MODEL_DIR = m.SAVE_DIR / "mlml" / DATASET_VERSION / "models"

LGB_PARAMS: Dict[str, object] = {
    "objective": "multiclass",
    "num_class": m.N_OUTCOMES,
    "metric": "multi_logloss",
    "learning_rate": 0.03,
    "num_leaves": 63,
    "min_data_in_leaf": 400,
    "feature_fraction": 0.65,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 10.0,
    "num_threads": 0,
    "verbose": -1,
    "seed": 17,
}

# (name, uses the baseline as `init_score`, keeps the `base` feature block).
#
# `C` and `Cb` differ only in whether the residual can SEE the baseline it is
# correcting. Both are legitimate — seeing it lets the correction depend on
# where the prior already sits, not seeing it forces a correction that is a
# pure function of the inputs — and the plan does not settle which, so both
# run and the data settles it.
# (name, uses `init_score`, keeps the `base` block, keeps the per-PA STATE
# block). Only the first three are deployable — see `STATE_NAMES`.
MODEL_SPECS: Dict[str, Tuple[bool, bool, bool]] = {
    "B": (False, True, False),   # flat multiclass; the baseline is nine columns
    "C": (True, False, False),   # residual; the baseline is prior, not feature
    "Cb": (True, True, False),   # residual that can also read its own prior
    "Cs": (True, False, True),   # + base-out state and TTO. DIAGNOSTIC ONLY.
    # deployability ablation — which state columns actually carry the gain
    "Cs_baseout":     (True, False, STATE_SPECS["baseout"]),
    "Cs_pitstate":    (True, False, STATE_SPECS["pitstate"]),
    "Cs_baseout_tto": (True, False, STATE_SPECS["baseout_tto"]),
    "Cs_baseout_inn": (True, False, STATE_SPECS["baseout_inn"]),
    "Cs_slot":        (True, False, STATE_SPECS["slot"]),
}

# Train / validate / test, walked forward. Two folds, because the standing bar
# in this project is a result with the SAME SIGN in both seasons (sim_state.md
# 3d.1) — a single fold cannot clear it however good the number looks.
FOLDS: Dict[str, Tuple[Tuple[int, ...], int, int]] = {
    "f25": ((2023,), 2024, 2025),
    "f26": ((2023, 2024), 2025, 2026),
}


def _softmax(z):
    import numpy as np
    e = np.exp(z - z.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def _feature_mask(keep_base: bool, keep_state=False):
    """Column selector, BY NAME.

    Deliberately not an index range. The state columns are not contiguous
    (`is_starter` sits between them), and a range that happened to be right
    today would silently start dropping the wrong column the first time a
    name is inserted into `CONTEXT_NAMES`.

    `keep_state` is False (drop all), True (keep all), or a SET of state
    column names to keep — the last is what the deployability ablation needs.
    """
    import numpy as np
    if keep_state is True:
        keep = set(STATE_NAMES)
    elif not keep_state:
        keep = set()
    else:
        keep = set(keep_state)
        bad = keep - set(STATE_NAMES)
        if bad:
            raise KeyError(f"mlb_ml: not state columns: {sorted(bad)}")
    mask = np.ones(N_FEATURES, dtype=bool)
    for i, nm in enumerate(FEATURE_NAMES):
        if not keep_base and nm.startswith("base_"):
            mask[i] = False
        if nm in STATE_NAMES and nm not in keep:
            mask[i] = False
    return mask


def _stack(seasons: Sequence[int], save_dir: Path = m.SAVE_DIR):
    import numpy as np
    parts = [load_dataset(s, save_dir) for s in seasons]
    return (np.vstack([p["X"] for p in parts]),
            np.concatenate([p["y"] for p in parts]),
            np.vstack([p["base"] for p in parts]),
            np.vstack([p["meta"] for p in parts]))


def model_path(tag: str, fold: str, save_dir: Path = m.SAVE_DIR) -> Path:
    return (Path(save_dir) / "mlml" / DATASET_VERSION / "models"
            / f"{tag}_{fold}.txt")


def train_model(tag: str, fold: str, save_dir: Path = m.SAVE_DIR,
                params: Optional[dict] = None, rounds: int = 4000,
                patience: int = 150, refresh: bool = False,
                verbose: bool = True) -> dict:
    """One model on one fold. Early-stopped on the VALIDATION season."""
    import numpy as np
    import lightgbm as lgb

    if tag not in MODEL_SPECS:
        raise KeyError(f"mlb_ml: unknown model {tag!r}; "
                       f"have {sorted(MODEL_SPECS)}")
    if fold not in FOLDS:
        raise KeyError(f"mlb_ml: unknown fold {fold!r}; have {sorted(FOLDS)}")
    use_init, keep_base, keep_state = MODEL_SPECS[tag]
    tr_seasons, va_season, _ = FOLDS[fold]

    dest = model_path(tag, fold, save_dir)
    meta_path = dest.with_suffix(".json")
    if dest.exists() and meta_path.exists() and not refresh:
        with open(meta_path) as fh:
            got = json.load(fh)
        if verbose:
            print(f"[train] {tag}/{fold} cached ({got['best_iter']} rounds, "
                  f"valid {got['valid_logloss']:.5f})")
        return got

    Xtr, ytr, btr, _ = _stack(tr_seasons, save_dir)
    Xva, yva, bva, _ = _stack([va_season], save_dir)
    mask = _feature_mask(keep_base, keep_state)
    names = [n for n, k in zip(FEATURE_NAMES, mask) if k]
    Xtr, Xva = Xtr[:, mask], Xva[:, mask]

    kw_tr, kw_va = {}, {}
    if use_init:
        # CLASS-MAJOR. See the block comment above — row-major is accepted
        # silently and throws the prior away.
        kw_tr["init_score"] = np.log(np.maximum(btr, 1e-9)).T.reshape(-1)
        kw_va["init_score"] = np.log(np.maximum(bva, 1e-9)).T.reshape(-1)

    dtr = lgb.Dataset(Xtr, label=ytr, feature_name=names, **kw_tr)
    dva = lgb.Dataset(Xva, label=yva, feature_name=names, reference=dtr,
                      **kw_va)
    ev: Dict[str, dict] = {}
    t0 = time.time()
    if verbose:
        print(f"[train] {tag}/{fold}: train {tr_seasons} "
              f"({len(ytr):,} PA) -> valid {va_season} ({len(yva):,} PA), "
              f"{len(names)} features, init_score={use_init}", flush=True)
    bst = lgb.train({**LGB_PARAMS, **(params or {})}, dtr,
                    num_boost_round=rounds, valid_sets=[dva],
                    valid_names=["valid"],
                    callbacks=[lgb.early_stopping(patience, verbose=False),
                               lgb.record_evaluation(ev),
                               lgb.log_evaluation(200 if verbose else 0)])
    dest.parent.mkdir(parents=True, exist_ok=True)
    bst.save_model(str(dest), num_iteration=bst.best_iteration)

    # The validation number is recomputed HERE rather than read off the
    # training log, because LightGBM's metric includes `init_score` and
    # `predict` does not — recomputing through the SAME path inference uses is
    # what catches the discrepancy if it is ever reintroduced.
    # **The per-class CENTRE, measured on the VALIDATION season.**
    #
    # A residual is free to learn a constant log-shift per outcome, and it
    # does — `HBP -0.091`, `3B -0.088` on the f26 fit. That shift is the
    # TRAINING seasons' run environment, not knowledge, and carrying it into a
    # season whose levels have drifted is pure drag: scored on the test season
    # the constant part of the residual is worth -0.00021 to -0.00026 nats
    # while the row-varying part is worth +0.00300. Subtracting it makes the
    # model strictly better and hands the LEVEL back to the incumbent, which
    # is where it belongs — the rate layer is rebased, projected and shrunk
    # against the season being priced, and the residual is not.
    #
    # Measured on VALIDATION, never on test. Train-set centring recovers most
    # of it (+0.00295 vs +0.00302 on f26) but validation is both out-of-fit
    # and legitimately available, being the set early stopping already used.
    #
    # This is trap 7 in `sim_state.md`, recorded five times: centre on the
    # population you actually apply it to.
    centre = ((bst.predict(Xva, raw_score=True).mean(axis=0)).tolist()
              if use_init else [0.0] * m.N_OUTCOMES)
    P = predict_proba(bst, Xva, bva if use_init else None)
    ll = float(-np.log(np.maximum(P[np.arange(len(yva)), yva], 1e-12)).mean())
    lgb_ll = ev["valid"]["multi_logloss"][bst.best_iteration - 1]
    if abs(ll - lgb_ll) > 1e-6:
        raise RuntimeError(
            f"mlb_ml: {tag}/{fold} validation log loss disagrees with "
            f"LightGBM's own metric ({ll:.6f} vs {lgb_ll:.6f}). The "
            f"init_score path is wrong — see the block comment in section 3.")
    base_ll = float(
        -np.log(np.maximum(bva[np.arange(len(yva)), yva], 1e-12)).mean())
    got = {"tag": tag, "fold": fold, "train_seasons": list(tr_seasons),
           "valid_season": va_season, "test_season": FOLDS[fold][2],
           "use_init": use_init, "keep_base": keep_base,
           # a frozenset is not JSON-serialisable and a bare bool loses which
           # COLUMNS the variant read — store the names, sorted, so the meta
           # says exactly what the model is allowed to see
           "keep_state": (sorted(keep_state) if not isinstance(keep_state, bool)
                          else keep_state),
           "deployable": not keep_state,
           "centre": centre,
           "features": names, "best_iter": int(bst.best_iteration),
           "valid_logloss": ll, "valid_base_logloss": base_ll,
           "valid_gain": base_ll - ll, "seconds": round(time.time() - t0, 1)}
    with open(meta_path, "w") as fh:
        json.dump(got, fh, indent=1)
    if verbose:
        print(f"[train] {tag}/{fold}: {got['best_iter']} rounds, valid "
              f"{ll:.5f} vs baseline {base_ll:.5f} "
              f"(gain {base_ll - ll:+.5f}) [{got['seconds']:.0f}s]",
              flush=True)
    return got


def predict_proba(bst, X, base=None, centre=None):
    """The model's nine probabilities.

    `base` non-None means the booster was trained with `init_score` and its
    raw output is a RESIDUAL — it has to be added back, because `predict` does
    not do it. `centre` is the per-class constant to strip first; see
    `train_model`.
    """
    import numpy as np
    raw = bst.predict(X, raw_score=True)
    if centre is not None:
        raw = raw - np.asarray(centre, dtype="float64")
    if base is None:
        return _softmax(raw)
    return _softmax(np.log(np.maximum(base, 1e-9)) + raw)


# --- centring on the population the model is APPLIED to --------------------
# `train_model` stamps a centre measured on the VALIDATION season. That kills
# the training-season level, and the A/B showed it does not kill enough: the
# game-level result was dominated by a run-level shift of +-0.1 runs whose SIGN
# FLIPPED between 2025 and 2026, swamping a matchup signal worth +0.003 nats.
# Validation-to-test drift is the residue.
#
# So centre on the season being priced instead. This uses FEATURES ONLY — which
# games were played, which lineups, which boards — and never an outcome, so it
# is not an outcome leak. It IS mildly forward-looking: an April game is priced
# with a constant computed from the whole season's feature distribution. That
# is acceptable for a DIAGNOSTIC whose question is "was the level drift the
# whole story"; a shippable version would use a trailing window, and this note
# exists so nobody ships the diagnostic by mistake.
#
# **The unit is the SEASON, never the game.** Centring per game would strip the
# game-to-game level variation entirely — which is precisely the quantity a
# totals market prices — and would guarantee a null by construction rather
# than measure one.
_APPLIED_CENTRE: Dict[tuple, list] = {}


def applied_centre(tag: str, fold: str, season: int, as_of: str = "",
                   save_dir: Path = m.SAVE_DIR) -> list:
    """The residual's mean, measured on the population it is APPLIED to.

    `train_model` stamps a centre measured on the VALIDATION season. That kills
    the training-season level and the A/B showed it does not kill enough: the
    game-level result was dominated by a run-level shift of +-0.1 runs whose
    SIGN FLIPPED between 2025 and 2026, swamping a matchup signal worth +0.003
    nats. Validation-to-test drift is the residue.

    **`as_of` is what makes this honest, and it is not optional.** Centring on
    the whole season being priced would use the feature distribution of games
    that have not been played yet — no outcome leaks, but an April game would
    be priced with a constant computed partly from September, and this project
    does not get to call that acceptable just because the leak is indirect. So
    the constant comes from plate appearances STRICTLY BEFORE the cutoff, which
    is exactly what a live model has: the season to date.

    The first version of this took the whole season and was labelled a
    diagnostic. It was then run as an A/B arm anyway and quoted as the headline
    result, which is how a caveat in a docstring turns into a number in a
    table. The whole-season path is gone rather than deprecated.

    Before the first cutoff there is nothing to centre on and the validation
    centre stands — which is the honest answer for opening day, not a
    fallback.
    """
    key = (tag, fold, int(season), str(as_of), str(save_dir))
    got = _APPLIED_CENTRE.get(key)
    if got is None:
        import numpy as np
        bst, meta = load_model(tag, fold, save_dir)
        d = load_dataset(season, save_dir)
        mask = _feature_mask(meta["keep_base"], meta.get("keep_state", False))
        X, meta_rows = d["X"], d["meta"]
        if as_of:
            # meta column 1 is the day index `month * 31 + day`, built by
            # `game_context`; comparing it to the cutoff the same way keeps
            # this on ONE definition of "before".
            cut_day = int(as_of[5:7]) * 31 + int(as_of[8:10])
            sel = meta_rows[:, 1] < cut_day
            if sel.sum() < 5000:
                _APPLIED_CENTRE[key] = list(meta["centre"] or [0.0] * 9)
                return _APPLIED_CENTRE[key]
            X = X[sel]
        got = bst.predict(X[:, mask],
                          raw_score=True).mean(axis=0).tolist()
        _APPLIED_CENTRE[key] = got
    return got


_BOOSTERS: Dict[tuple, object] = {}


def load_model(tag: str, fold: str, save_dir: Path = m.SAVE_DIR):
    import lightgbm as lgb
    key = (tag, fold, str(save_dir))
    got = _BOOSTERS.get(key)
    if got is None:
        path = model_path(tag, fold, save_dir)
        meta_path = path.with_suffix(".json")
        if not path.exists():
            raise FileNotFoundError(
                f"mlb_ml: no model {tag}/{fold} at {path}. "
                f"Run `python mlb_ml.py train {tag} --fold {fold}`.")
        with open(meta_path) as fh:
            meta = json.load(fh)
        got = (lgb.Booster(model_file=str(path)), meta)
        _BOOSTERS[key] = got
    return got


def model_proba(tag: str, fold: str, X, base,
                save_dir: Path = m.SAVE_DIR):
    """Full-dataset X (all 228 columns) -> the model's probabilities."""
    bst, meta = load_model(tag, fold, save_dir)
    mask = _feature_mask(meta["keep_base"], meta.get("keep_state", False))
    return predict_proba(bst, X[:, mask],
                         base if meta["use_init"] else None,
                         centre=meta.get("centre"))


# ---------------------------------------------------------------------------
# Level 1 — outcome calibration, per the experiment plan section 8
# ---------------------------------------------------------------------------
# **Aggregate log loss is not sufficient and the plan says so.** A model can
# improve the pooled number while flattening the HOME-RUN distribution across
# plate appearances, and a flattened HR distribution is strictly worse for
# totals however good the log loss looks — the run distribution's whole right
# tail is home runs. So dispersion is reported per outcome alongside the
# calibration, and it is the column to read second.

def _calibration(p, hit, bins: int = 10) -> float:
    """Worst absolute deviation between predicted and observed, over
    equal-COUNT bins of the predicted probability. Equal-count rather than
    equal-width: 3B probabilities live in a range of 0.002 and equal-width
    bins would put every row in one of them."""
    import numpy as np
    order = np.argsort(p)
    worst = 0.0
    for chunk in np.array_split(order, bins):
        if len(chunk) < 50:
            continue
        worst = max(worst, abs(float(p[chunk].mean() - hit[chunk].mean())))
    return worst


def score_pa(P, y, base, label: str = "") -> dict:
    """Per-outcome calibration, Brier and dispersion for one prediction set."""
    import numpy as np
    n = len(y)
    idx = np.arange(n)
    ll = float(-np.log(np.maximum(P[idx, y], 1e-12)).mean())
    ll_base = float(-np.log(np.maximum(base[idx, y], 1e-12)).mean())
    rows = []
    for i in range(m.N_OUTCOMES):
        hit = (y == i).astype("float64")
        rows.append({
            "outcome": m.OUTCOME_NAMES[i],
            "pred": float(P[:, i].mean()),
            "obs": float(hit.mean()),
            "brier": float(((P[:, i] - hit) ** 2).mean()),
            "brier_base": float(((base[:, i] - hit) ** 2).mean()),
            "sd": float(P[:, i].std()),
            "sd_base": float(base[:, i].std()),
            "cal": _calibration(P[:, i], hit),
            "cal_base": _calibration(base[:, i], hit),
        })
    return {"label": label, "n": n, "logloss": ll, "logloss_base": ll_base,
            "gain": ll_base - ll, "outcomes": rows}


def print_pa_score(sc: dict) -> None:
    print(f"\n  {sc['label']}   n {sc['n']:,}   log loss {sc['logloss']:.5f}  "
          f"(baseline {sc['logloss_base']:.5f}, gain {sc['gain']:+.5f})")
    print(f"    {'outcome':9s} {'pred':>8s} {'obs':>8s} {'rel':>7s} "
          f"{'Brier x1e4':>11s} {'vs base':>9s} {'sd/base':>8s} "
          f"{'cal':>7s} {'cal base':>9s}")
    for r in sc["outcomes"]:
        rel = (r["pred"] - r["obs"]) / r["obs"] if r["obs"] else float("nan")
        db = (r["brier"] - r["brier_base"]) * 1e4
        sdr = r["sd"] / r["sd_base"] if r["sd_base"] else float("nan")
        print(f"    {r['outcome']:9s} {r['pred']:8.4f} {r['obs']:8.4f} "
              f"{rel:+7.1%} {r['brier']*1e4:11.3f} {db:+9.3f} "
              f"{sdr:8.3f} {r['cal']:7.4f} {r['cal_base']:9.4f}")


BLEND_ALPHAS = (0.0, 0.10, 0.25, 0.50, 0.75, 1.00)


def score_models(fold: str = "f26",
                 tags: Sequence[str] = ("B", "C", "Cb", "Cs"),
                 save_dir: Path = m.SAVE_DIR,
                 alphas: Sequence[float] = BLEND_ALPHAS) -> dict:
    """Level 1 for every model on a fold's TEST season, plus the blend sweep.

    The test season is never seen by training or by early stopping, and the
    baseline column it is scored against is the one the `base` A/B arm will
    simulate — so a gain here is directly the quantity section 9 of the plan
    asks to isolate.
    """
    import numpy as np
    _, _, test_season = FOLDS[fold]
    d = load_dataset(test_season, save_dir)
    X, y, base = d["X"], d["y"], d["base"]
    print(f"\n=== FOLD {fold}: test season {test_season}, {len(y):,} PA ===")
    print_pa_score(score_pa(base, y, base, "baseline (incumbent)"))

    out = {"fold": fold, "test_season": test_season, "models": {}}
    for tag in tags:
        P = model_proba(tag, fold, X, base, save_dir)
        sc = score_pa(P, y, base, f"model {tag}")
        print_pa_score(sc)
        blends = []
        for a in alphas:
            Pb = a * P + (1.0 - a) * base
            ll = float(-np.log(np.maximum(
                Pb[np.arange(len(y)), y], 1e-12)).mean())
            blends.append({"alpha": a, "logloss": ll})
        best = min(blends, key=lambda r: r["logloss"])
        print(f"    blend  " + "  ".join(
            f"a={r['alpha']:.2f} {r['logloss']:.5f}" for r in blends))
        print(f"    best alpha {best['alpha']:.2f} "
              f"({best['logloss']:.5f}, gain "
              f"{sc['logloss_base'] - best['logloss']:+.5f})")
        out["models"][tag] = {"score": sc, "blends": blends, "best": best}
    return out


def feature_report(tag: str = "C", fold: str = "f26", top: int = 40,
                   save_dir: Path = m.SAVE_DIR) -> None:
    """Which features the residual actually uses, by total gain."""
    bst, meta = load_model(tag, fold, save_dir)
    gain = bst.feature_importance("gain")
    names = bst.feature_name()
    tot = float(gain.sum()) or 1.0
    order = sorted(range(len(names)), key=lambda i: -gain[i])
    by_block: Dict[str, float] = {}
    for i, nm in enumerate(names):
        blk = ("bat" if nm.startswith("b_") else
               "pit" if nm.startswith("p_") else
               "base" if nm.startswith("base_") else
               "bat_rates" if nm.startswith("br_") else
               "pit_rates" if nm.startswith("pr_") else
               "matchup" if nm.startswith("x_") or nm in
               ("hand_code", "same_hand") else "context")
        by_block[blk] = by_block.get(blk, 0.0) + float(gain[i])
    print(f"\nFEATURE GAIN  {tag}/{fold}  ({meta['best_iter']} rounds)")
    print("  by block:")
    for blk, g in sorted(by_block.items(), key=lambda kv: -kv[1]):
        print(f"    {blk:10s} {g/tot:6.1%}")
    print(f"  top {top}:")
    for i in order[:top]:
        print(f"    {names[i]:26s} {gain[i]/tot:6.2%}")


# ===========================================================================
# 4. THE ADJUSTER — the trained correction, inside the simulator
# ===========================================================================
# `mlb_sim.simulate_game` takes an `ml` argument and applies whatever it
# returns as the LAST step of `pa_rates`, as multipliers on the fully composed
# vector. Multipliers rather than a replacement vector, because tonight's
# game-form draw is applied before this point and the model never saw it — a
# replacement would silently overwrite the dispersion the engine's totals
# calibration depends on.
#
# **It is memoised per (batter, pitcher, side), and that is what makes it
# affordable.** A game is ~76 plate appearances and an A/B arm plays it 2,000
# times; predicting per PA would be 152,000 gradient-boosted evaluations for a
# game with about 200 distinct matchups in it. The memo is why models that
# read per-PA state ("Cs") are not deployable through this path — see
# `STATE_NAMES`.

def _blend(P_ml, P_base, mode: str, alpha: float):
    """Model D from the plan. `mode` "ml" is alpha = 1 by definition."""
    if mode == "ml" or alpha >= 1.0:
        return P_ml
    return alpha * P_ml + (1.0 - alpha) * P_base


class GameAdjuster:
    """One game's rate correction, as a callable the simulator can hold."""

    def __init__(self, tag: str, fold: str, season: int, as_of: str,
                 row: dict, home, away, mode: str = "ml", alpha: float = 1.0,
                 save_dir: Path = m.SAVE_DIR):
        import numpy as np
        self.np = np
        if (m.ML_HIER_NODES or ""):
            self.bst, self.meta = None, {"keep_base": False,
                                         "keep_state": False,
                                         "use_init": True, "deployable": True,
                                         "centre": None}
        else:
            self.bst, self.meta = load_model(tag, fold, save_dir)
        if not self.meta.get("deployable", True):
            raise ValueError(
                f"mlb_ml: model {tag}/{fold} reads per-PA state "
                f"({sorted(STATE_NAMES)}) and cannot be served through the "
                f"memoised adjuster. It is a PA-level diagnostic only.")
        self.mask = _feature_mask(self.meta["keep_base"],
                                  self.meta.get("keep_state", False))
        self.centre = (applied_centre(tag, fold, season, as_of, save_dir)
                       if m.ML_SELF_CENTRE else self.meta.get("centre"))
        # The HIERARCHY path. `alpha` is applied INSIDE `hier_proba`, in logit
        # space at each node, so `_blend` must not touch it again afterwards —
        # scaling a residual and then mixing the result with the baseline
        # applies the shrink twice and is not what either knob means.
        self.hier = [x for x in (m.ML_HIER_NODES or "").split(",") if x]
        if self.hier == ["all"]:
            self.hier = list(NODE_NAMES)
        self.use_init = self.meta["use_init"]
        self.mode, self.alpha = mode, float(alpha)
        self.season, self.save_dir, self.fold = season, save_dir, fold
        self.gc = game_context(row, save_dir)
        self.sides = {True: home, False: away}     # is_home_batting -> ...
        # **An empty `as_of` means LIVE**, and reads the current board rather
        # than a frozen snapshot. A backtest must use the snapshot or it leaks;
        # a projection of tonight's game legitimately has the season to date,
        # and handing it a cutoff board would price tonight on data from the
        # last time boards were cached.
        def _board(side):
            rows = (m.load_board_asof(side, season, as_of, save_dir) if as_of
                    else m.load_board(side, season, save_dir)) or []
            return {i: r for r in rows if (i := m._row_id(r)) is not None}
        self.bat_board, self.pit_board = _board("bat"), _board("pit")
        self._bf: Dict[Optional[int], object] = {}
        self._pf: Dict[Optional[int], object] = {}
        self._memo: Dict[tuple, Dict[int, float]] = {}
        self.n_predict = 0
        self.n_batched = 0
        self._prime(home, away)

    # --- one predict call for the whole game -----------------------------
    # **Batched, and the difference is 35x of wall clock.** A single-row
    # LightGBM call is ~2 ms of Python and marshalling overhead against
    # microseconds of actual arithmetic, and a game has ~270 distinct
    # matchups: nine hitters against a starter and fourteen relievers, both
    # ways. Predicted one at a time that is half a second a game, which on a
    # 1,700-game arm is 45 minutes of pure overhead for work that takes about
    # one second in a single batch.
    #
    # The lazy path below is kept for anything the grid did not anticipate — a
    # replacement-level callup, a position player mopping up — so an unseen
    # matchup is still priced rather than silently left uncorrected.

    def _prime(self, home, away) -> None:
        np = self.np
        keys, rows, bases = [], [], []
        for is_home_batting in (True, False):
            bat_side = home if is_home_batting else away
            pit_side = away if is_home_batting else home
            arms = [pit_side.starter] + list(pit_side.bullpen)
            for bat in bat_side.lineup:
                for pit in arms:
                    if pit is None:
                        continue
                    key = (bat.player_id, pit.player_id, is_home_batting,
                           pit.is_starter)
                    if key in self._memo:
                        continue
                    base = self.base_vector(bat, pit, is_home_batting)
                    keys.append((key, base))
                    rows.append(self._row(bat, pit, is_home_batting, base))
                    self._memo[key] = None          # claim it
                    bases.append(base)
        if not rows:
            return
        Xf = np.vstack(rows)
        B = np.vstack(bases)
        if self.hier:
            P = hier_proba(self.fold, Xf, B, self.hier, alpha=self.alpha,
                           save_dir=self.save_dir)
        else:
            P = _blend(predict_proba(self.bst, Xf[:, self.mask],
                                     B if self.use_init else None,
                                     centre=self.centre),
                       B, self.mode, self.alpha)
        self.n_batched = len(rows)
        for (key, base), p in zip(keys, P):
            self._memo[key] = self._mult(p, base)

    def _mult(self, P, base) -> Dict[int, float]:
        """Multipliers, not a vector: `apply_multipliers` renormalises, so a
        common factor cancels and only the SHAPE of the correction survives —
        which is what lets tonight's form draw through instead of being
        overwritten by a model that never saw it."""
        return {i: float(P[i] / max(base[i], 1e-9))
                for i in range(m.N_OUTCOMES)}

    def _row(self, bat, pit, is_home_batting: bool, base):
        np = self.np
        bid, pid = bat.player_id, pit.player_id
        bfeat = self._bf.get(bid)
        if bfeat is None:
            bfeat = _player_features(self.bat_board.get(bid), BAT_COLS)
            self._bf[bid] = bfeat
        pfeat = self._pf.get(pid)
        if pfeat is None:
            pfeat = _player_features(self.pit_board.get(pid), PIT_COLS)
            self._pf[pid] = pfeat
        # The PITCHING side owns the defence, the arms and the catcher.
        pit_side = self.sides[not is_home_batting]
        return np.concatenate((
            bfeat, pfeat,
            _matchup_features(self.bat_board.get(bid), self.pit_board.get(pid),
                              bat.bats, pit.throws),
            context_features(self.gc, pit_side, is_home_batting,
                             pit.is_starter, state=None),
            np.log(np.maximum(base, 1e-9)),
            np.log(np.maximum(np.asarray(bat.rates, dtype="float32"), 1e-9)),
            np.log(np.maximum(np.asarray(pit.rates, dtype="float32"), 1e-9)),
        )).astype("float32")

    def base_vector(self, bat, pit, is_home_batting: bool):
        """The vector the model was TRAINED against: the engine's own
        composition with the game-form draw at its mean. Not the runtime
        vector — that carries tonight's draw, which the model never saw."""
        pit_side = self.sides[not is_home_batting]
        return self.np.asarray(m.pa_rates(
            bat, pit, faced=0, oaa=pit_side.oaa, framing=pit_side.framing,
            is_home=is_home_batting,
            tilt=(self.gc["wx"]
                  + self.gc["park"]["home" if is_home_batting else "away"])),
            dtype="float32")

    def __call__(self, bat, pit, is_home: bool) -> Dict[int, float]:
        key = (bat.player_id, pit.player_id, bool(is_home), pit.is_starter)
        got = self._memo.get(key)
        if got is None:
            base = self.base_vector(bat, pit, is_home)
            Xf = self._row(bat, pit, is_home, base)[None, :]
            self.n_predict += 1
            if self.hier:
                P = hier_proba(self.fold, Xf, base[None, :], self.hier,
                               alpha=self.alpha, save_dir=self.save_dir)[0]
            else:
                P = _blend(predict_proba(self.bst, Xf[:, self.mask],
                                         base[None, :] if self.use_init
                                         else None, centre=self.centre)[0],
                           base, self.mode, self.alpha)
            got = self._mult(P, base)
            self._memo[key] = got
        return got


def game_adjuster(tag: str, fold: str, season: int, as_of: str, row: dict,
                  home, away, mode: str = "ml", alpha: float = 1.0,
                  save_dir: Path = m.SAVE_DIR):
    return GameAdjuster(tag, fold, season, as_of, row, home, away,
                        mode=mode, alpha=alpha, save_dir=save_dir)


# ===========================================================================
# 5. THE HIERARCHY — one residual per RUN-RELEVANT component
# ===========================================================================
# **Why the flat nine-class model is the wrong object, measured rather than
# argued.** Model C's Brier improvement on the 2026 test season, per outcome,
# x1e4 against the incumbent:
#
#     K -2.56   GB_OUT -2.60   AIR_OUT -1.80   1B -0.79   BB -0.58
#     HR -0.29   2B -0.06   HBP -0.03   3B -0.01
#
# 80% of it is strikeouts and the two out types. HOME RUNS GET 3.3%. Runs are
# driven by home runs, walks and hits, so the flat model spends nearly all of
# its accuracy on the components that matter least to the thing being priced —
# and because the nine probabilities must sum to one, those gains push mass
# around on home runs and walks as a side effect rather than as a decision.
#
# So model the conditional structure instead, one binary residual per node:
#
#     PA
#     |-- K                                   node "K"    over all PA
#     +-- not K
#         |-- BB / HBP                        node "BB"   over not-K
#         +-- ball in play
#             |-- HR                          node "HR"   over BIP
#             +-- not HR
#                 |-- out (GB / AIR)          node "HIT"  over non-HR BIP
#                 +-- hit
#                     |-- 1B
#                     +-- XBH                 node "XBH"  over hits
#                         |-- 2B
#                         +-- 3B              node "3B"   over XBH
#
# Each node is a binary LightGBM with `init_score = logit(baseline at that
# node)`, so it is a residual in exactly the sense section 3 established for
# the multiclass version. The two splits the hierarchy does not model — HBP
# within BB+HBP, and ground versus air within outs — are taken from the
# BASELINE's own ratio, because neither carries run value the engine reads
# differently and neither is worth a model.
#
# Two things fall out for free. Ablation (`--nodes HR,BB`) becomes a set
# operation rather than a retrain, which is what section 8 of the fixes memo
# asks for. And residual-STRENGTH blending becomes exact: alpha multiplies the
# logit-space correction at each node, so it scales the correction rather than
# mixing two distributions — see `BLEND_IN_LOGIT_SPACE`.

# (name, outcomes that count as a SUCCESS, outcomes that form the DENOMINATOR;
#  None denominator means all nine).
NODE_SPEC: Tuple[Tuple[str, Tuple[int, ...], Optional[Tuple[int, ...]]], ...] = (
    ("K",   (m.K,),                    None),
    ("BB",  (m.BB, m.HBP),             (m.BB, m.HBP, m.GB_OUT, m.AIR_OUT,
                                        m.S1B, m.S2B, m.S3B, m.HR)),
    ("HR",  (m.HR,),                   (m.GB_OUT, m.AIR_OUT, m.S1B, m.S2B,
                                        m.S3B, m.HR)),
    ("HIT", (m.S1B, m.S2B, m.S3B),     (m.GB_OUT, m.AIR_OUT, m.S1B, m.S2B,
                                        m.S3B)),
    ("XBH", (m.S2B, m.S3B),            (m.S1B, m.S2B, m.S3B)),
    ("3B",  (m.S3B,),                  (m.S2B, m.S3B)),
)
NODE_NAMES: Tuple[str, ...] = tuple(n for n, _, _ in NODE_SPEC)

# The run-relevant three. `HIT` and the two extra-base nodes move BABIP, which
# the fixes memo flags as the component most likely to add noise without
# adding runs; they are separable so that can be measured rather than assumed.
NODES_RUN = ("K", "BB", "HR")

# **Columns that are CATEGORIES, declared to LightGBM rather than merely
# documented as such.** `_venue_ids` says "so `venue_id` is a real categorical
# rather than an ordering" and `_hand_code` says "handedness as a single
# categorical" — but nothing ever passed `categorical_feature`, so both went in
# as float32 and were split ORDINALLY: `venue_id <= 12.5` is a cut through the
# ALPHABETICAL order of park names, and `hand_code <= 2.5` groups {LL, LR, RL}
# against {RR, SL, SR}. Neither cut means anything.
#
# It is worse under the tuned configuration than the shipped one: `num_leaves`
# = 7 allows at most six splits in a whole tree, so isolating one park out of
# 31 is not inefficient, it is IMPOSSIBLE. The docstrings asserted an intent
# the code never implemented.
#
# This is part of the FINGERPRINT (see `node_model_path`), so models fitted
# with and without it coexist on disk and can be A/B'd — trap 2 of section 4c.
NODE_CATEGORICALS: Tuple[str, ...] = ("venue_id", "hand_code")

LGB_NODE_PARAMS: Dict[str, object] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.03,
    "num_leaves": 31,
    "min_data_in_leaf": 500,
    "feature_fraction": 0.6,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 20.0,
    "num_threads": 0,
    "verbose": -1,
    "seed": 17,
}


def _logit(p, eps: float = 1e-9):
    import numpy as np
    p = np.clip(p, eps, 1.0 - eps)
    return np.log(p / (1.0 - p))


def _expit(z):
    import numpy as np
    return 1.0 / (1.0 + np.exp(-z))


def node_probs(vec):
    """A 9-vector (or n x 9) -> the six conditional probabilities."""
    import numpy as np
    v = np.atleast_2d(np.asarray(vec, dtype="float64"))
    out = np.zeros((v.shape[0], len(NODE_SPEC)))
    for j, (_, pos, den) in enumerate(NODE_SPEC):
        d = v.sum(axis=1) if den is None else v[:, list(den)].sum(axis=1)
        n = v[:, list(pos)].sum(axis=1)
        out[:, j] = np.where(d > 0, n / np.maximum(d, 1e-12), 0.0)
    return out


def node_targets(y):
    """Per node: which rows are in its denominator, and their binary label.

    A node is only defined on its conditioning set — the HR node says nothing
    about a plate appearance that ended in a strikeout — so each model trains
    on its own subset. That is not a filtering convenience; it is what makes
    each node's residual a statement about a CONDITIONAL probability rather
    than a joint one.
    """
    import numpy as np
    y = np.asarray(y)
    out = []
    for _, pos, den in NODE_SPEC:
        mask = (np.ones(len(y), dtype=bool) if den is None
                else np.isin(y, list(den)))
        out.append((mask, np.isin(y, list(pos)).astype("int8")))
    return out


def reconstruct(base, nodes):
    """Baseline 9-vector + six conditional probabilities -> a 9-vector.

    HBP-within-walks and ground-within-outs come from the BASELINE's own
    ratios: the hierarchy does not model them, and inventing a split would be
    a claim the data was never asked to support.
    """
    import numpy as np
    b = np.atleast_2d(np.asarray(base, dtype="float64"))
    q = np.atleast_2d(np.asarray(nodes, dtype="float64"))
    pK, pBB, pHR, pHIT, pXBH, p3B = (q[:, j] for j in range(6))

    out = np.zeros_like(b)
    out[:, m.K] = pK
    notk = 1.0 - pK
    walks = notk * pBB
    # HBP's share of the walk bucket, from the baseline
    wb = b[:, m.BB] + b[:, m.HBP]
    hbp_share = np.where(wb > 0, b[:, m.HBP] / np.maximum(wb, 1e-12), 0.0)
    out[:, m.HBP] = walks * hbp_share
    out[:, m.BB] = walks * (1.0 - hbp_share)

    bip = notk * (1.0 - pBB)
    out[:, m.HR] = bip * pHR
    nonhr = bip * (1.0 - pHR)
    hits = nonhr * pHIT
    outs = nonhr * (1.0 - pHIT)
    ob = b[:, m.GB_OUT] + b[:, m.AIR_OUT]
    gb_share = np.where(ob > 0, b[:, m.GB_OUT] / np.maximum(ob, 1e-12), 0.46)
    out[:, m.GB_OUT] = outs * gb_share
    out[:, m.AIR_OUT] = outs * (1.0 - gb_share)

    xbh = hits * pXBH
    out[:, m.S1B] = hits * (1.0 - pXBH)
    out[:, m.S3B] = xbh * p3B
    out[:, m.S2B] = xbh * (1.0 - p3B)
    s = out.sum(axis=1, keepdims=True)
    return out / np.maximum(s, 1e-12)


def node_model_path(node: str, fold: str,
                    save_dir: Path = m.SAVE_DIR,
                    fp: Optional[str] = None) -> Path:
    """Where one node's booster lives.

    **The path carries the CONFIGURATION's fingerprint**, so a shipped-default
    fit and a tuned fit coexist rather than overwriting each other. Without it
    there is one file per (node, fold) and the two cannot be A/B'd at all —
    switching configurations would mean retraining in place, which destroys
    the arm you are comparing against.
    """
    if fp is None:
        r, pat = active_node_fit(save_dir)
        fp = node_fingerprint(active_node_params(save_dir), r, pat)
    return (Path(save_dir) / "mlml" / DATASET_VERSION / "models"
            / f"node_{node}_{fold}_{fp}.txt")


# --- the fit, once, so the trainer and the tuner cannot drift --------------
# `train_nodes` and `tune_nodes` have to fit a node the SAME way or the search
# optimises something the trainer does not build. Two copies would agree on
# the day they were written; this is the `pa_rates` argument (section 4) at
# one more level down, and it is why both go through `_fit_node`.

_NODE_ARRAYS: Dict[tuple, dict] = {}


def node_arrays(fold: str, save_dir: Path = m.SAVE_DIR) -> dict:
    """Everything a node fit needs on one fold, loaded and masked once.

    Memoised because the stack is ~275 MB and a search refits six nodes
    dozens of times; loading it per trial is most of the wall clock.
    """
    key = (fold, str(save_dir))
    got = _NODE_ARRAYS.get(key)
    if got is not None:
        return got
    tr_seasons, va_season, _ = FOLDS[fold]
    Xtr, ytr, btr, _ = _stack(tr_seasons, save_dir)
    Xva, yva, bva, _ = _stack([va_season], save_dir)
    # The hierarchy never sees the flat model's `base_` columns: its prior
    # enters as `init_score` at each node, which is the same argument section 3
    # made for model C and the reason C beat B.
    mask = _feature_mask(keep_base=False, keep_state=False)
    got = {"fold": fold, "train_seasons": tuple(tr_seasons),
           "valid_season": va_season,
           "names": [n for n, k in zip(FEATURE_NAMES, mask) if k],
           "Xtr": Xtr[:, mask], "Xva": Xva[:, mask],
           "ytr": ytr, "yva": yva, "btr": btr, "bva": bva,
           "qtr": node_probs(btr), "qva": node_probs(bva),
           "ttr": node_targets(ytr), "tva": node_targets(yva)}
    _NODE_ARRAYS[key] = got
    return got


def _fit_node(arr: dict, j: int, params: dict, rounds: int, patience: int):
    """One node, on one fold. Returns (booster, centre, valid_ll, base_ll).

    `centre` is the mean raw score on the node's VALIDATION denominator, and
    it is subtracted before scoring — the training seasons' LEVEL is not
    knowledge and does not generalise. Same argument as `train_model`, and
    `sim_state.md` trap 7 for the fifth time.
    """
    import numpy as np
    import lightgbm as lgb
    mtr, ltr = arr["ttr"][j]
    mva, lva = arr["tva"][j]
    p = node_params({**LGB_NODE_PARAMS, **params}, int(mtr.sum()))
    cats = [n for n in NODE_CATEGORICALS if n in arr["names"]]
    dtr = lgb.Dataset(arr["Xtr"][mtr], label=ltr[mtr],
                      feature_name=arr["names"], categorical_feature=cats,
                      init_score=_logit(arr["qtr"][mtr, j]))
    dva = lgb.Dataset(arr["Xva"][mva], label=lva[mva],
                      feature_name=arr["names"], categorical_feature=cats,
                      reference=dtr,
                      init_score=_logit(arr["qva"][mva, j]))
    bst = lgb.train(p, dtr, num_boost_round=rounds, valid_sets=[dva],
                    valid_names=["valid"],
                    callbacks=[lgb.early_stopping(patience, verbose=False)])
    centre = float(bst.predict(arr["Xva"][mva], raw_score=True,
                               num_iteration=bst.best_iteration).mean())
    raw = bst.predict(arr["Xva"][mva], raw_score=True,
                      num_iteration=bst.best_iteration) - centre
    q = _expit(_logit(arr["qva"][mva, j]) + raw)
    y1 = lva[mva].astype("float64")

    def _bll(pr):
        return float(-(y1 * np.log(np.maximum(pr, 1e-12))
                       + (1 - y1) * np.log(np.maximum(1 - pr, 1e-12))).mean())

    return bst, centre, _bll(q), _bll(arr["qva"][mva, j])


def _joint_ll(arr: dict, fits: Dict[int, tuple], alpha: float = 1.0) -> float:
    """The nine-outcome log loss of the RECONSTRUCTED vector on validation.

    The objective a shared configuration has to be judged on. Per-node gains
    are not commensurable — 3B's denominator is 15k rows of 630k, so summing
    them lets the rarest node choose parameters for the other five — and the
    simulator consumes the reconstructed vector, never a node.
    """
    import numpy as np
    q = arr["qva"].copy()
    for j, (bst, centre) in fits.items():
        raw = bst.predict(arr["Xva"], raw_score=True,
                          num_iteration=bst.best_iteration) - centre
        q[:, j] = _expit(_logit(q[:, j]) + alpha * raw)
    P = reconstruct(arr["bva"], q)
    y = arr["yva"]
    return float(-np.log(np.maximum(P[np.arange(len(y)), y], 1e-12)).mean())


def baseline_joint_ll(arr: dict) -> float:
    """What the incumbent scores on the same rows — the mark to beat."""
    import numpy as np
    y = arr["yva"]
    b = arr["bva"]
    return float(-np.log(np.maximum(b[np.arange(len(y)), y], 1e-12)).mean())


def train_nodes(fold: str = "f26", nodes: Sequence[str] = NODE_NAMES,
                save_dir: Path = m.SAVE_DIR, rounds: Optional[int] = None,
                patience: Optional[int] = None, refresh: bool = False,
                params: Optional[dict] = None,
                verbose: bool = True) -> Dict[str, dict]:
    """One binary residual per node, early-stopped on the validation season.

    `params` overrides `LGB_NODE_PARAMS`; passing None uses whatever
    `active_node_params()` resolves to, which is the tuned configuration when
    one has been searched and the shipped default when it has not.
    """
    p = dict(active_node_params(save_dir) if params is None else params)
    dr, dp = active_node_fit(save_dir)
    rounds = dr if rounds is None else rounds
    patience = dp if patience is None else patience
    # The budget is fingerprinted WITH the parameters — a model fitted to a
    # different number of rounds is a different model, and this is the only
    # thing that makes that visible.
    fp = node_fingerprint(p, rounds, patience)
    arr = node_arrays(fold, save_dir)

    got: Dict[str, dict] = {}
    for j, node in enumerate(NODE_NAMES):
        if node not in nodes:
            continue
        dest = node_model_path(node, fold, save_dir, fp)
        meta_path = dest.with_suffix(".json")
        if dest.exists() and meta_path.exists() and not refresh:
            with open(meta_path) as fh:
                cached = json.load(fh)
            # **A cached model fitted under DIFFERENT parameters is not this
            # model.** Without the fingerprint the search would appear to
            # take effect and the shipped default would keep being served —
            # a silent no-op of exactly the kind `sim_state.md` trap 11
            # records three of in one session.
            if cached.get("params_fp") == fp:
                got[node] = cached
                if verbose:
                    print(f"[nodes] {node}/{fold} cached "
                          f"(gain {cached['valid_gain']:+.6f})")
                continue
            if verbose:
                print(f"[nodes] {node}/{fold} cached under a DIFFERENT "
                      f"configuration ({cached.get('params_fp')} != {fp}) "
                      f"— refitting", flush=True)
        t0 = time.time()
        bst, centre, ll, llb = _fit_node(arr, j, p, rounds, patience)
        dest.parent.mkdir(parents=True, exist_ok=True)
        bst.save_model(str(dest), num_iteration=bst.best_iteration)
        n_train = int(arr["ttr"][j][0].sum())
        rec = {"node": node, "fold": fold, "best_iter": int(bst.best_iteration),
               "centre": centre, "n_train": n_train,
               "n_valid": int(arr["tva"][j][0].sum()), "valid_logloss": ll,
               "valid_base_logloss": llb, "valid_gain": llb - ll,
               "params": {k: v for k, v in p.items()},
               "rounds": rounds, "patience": patience,
               "params_fp": fp,
               "min_data_in_leaf": node_params(p, n_train).get(
                   "min_data_in_leaf"),
               "features": arr["names"], "seconds": round(time.time() - t0, 1)}
        with open(meta_path, "w") as fh:
            json.dump(rec, fh, default=str)
        got[node] = rec
        if verbose:
            print(f"[nodes] {node:4s}/{fold}: {rec['best_iter']:4d} rounds, "
                  f"n {rec['n_train']:,}  valid {ll:.6f} vs base {llb:.6f} "
                  f"(gain {llb - ll:+.6f}) [{rec['seconds']:.0f}s]", flush=True)
    return got


_NODE_BOOSTERS: Dict[tuple, object] = {}


def load_nodes(fold: str, nodes: Sequence[str] = NODE_NAMES,
               save_dir: Path = m.SAVE_DIR):
    import lightgbm as lgb
    key = (fold, tuple(sorted(nodes)), str(save_dir))
    got = _NODE_BOOSTERS.get(key)
    if got is None:
        out = {}
        _r, _pat = active_node_fit(save_dir)
        want = node_fingerprint(active_node_params(save_dir), _r, _pat)
        for node in nodes:
            path = node_model_path(node, fold, save_dir, want)
            if not path.exists():
                raise FileNotFoundError(
                    f"mlb_ml: no node model {node}/{fold} under configuration "
                    f"{want!r} at {path}. Run `python mlb_ml.py train-nodes "
                    f"--fold {fold}` with ML_NODE_PARAMS="
                    f"{m.ML_NODE_PARAMS!r}.")
            with open(path.with_suffix(".json")) as fh:
                meta = json.load(fh)
            # **Serving a model fitted under a configuration that is no longer
            # active is the silent-no-op failure, not a missing file.** It
            # would answer with nine plausible probabilities and the search
            # would read as having had no effect. `sim_state.md` trap 11:
            # prefer raising over returning something for an impossible state.
            got_fp = meta.get("params_fp")
            if got_fp != want:
                raise RuntimeError(
                    f"mlb_ml: node model {node}/{fold} was fitted under "
                    f"configuration {got_fp!r} but the active one is "
                    f"{want!r}. Re-fit with `python mlb_ml.py train-nodes "
                    f"--fold {fold}` (it detects this itself), or set "
                    f"ML_NODE_PARAMS to 'shipped' to serve the hand-chosen "
                    f"defaults.")
            out[node] = (lgb.Booster(model_file=str(path)), meta)
        got = out
        _NODE_BOOSTERS[key] = got
    return got


def hier_proba(fold: str, X, base, nodes: Sequence[str] = NODE_NAMES,
               alpha: float = 1.0, save_dir: Path = m.SAVE_DIR):
    """The hierarchy's 9-vector.

    **`alpha` scales the RESIDUAL, in logit space, not the probabilities.**
    Mixing two distributions (`a*P_ml + (1-a)*P_base`) pulls the result toward
    the baseline and damps its spread as a side effect; scaling the logit-space
    correction changes the correction's STRENGTH while preserving its shape.
    At alpha = 0 the two agree exactly; away from it they do not, and the
    fixes memo is right that the second is the meaningful knob.
    """
    import numpy as np
    got = load_nodes(fold, nodes, save_dir)
    mask = _feature_mask(keep_base=False, keep_state=False)
    Xm = X[:, mask]
    q = node_probs(base)
    for j, name in enumerate(NODE_NAMES):
        if name not in got:
            continue                      # ablated: keep the baseline's node
        bst, meta = got[name]
        raw = bst.predict(Xm, raw_score=True) - meta["centre"]
        q[:, j] = _expit(_logit(q[:, j]) + alpha * raw)
    return reconstruct(base, q)


def score_hier(fold: str = "f26",
               node_sets: Sequence[Tuple[str, Sequence[str]]] = (),
               alphas: Sequence[float] = (0.25, 0.5, 1.0),
               save_dir: Path = m.SAVE_DIR) -> dict:
    """Section 8's ablation, at the PA level: which nodes are worth anything.

    Reported alongside the RUN VALUE error, because that is the quantity the
    simulator actually consumes and log loss is not — a node can improve its
    own conditional probability and move the implied run environment the wrong
    way, which is the whole reason the flat model was the wrong object.
    """
    import numpy as np
    _, _, test = FOLDS[fold]
    d = load_dataset(test, save_dir)
    X, y, base = d["X"], d["y"], d["base"]
    idx = np.arange(len(y))
    oh = np.zeros_like(base); oh[idx, y] = 1.0
    rv_obs = np.array([m.rate_run_value(r) for r in oh]).mean()

    def rv(P):
        return (np.array([m.rate_run_value(r) for r in P]).mean()
                - rv_obs) * 37.8

    ll_b = float(-np.log(np.maximum(base[idx, y], 1e-12)).mean())
    print(f"\n=== HIERARCHY, fold {fold}, test {test}, {len(y):,} PA ===")
    print(f"  {'nodes':22s} {'alpha':>6s} {'log loss':>10s} {'gain':>9s} "
          f"{'HR sd/base':>11s} {'run-level err':>14s}")
    print(f"  {'baseline':22s} {'-':>6s} {ll_b:10.5f} {0.0:+9.5f} "
          f"{1.000:11.3f} {rv(base):+13.3f}")
    sets = list(node_sets) or [("all", NODE_NAMES), ("K,BB,HR", NODES_RUN),
                               ("HR", ("HR",)), ("BB", ("BB",)),
                               ("K", ("K",)), ("HIT", ("HIT",)),
                               ("XBH,3B", ("XBH", "3B"))]
    out = {}
    for label, ns in sets:
        for a in alphas:
            P = hier_proba(fold, X, base, ns, alpha=a, save_dir=save_dir)
            ll = float(-np.log(np.maximum(P[idx, y], 1e-12)).mean())
            sd = float(P[:, m.HR].std() / base[:, m.HR].std())
            print(f"  {label:22s} {a:6.2f} {ll:10.5f} {ll_b - ll:+9.5f} "
                  f"{sd:11.3f} {rv(P):+13.3f}")
            out[(label, a)] = {"logloss": ll, "gain": ll_b - ll,
                               "hr_sd_ratio": sd, "run_err": rv(P)}
    return out


# ===========================================================================
# 5b. TUNING THE NODES — a search that cannot see either test season
# ===========================================================================
# `LGB_NODE_PARAMS` was chosen, never searched, and one parameter set is
# applied to six nodes whose training sets span 8,028 rows (3B on f25) to
# 325,841 (K on f26) — a 40x range. `min_data_in_leaf = 500` is 6.2% of the
# 3B node's ENTIRE training set, which caps it near 16 leaves however high
# `num_leaves` is set, and XBH/f26 early-stopped at 16 rounds. A probe over
# eight hand-picked configurations moved 3B/f26 from +0.001712 to +0.002144
# and XBH/f26 from +0.000097 to +0.000245, both toward SMALLER trees and a
# LOWER learning rate — which is what a residual on a strong prior should
# want: many shallow corrections rather than a few deep ones.
#
# **The search selects on fold f25's VALIDATION season (2024) and nothing
# else, and the chosen configuration is then applied to BOTH folds.** 2024 is
# strictly before 2025 and 2026, so it leaks into neither test season. The
# obvious alternative — tune each fold on its own validation — is clean for
# f26 but NOT for f25: f26 validates on 2025, which is f25's TEST season, so
# a configuration chosen by pooling the two folds would have been picked
# partly by reading the answer. Selecting once, on the earliest season either
# fold is allowed to see, is the only arrangement that keeps BOTH test
# numbers honest. This is `sim_state.md` section 5 item 7 — the standing
# complaint that `ML_BLEND_ALPHA` and the node set were chosen on the test
# seasons — deliberately not repeated.
#
# Two things this does NOT claim. Validation is doing double duty (early
# stopping already used it), so the validation gain reported here is
# optimistic; only the test number is not. And a search over ~50 trials on a
# node whose validation set is 7,458 rows can fit the validation set, which
# is the reason the configuration is SHARED across all six nodes and both
# folds rather than fitted per node — six parameters chosen against ~500k
# pooled validation rows, not thirty-six against 7,458.
#
# The objective is the JOINT nine-outcome log loss of the reconstructed
# vector, not a sum of per-node gains. Node gains are not commensurable: 3B is
# conditioned on XBH conditioned on hits, so its denominator is 15k rows of
# 630k, and summing raw gains would let the rarest node choose the parameters
# the other five have to live with. The joint log loss is what `score_hier`
# reports and the reconstructed vector is what the simulator consumes.
#
# Scored at `alpha = 1.0` on purpose. `ML_BLEND_ALPHA` scales the residual at
# deployment and is a SEPARATE decision; tuning capacity against an
# already-shrunken residual would confound the two, and a model that needs
# alpha to hide its overfitting is one that should have been fitted
# differently.

NODE_TUNE_FOLD = "f25"

# `min_data_in_leaf` is searched in two parameterisations because the node
# sizes span 40x and it is not obvious which generalises: an absolute floor
# treats a 15k-row node and a 326k-row node alike, while a fraction scales
# with the evidence available. Either way it is ONE degree of freedom — a
# trial picks a spec, not six numbers.
NODE_MIN_DATA_SPECS: Tuple[Tuple[str, float], ...] = (
    ("abs", 20.0), ("abs", 50.0), ("abs", 100.0), ("abs", 200.0),
    ("abs", 400.0), ("abs", 800.0),
    ("frac", 0.0005), ("frac", 0.001), ("frac", 0.002),
    ("frac", 0.005), ("frac", 0.01),
)

NODE_TUNE_SPACE: Dict[str, Tuple] = {
    "learning_rate": (0.005, 0.01, 0.02, 0.03, 0.05),
    "num_leaves": (3, 7, 15, 31, 63),
    "min_data_spec": NODE_MIN_DATA_SPECS,
    "feature_fraction": (0.2, 0.3, 0.45, 0.6, 0.8),
    "bagging_fraction": (0.6, 0.8, 1.0),
    "lambda_l2": (1.0, 5.0, 20.0, 80.0, 320.0),
}


def _min_data_for(spec, n_train: int) -> int:
    """Resolve one `min_data_spec` against one node's training size."""
    kind, val = spec
    if str(kind) == "abs":
        return int(val)
    return int(max(20, min(2000, round(float(val) * n_train))))


def node_params(params: dict, n_train: int) -> dict:
    """A configuration, resolved for one node.

    `min_data_spec` is not a LightGBM parameter — it is the two-way choice
    above, and it has to become `min_data_in_leaf` against the node's own row
    count before it reaches `lgb.train`. Everything else passes through, so a
    plain LightGBM dict survives this unchanged.
    """
    out = {k: v for k, v in params.items()
           if k != "min_data_spec" and not k.startswith("_")}
    spec = params.get("min_data_spec")
    if spec is not None:
        out["min_data_in_leaf"] = _min_data_for(spec, n_train)
    return out


def node_fingerprint(params: dict, rounds: int, patience: int) -> str:
    """THE recipe for a node configuration's digest, in one place.

    The fit BUDGET is part of the configuration (4c trap 1) and so is the
    CATEGORICAL declaration (`NODE_CATEGORICALS`) — a model fitted with
    `venue_id` as a category is a different model from one fitted with it as a
    number, and if the two share a path one silently overwrites the other.

    It is a FUNCTION rather than an inlined dict because assembling the recipe
    by hand at each call site is precisely how "two strings for one
    configuration" happened before, and adding `_cats` had already spread it to
    four sites. `test_the_banner_names_the_fingerprint_the_models_are_filed_under`
    pins that there is only one.
    """
    return params_fingerprint({**params, "_rounds": int(rounds),
                               "_patience": int(patience),
                               "_cats": list(NODE_CATEGORICALS)})


def params_fingerprint(params: dict) -> str:
    """A stable digest of the configuration a node model was fitted with.

    Written into every node's meta so a cached model fitted under a DIFFERENT
    configuration is detected rather than silently reused. `sim_state.md`
    trap 9 is this failure one level up: a cache that parses fine and answers
    with the wrong thing is worse than a missing one, and trap 11 records
    three silently-disabled variants in a single session.
    """
    import hashlib
    payload = json.dumps({k: params[k] for k in sorted(params)},
                         sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def node_tune_path(fold: str = NODE_TUNE_FOLD,
                   save_dir: Path = m.SAVE_DIR) -> Path:
    return (Path(save_dir) / "mlml" / DATASET_VERSION / "models"
            / f"node_tune_{fold}.json")


# **The fit BUDGET is part of the configuration, not a call-site default.**
# The search ran at rounds 12000 / patience 400 and picked a learning rate of
# 0.005, which needs 660-3255 rounds to converge; `train_nodes` then defaulted
# to 3000/120 and fitted K/f25 in 1726 rounds instead of the 2890 the search
# selected. The deployed model was a DIFFERENT model from the searched one,
# and the fingerprint could not see it because it covered only the LightGBM
# parameters. Both now travel with the configuration and both are
# fingerprinted, so the mismatch is a loud refusal rather than a quiet
# few-percent haircut.
NODE_FIT_ROUNDS = 3000
NODE_FIT_PATIENCE = 120


def active_node_fit(save_dir: Path = m.SAVE_DIR) -> Tuple[int, int]:
    """(rounds, patience) for the active configuration."""
    want = getattr(m, "ML_NODE_PARAMS", "shipped")
    if want != "tuned":
        return NODE_FIT_ROUNDS, NODE_FIT_PATIENCE
    path = node_tune_path(NODE_TUNE_FOLD, save_dir)
    with open(path) as fh:
        got = json.load(fh)
    return int(got["rounds"]), int(got["patience"])


def active_node_params(save_dir: Path = m.SAVE_DIR) -> dict:
    """The configuration the node models are fitted and served under.

    The searched result once `tune-nodes` has written one, the shipped default
    until then. It is read from disk rather than pasted into the source so the
    search and the models it produced cannot disagree, and `params_fp` in
    every node's meta records which one was actually used.
    """
    want = getattr(m, "ML_NODE_PARAMS", "shipped")
    if want == "shipped":
        return dict(LGB_NODE_PARAMS)
    if want != "tuned":
        raise ValueError(
            f"mlb_sim.ML_NODE_PARAMS must be 'shipped' or 'tuned', "
            f"not {want!r}.")
    path = node_tune_path(NODE_TUNE_FOLD, save_dir)
    if not path.exists():
        # **Not a fallback.** An arm that asked for the tuned configuration
        # and quietly got the shipped one is the silent no-op this file has
        # recorded three of in a single session (trap 11).
        raise FileNotFoundError(
            f"mlb_sim.ML_NODE_PARAMS is 'tuned' but no search result exists "
            f"at {path}. Run `python mlb_ml.py tune-nodes`.")
    with open(path) as fh:
        return {**LGB_NODE_PARAMS, **json.load(fh)["best_params"]}


def _sample_params(space: Dict[str, Tuple], rng) -> dict:
    return {k: v[rng.randrange(len(v))] for k, v in space.items()}


def _tune_trial(arr: dict, params: dict, rounds: int, patience: int,
                alpha: float = 1.0) -> dict:
    """Fit all six nodes under one configuration and score the joint vector."""
    fits, per_node = {}, {}
    for j, node in enumerate(NODE_NAMES):
        bst, centre, ll, llb = _fit_node(arr, j, params, rounds, patience)
        fits[j] = (bst, centre)
        per_node[node] = {"gain": llb - ll, "iters": int(bst.best_iteration),
                          "min_data": node_params(
                              {**LGB_NODE_PARAMS, **params},
                              int(arr["ttr"][j][0].sum()))["min_data_in_leaf"]}
    return {"joint_ll": _joint_ll(arr, fits, alpha), "nodes": per_node}


def boundary_flags(params: dict) -> Dict[str, str]:
    """Which chosen values sit at an END of their grid.

    **A search whose winner is on the boundary has not found an optimum, it
    has run out of room.** The first f25 search picked `num_leaves = 3` and
    `lambda_l2 = 320` — the bottom and the top of their ranges — which says
    the residual wants an even weaker, even more heavily penalised learner
    than the grid could express, and that the reported gain is a LOWER bound
    on what this configuration family is worth. `num_leaves = 3` is nearly
    LightGBM's floor (2) so that edge is close to real; `lambda_l2 = 320`
    is an arbitrary stopping point and is not.

    Reported rather than auto-extended, because widening the grid invalidates
    the seeded draws and has to start a fresh table — that is a decision, not
    a retry.
    """
    out: Dict[str, str] = {}
    for key, values in NODE_TUNE_SPACE.items():
        got = params.get(key)
        if got is None or len(values) < 2:
            continue
        # **Only an ORDERED NUMERIC range has ends.** `min_data_spec` is a
        # categorical of ("abs", n) and ("frac", f) pairs, and those tuples
        # sort perfectly well against each other — lexically, on the kind
        # string — so a `try: sorted(...) except TypeError` guard does NOT
        # skip it. It silently ranked "abs" below "frac" and reported the
        # smallest absolute floor as a boundary, which means nothing: the two
        # parameterisations are alternatives, not a scale.
        if not all(isinstance(v, (int, float))
                   and not isinstance(v, bool) for v in values):
            continue
        order = sorted(values)
        if got == order[0]:
            out[key] = "low"
        elif got == order[-1]:
            out[key] = "high"
    return out


def node_tune_partial_path(fold: str = NODE_TUNE_FOLD,
                           save_dir: Path = m.SAVE_DIR) -> Path:
    return node_tune_path(fold, save_dir).with_suffix(".partial.json")


def _machine() -> dict:
    """What ran the search.

    Recorded because the trial table is NOT machine-portable: LightGBM with
    `num_threads = 0` takes every core, and thread count changes the order
    floating-point sums accumulate in, so the same seed on 8 and on 24 cores
    gives slightly different trees. A table with rows from two machines is a
    table whose rows are not comparable, which is precisely what a search
    compares. Resume refuses to mix them for that reason.
    """
    import platform
    try:
        import lightgbm as lgb
        ver = lgb.__version__
    except Exception:                                   # pragma: no cover
        ver = "?"
    return {"host": platform.node(), "cores": os.cpu_count(),
            "python": platform.python_version(), "lightgbm": ver}


def tune_nodes(fold: str = NODE_TUNE_FOLD, trials: int = 48, seed: int = 17,
               rounds: int = 12000, patience: int = 400,
               refine: bool = True, alpha: float = 1.0,
               save_dir: Path = m.SAVE_DIR, refresh: bool = False,
               resume: bool = True, verbose: bool = True) -> dict:
    """Random search then coordinate refinement, on ONE validation season.

    Writes `node_tune_<fold>.json` (the configuration `active_node_params`
    then serves) and appends every trial to `tune_nodes_<fold>.log` as it
    happens, so a run this long can be followed with `tail -f` instead of
    waited on blind.

    **Checkpointed per TRIAL, not per run.** A ~4 hour search that only
    persists its answer at the end has to be restarted from zero after an SSH
    drop, a reboot, or a move to another machine — and the temptation is then
    to stitch a resumed table together out of two runs. `.partial.json` is
    rewritten after every trial and `resume` picks it up; the draws are
    reproduced by replaying the same seeded sampler, so a resumed table is
    identical to an uninterrupted one.
    """
    import random
    dest = node_tune_path(fold, save_dir)
    if dest.exists() and not refresh:
        with open(dest) as fh:
            got = json.load(fh)
        if verbose:
            print(f"[tune] {fold} cached: joint {got['best_ll']:.6f} "
                  f"vs shipped {got['shipped_ll']:.6f}")
        return got

    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = node_tune_partial_path(fold, save_dir)
    log_path = dest.parent.parent / f"tune_nodes_{fold}.log"
    log = open(log_path, "a", buffering=1)
    here = _machine()

    def say(msg: str) -> None:
        log.write(msg + "\n")
        if verbose:
            print(msg, flush=True)

    # --- pick up an interrupted run ---------------------------------------
    table: List[dict] = []
    part = None
    space_fp = params_fingerprint(
        {k: list(v) for k, v in NODE_TUNE_SPACE.items()})
    if part_path.exists() and resume and not refresh:
        with open(part_path) as fh:
            part = json.load(fh)
        was = part.get("machine", {})
        if was != here:
            raise RuntimeError(
                f"mlb_ml: {part_path} was written by {was} and this is "
                f"{here}. LightGBM sums in thread order and changes its own "
                f"arithmetic between versions, so those trials are not "
                f"comparable with the ones this machine would produce — and a "
                f"table whose rows are not comparable cannot be searched. "
                f"Delete it to start clean, or finish the run where it "
                f"started.")
        if (part.get("seed"), part.get("alpha"), part.get("rounds"),
                part.get("patience")) != (seed, alpha, rounds, patience):
            raise RuntimeError(
                f"mlb_ml: {part_path} was written under different search "
                f"settings ({part.get('seed')}/{part.get('alpha')}/"
                f"{part.get('rounds')}/{part.get('patience')} vs "
                f"{seed}/{alpha}/{rounds}/{patience}). Delete it to start "
                f"clean.")
        # **The GRID is part of the search, not scenery.** `_sample_params`
        # draws by index into each tuple, so inserting one value reassigns
        # every later draw from the same seed — a resumed run would replay
        # different configurations under the same trial numbers and the table
        # would silently stop being one search. Widening the space (which the
        # boundary check below asks for) MUST start a fresh table.
        if part.get("space_fp") != space_fp:
            raise RuntimeError(
                f"mlb_ml: {part_path} was written against a different search "
                f"space ({part.get('space_fp')} != {space_fp}). The seeded "
                f"draws index into the grid, so a changed grid replays "
                f"different configurations under the same trial numbers. "
                f"Delete it to start clean.")
        table = part["table"]

    arr = node_arrays(fold, save_dir)
    ll_base = baseline_joint_ll(arr)
    t0 = time.time()

    def checkpoint() -> None:
        tmp = part_path.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump({"fold": fold, "seed": seed, "alpha": alpha,
                       "rounds": rounds, "patience": patience,
                       "trials": trials, "machine": here,
                       "space_fp": space_fp,
                       "baseline_ll": ll_base, "table": table},
                      fh, default=str)
        tmp.replace(part_path)          # atomic: a killed write cannot
                                        # leave a half-parsed checkpoint

    def done(params: dict):
        """The row for this configuration if it has already been run."""
        want = json.dumps(params, sort_keys=True, default=str)
        for row in table:
            if json.dumps(row["params"], sort_keys=True,
                          default=str) == want:
                return row
        return None

    def run(params: dict, source: str, label: str) -> dict:
        got = done(params)
        if got is not None:
            say(f"  [resume] {label} joint {got['joint_ll']:.6f}  "
                f"gain {ll_base - got['joint_ll']:+.6f}")
            return got
        t1 = time.time()
        res = _tune_trial(arr, params, rounds, patience, alpha)
        row = {"trial": len(table), "params": params,
               "joint_ll": res["joint_ll"], "nodes": res["nodes"],
               "source": source}
        table.append(row)
        checkpoint()
        say(f"  {label} joint {res['joint_ll']:.6f}  "
            f"gain {ll_base - res['joint_ll']:+.6f} [{time.time() - t1:.0f}s]")
        return row

    say(f"\n=== tune-nodes {fold}: train {arr['train_seasons']} -> select on "
        f"{arr['valid_season']} ({len(arr['yva']):,} PA), {trials} trials, "
        f"alpha {alpha} ===")
    say(f"  machine {here['host']} / {here['cores']} cores / "
        f"lightgbm {here['lightgbm']}"
        + (f"  [RESUMED from {len(table)} trials]" if table else ""))
    say(f"  incumbent (no residual) joint log loss {ll_base:.6f}")

    shipped = run({}, "shipped", "shipped LGB_NODE_PARAMS  ")

    def best_row() -> dict:
        return min(table, key=lambda r: r["joint_ll"])

    rng = random.Random(seed)
    for t in range(1, trials + 1):
        params = _sample_params(NODE_TUNE_SPACE, rng)
        prev = best_row()["joint_ll"]
        row = run(params, "random",
                  f"[{t:3d}/{trials}] "
                  f"lr {params['learning_rate']:<5} "
                  f"lv {params['num_leaves']:<3} "
                  f"md {params['min_data_spec'][0]}:"
                  f"{params['min_data_spec'][1]:<7} "
                  f"ff {params['feature_fraction']:<4} "
                  f"bf {params['bagging_fraction']:<4} "
                  f"l2 {params['lambda_l2']:<5}")
        if row["joint_ll"] < prev:
            say(f"           ^ best so far")

    if refine:
        # Coordinate descent over the same grid. One parameter at a time, the
        # rest held at the incumbent best — cheap, and it reaches
        # configurations a 48-draw random search over a 5x5x11x5x3x5 grid
        # (20,625 points) will not have sampled.
        say("  --- coordinate refinement ---")
        for key, values in NODE_TUNE_SPACE.items():
            for val in values:
                base_params = dict(best_row()["params"])
                if base_params.get(key) == val:
                    continue
                run({**base_params, key: val}, f"refine:{key}",
                    f"[refine {key:16s} = {str(val):12s}]")

    win = best_row()
    out = {"fold": fold, "select_season": arr["valid_season"],
           "train_seasons": list(arr["train_seasons"]),
           "n_select_pa": int(len(arr["yva"])), "trials": trials, "seed": seed,
           "alpha": alpha, "rounds": rounds, "patience": patience,
           "machine": here, "resumed_from": (part or {}).get("machine"),
           "space_fp": space_fp,
           "space": {k: list(v) for k, v in NODE_TUNE_SPACE.items()},
           "baseline_ll": ll_base, "shipped_ll": shipped["joint_ll"],
           "shipped_nodes": shipped["nodes"],
           "best_ll": win["joint_ll"], "best_nodes": win["nodes"],
           "best_params": win["params"],
           "best_fp": params_fingerprint({**LGB_NODE_PARAMS,
                                          **win["params"]}),
           "seconds": round(time.time() - t0, 1), "table": table}
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    say(f"  BEST joint {out['best_ll']:.6f} vs shipped {out['shipped_ll']:.6f} "
        f"({out['shipped_ll'] - out['best_ll']:+.6f}) "
        f"-> {json.dumps(win['params'], default=str)}")
    edge = boundary_flags(win["params"])
    if edge:
        say(f"  BOUNDARY: {', '.join(f'{k} at the {v} end' for k, v in sorted(edge.items()))}"
            f" — the grid ran out of room, so this gain is a LOWER bound. "
            f"Widening it starts a fresh table (see `boundary_flags`).")
    say(f"  wrote {dest}  [{out['seconds']:.0f}s this run]")
    log.close()
    return out


def tune_report(fold: str = NODE_TUNE_FOLD,
                save_dir: Path = m.SAVE_DIR) -> dict:
    """What the search chose, and what it bought per node."""
    path = node_tune_path(fold, save_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"mlb_ml: no search on {fold} at {path}. Run "
            f"`python mlb_ml.py tune-nodes --fold {fold}`.")
    with open(path) as fh:
        got = json.load(fh)
    print(f"\n=== node search, fold {fold}: selected on "
          f"{got['select_season']} ({got['n_select_pa']:,} PA) ===")
    print(f"  incumbent  {got['baseline_ll']:.6f}")
    print(f"  shipped    {got['shipped_ll']:.6f}  "
          f"gain {got['baseline_ll'] - got['shipped_ll']:+.6f}")
    print(f"  tuned      {got['best_ll']:.6f}  "
          f"gain {got['baseline_ll'] - got['best_ll']:+.6f}  "
          f"({got['shipped_ll'] - got['best_ll']:+.6f} over shipped)")
    print(f"  params     {json.dumps(got['best_params'], default=str)}")
    edge = boundary_flags(got["best_params"])
    if edge:
        print(f"  BOUNDARY   {', '.join(f'{k} at the {v} end' for k, v in sorted(edge.items()))}"
              f"  <- the grid ran out of room; the gain is a lower bound")
    print(f"\n  {'node':5s} {'shipped gain':>13s} {'tuned gain':>12s} "
          f"{'iters':>7s} {'min_data':>9s}")
    for node in NODE_NAMES:
        s = got["shipped_nodes"][node]
        b = got["best_nodes"][node]
        print(f"  {node:5s} {s['gain']:+13.6f} {b['gain']:+12.6f} "
              f"{b['iters']:7d} {b['min_data']:9d}")
    return got


# ===========================================================================
# CLI
# ===========================================================================

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""

    if cmd == "pa":
        ap = argparse.ArgumentParser(prog="mlb_ml.py pa")
        ap.add_argument("seasons", nargs="*", type=int, default=[2026])
        ap.add_argument("--workers", type=int, default=10)
        ap.add_argument("--refresh", action="store_true")
        ap.add_argument("--limit", type=int, default=None,
                        help="first N games only — a smoke run, NOT a cache "
                             "worth keeping")
        a = ap.parse_args(argv[1:])
        for s in (a.seasons or [2026]):
            collect_pa(s, workers=a.workers, refresh=a.refresh, limit=a.limit)
        return 0

    if cmd == "train":
        ap = argparse.ArgumentParser(prog="mlb_ml.py train")
        ap.add_argument("tags", nargs="*", default=["B", "C", "Cb", "Cs"])
        ap.add_argument("--fold", default="f26",
                        help=f"one of {sorted(FOLDS)}, or 'all'")
        ap.add_argument("--refresh", action="store_true")
        ap.add_argument("--rounds", type=int, default=4000)
        a = ap.parse_args(argv[1:])
        folds = sorted(FOLDS) if a.fold == "all" else [a.fold]
        for f in folds:
            for tag in a.tags:
                train_model(tag, f, refresh=a.refresh, rounds=a.rounds)
        return 0

    if cmd == "train-nodes":
        ap = argparse.ArgumentParser(prog="mlb_ml.py train-nodes")
        ap.add_argument("--fold", default="f26",
                        help=f"one of {sorted(FOLDS)}, or 'all'")
        ap.add_argument("--nodes", default=",".join(NODE_NAMES))
        ap.add_argument("--config", default=None,
                        choices=["shipped", "tuned"],
                        help="which node configuration to fit under; the "
                             "default is whatever mlb_sim.ML_NODE_PARAMS "
                             "already says")
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        if a.config:
            m.ML_NODE_PARAMS = a.config
        folds = sorted(FOLDS) if a.fold == "all" else [a.fold]
        ns = [x for x in a.nodes.split(",") if x]
        bad = [x for x in ns if x not in NODE_NAMES]
        if bad:
            raise SystemExit(f"mlb_ml: unknown node(s) {bad}; "
                             f"have {list(NODE_NAMES)}")
        p = active_node_params()
        _r, _pat = active_node_fit()
        # the fingerprint the models are FILED under, budget included — the
        # params-only digest is not the same string and printing it invites
        # exactly the mismatch the budget fix just closed
        _fp = node_fingerprint(p, _r, _pat)
        _diff = {k: v for k, v in sorted(p.items())
                 if k not in LGB_NODE_PARAMS or LGB_NODE_PARAMS[k] != v}
        print(f"[nodes] configuration {_fp} "
              f"({'TUNED' if _diff else 'shipped default'}, "
              f"rounds {_r} patience {_pat}) "
              f"{json.dumps(_diff, default=str)}", flush=True)
        for f in folds:
            train_nodes(f, ns, refresh=a.refresh)
        return 0

    if cmd == "tune-nodes":
        ap = argparse.ArgumentParser(prog="mlb_ml.py tune-nodes")
        ap.add_argument("--fold", default=NODE_TUNE_FOLD,
                        help="the fold whose VALIDATION season selects the "
                             "configuration. The default is the only one that "
                             "leaks into neither test season — see section 5b.")
        ap.add_argument("--trials", type=int, default=48)
        ap.add_argument("--seed", type=int, default=17)
        ap.add_argument("--rounds", type=int, default=12000)
        ap.add_argument("--patience", type=int, default=400)
        ap.add_argument("--alpha", type=float, default=1.0)
        ap.add_argument("--no-refine", action="store_true")
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        if a.fold != NODE_TUNE_FOLD:
            print(f"[tune] WARNING: selecting on fold {a.fold}, whose "
                  f"validation season is {FOLDS[a.fold][1]}. Only "
                  f"{NODE_TUNE_FOLD} (validation {FOLDS[NODE_TUNE_FOLD][1]}) "
                  f"is before BOTH test seasons; anything else contaminates "
                  f"a test number. See section 5b.", flush=True)
        tune_nodes(a.fold, trials=a.trials, seed=a.seed, rounds=a.rounds,
                   patience=a.patience, alpha=a.alpha,
                   refine=not a.no_refine, refresh=a.refresh)
        return 0

    if cmd == "tune-report":
        ap = argparse.ArgumentParser(prog="mlb_ml.py tune-report")
        ap.add_argument("--fold", default=NODE_TUNE_FOLD)
        a = ap.parse_args(argv[1:])
        tune_report(a.fold)
        return 0

    if cmd == "score-hier":
        ap = argparse.ArgumentParser(prog="mlb_ml.py score-hier")
        ap.add_argument("--fold", default="f26")
        ap.add_argument("--alpha", default="0.25,0.5,1.0")
        ap.add_argument("--config", default=None,
                        choices=["shipped", "tuned"])
        a = ap.parse_args(argv[1:])
        if a.config:
            m.ML_NODE_PARAMS = a.config
        _r, _pat = active_node_fit()
        _fp = node_fingerprint(active_node_params(), _r, _pat)
        print(f"[hier] node configuration: {m.ML_NODE_PARAMS} "
              f"({_fp}, rounds {_r} patience {_pat})")
        folds = sorted(FOLDS) if a.fold == "all" else [a.fold]
        al = [float(x) for x in a.alpha.split(",") if x]
        for f in folds:
            score_hier(f, alphas=al)
        return 0

    if cmd == "score":
        ap = argparse.ArgumentParser(prog="mlb_ml.py score")
        ap.add_argument("tags", nargs="*", default=["B", "C", "Cb", "Cs"])
        ap.add_argument("--fold", default="f26")
        a = ap.parse_args(argv[1:])
        folds = sorted(FOLDS) if a.fold == "all" else [a.fold]
        for f in folds:
            score_models(f, a.tags)
        return 0

    if cmd == "features":
        ap = argparse.ArgumentParser(prog="mlb_ml.py features")
        ap.add_argument("tag", nargs="?", default="C")
        ap.add_argument("--fold", default="f26")
        ap.add_argument("--top", type=int, default=40)
        a = ap.parse_args(argv[1:])
        feature_report(a.tag, a.fold, a.top)
        return 0

    if cmd == "dataset":
        ap = argparse.ArgumentParser(prog="mlb_ml.py dataset")
        ap.add_argument("seasons", nargs="*", type=int, default=[2026])
        ap.add_argument("--workers", type=int, default=None)
        ap.add_argument("--refresh", action="store_true")
        ap.add_argument("--limit", type=int, default=None,
                        help="first N cutoffs only — a smoke run")
        a = ap.parse_args(argv[1:])
        for s in (a.seasons or [2026]):
            build_dataset(s, workers=a.workers, refresh=a.refresh,
                          limit=a.limit)
        return 0

    if cmd == "dataset-report":
        ap = argparse.ArgumentParser(prog="mlb_ml.py dataset-report")
        ap.add_argument("seasons", nargs="*", type=int, default=[2026])
        a = ap.parse_args(argv[1:])
        for s in (a.seasons or [2026]):
            dataset_report(s)
        return 0

    if cmd == "pa-report":
        ap = argparse.ArgumentParser(prog="mlb_ml.py pa-report")
        ap.add_argument("seasons", nargs="*", type=int, default=[2026])
        a = ap.parse_args(argv[1:])
        for s in (a.seasons or [2026]):
            pa_report(s)
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
