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

Everything lives here on purpose. The only outside dependencies are
`weatherman` (park geometry, wind rotation) and `homerunwidget`
(BallFlightSimulator, CD_NEUTRAL), both imported LAZILY inside the functions
that need them so this module stays importable without scipy, pywavefront or
a QApplication.

See `sim_state.md` for the spec, what is validated, what is broken and the
traps. (It used to be section 8 of `instruc_effort_MLB.md`, which is the
EffortMLB VIEWER's document — do not put engine material back there.) Four of
the traps matter enough to repeat here:

  * **Fatigue is smooth in pitch count / BF, never a step at batter 19.**
    Brill, Deshpande & Wyner (arXiv:2210.06724) show the apparent
    times-through-the-order discontinuity does not survive controlling for
    batter/pitcher quality and selection. `sp_tto3` and `tto_penalty()` in
    EffortMLB.py measure MANAGER BEHAVIOUR and must not be used as outcome
    multipliers.
  * **Fatigue must also be CENTRED** (`FATIGUE_REF_BF`) — a pitcher's season
    rates already contain his own average fatigue, so an uncentred multiplier
    charges it twice.
  * **Recency weighting is worth far more than its log-loss gain suggests**
    (arXiv:2511.17733) — but it is not free and it is not universal. Measured
    on two seasons, within-season recency helps HITTERS (pooled t +3.09) and
    is null for PITCHERS with the two seasons at opposite signs, because a
    pitcher's sample is small enough that discarding a third of its effective
    weight costs more than the staleness it removes. `RECENCY_HALF_LIFE_PIT`
    is 0.0 on purpose. sim_state.md 3d.9.
  * **Base-running detail buys nothing for WIN PROBABILITY** (same paper) —
    but that is a narrower claim than it first reads, and taking it as
    "base-running does not matter" is wrong here. The paper asked whether
    better base-running TRANSITIONS improved a manager's pull/hold decision.
    We are pricing stolen bases, runs scored and RBI, where the runner's own
    ability is the quantity being bet on. So the transition CONSTANTS stay
    coarse, while WHO is running is per-player: every advancement roll is
    taken by the specific runner, at his own steal rate and speed
    (`Batter.steal_attempt/steal_success/speed`, filled by `runner_profile`).

Anything pooled across processes must stay at module level.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
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
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

# ---------------------------------------------------------------------------
# **This module must be ONE object however it is entered.**
#
# Run as `python mlb_sim.py ...` this file is `__main__`, and `sys.modules` has
# no entry called "mlb_sim" at all. So the moment anything it imports does
# `import mlb_sim` — `mlb_ml` does, to reach `pa_rates` and the outcome
# constants — Python builds a SECOND, independent copy with the SHIPPED
# defaults. Two module objects, one name, and every constant the A/B harness
# rebinds is invisible to the second one.
#
# It fails silently and it already cost a full A/B. `ab_configure` ablates
# catcher framing (`FRAMING_TILT_SCALE = 0.0`) for every arm; the second copy
# kept the shipped 0.6394, so the ML adjuster computed its baseline WITH a
# framing term the simulator was running WITHOUT, and every multiplier it
# returned carried the difference. `ML_SELF_CENTRE` read False forever, making
# two arms byte-identical — which is the only reason this was ever noticed,
# and is exactly the tell §3d and `ab_score` tell you to treat as a bug report
# rather than a null.
#
# `__mp_main__` is in the tuple because `forkserver` and `spawn` import the
# main module under THAT name, so a pool worker hits the identical trap by a
# different door.
if __name__ in ("__main__", "__mp_main__"):
    sys.modules.setdefault("mlb_sim", sys.modules[__name__])
# ---------------------------------------------------------------------------

import bmielke_core

# **These stay LAZY on purpose, and the module docstring depends on it.**
# `weatherman` imports PyQt6 at module scope and `homerunwidget` pulls scipy
# and pywavefront, so hoisting either would make this module un-importable in
# a headless environment — which is the whole point of it being Qt-free.
# `pandas` / `numpy` / `bs4` / `OddsPortalClient` are only needed on a few
# paths and are heavy enough not to pay for on every import.
#
# `weatherman` was being re-imported inside five separate functions; `_wm()`
# caches it so the laziness costs one lookup instead of five import
# statements scattered through the file.
_WEATHERMAN = None


def _wm():
    """The `weatherman` module, imported on first use and cached."""
    global _WEATHERMAN
    if _WEATHERMAN is None:
        import weatherman
        _WEATHERMAN = weatherman
    return _WEATHERMAN

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

# MEASURED off 700 games of play-by-play runner movement, not assumed.
# Extraction note: a runner's advance can span SEVERAL movement records
# (1B->2B then 2B->3B), so the final base must be taken per runner id — reading
# the first record reported first-to-third at 0.08% instead of 36%.
#
# **A runner who HOLDS generates no movement record**, which is why the four
# rates below it — sac fly, double play, and the two ground-out advances —
# were left as league estimates and recorded in 5.6c as unmeasurable. They are
# unmeasurable *by that method only*. Counted as OUTCOMES — take the base-out
# state before the play and ask what happened — the holders are just the
# denominator minus the numerator, and nothing has to be inferred from an
# absence. `collect_baserunning` (section 15b) is that pass, and every value
# here now comes off it, with the old hand-set number kept as the fallback so
# a missing cache degrades to the shipped model rather than to zero.
#
#   python mlb_sim.py baserunning --refresh     # rebuild the cache
#   python mlb_sim.py baserunning               # measured against shipped


# Which season's base-running the constants below are read from. Like
# `DEPLOY_SEASON`, this exists so a backtest of an earlier year is not run on a
# later year's league — though unlike deployment these rates are close to flat
# across seasons, so it is a smaller exposure than the hook curve's. Declared
# HERE rather than beside the collector in section 15b, because a second copy
# of the number is a second thing to forget to change.
BASERUN_SEASON = 2026


def _measured_baserunning(season: int = BASERUN_SEASON) -> Dict[str, float]:
    """The `rates` block of `savedata/baserunning_<season>.json`, or {}.

    Defined HERE, 1,800 lines above `SAVE_DIR`, and building its path the same
    way `SAVE_DIR` does rather than waiting for it. These constants are read on
    `advance()`'s hot path and have to hold their measured values before
    anything downstream can capture a shipped one as a default argument — the
    frozen-default trap section 8 records twice.
    """
    try:
        with open(Path(__file__).resolve().parent / "savedata"
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
# inside `PA - SO - BB - HBP - H`, so without this the engine turns roughly
# half a baserunner per team-game into an out, and pays for it twice: the
# runner never appears AND the inning ends sooner. A ROE is an at-bat and not
# a hit, which is exactly how GB_OUT is already recorded, so only the base/out
# state changes here. 0.038 of ground-ball outs gives 0.31 reaches per
# team-game and lands league scoring on 4.41 R/G against a real 4.40. (0.045
# matches the reach rate more exactly at 0.37 but pushes scoring to 4.45 —
# runs is the aggregate every price keys off, so it wins the tie.)
P_REACH_ON_ERROR = 0.038

# "Free" advancement — everything that moves a runner without a batted ball:
# stolen bases, wild pitches, passed balls, balks, errors, defensive
# indifference, and extra bases taken on throws. Modelling none of it left run
# expectancy short by up to 0.40 runs in the states with the most runners and
# the most outs remaining, while bases-empty and two-out states matched
# exactly — which is the signature that says the PA model is fine and the
# BASE-RUNNING is not. Both constants are calibrated against our own measured
# RE24 (`savedata/pbp/season_2026_v2.json`); re-fit them before trusting a
# changed advancement model.
# Steals are modelled as an ATTEMPT with a success rate, not as free bases.
# Modelling only successful steals is the tempting shortcut and it is wrong in
# a way that flatters the model: it hands out the extra base and never charges
# the out. Calibrated against measured opportunity — 9.78 steal-eligible PAs
# per team-game — so 0.096 gives ~0.94 attempts, ~0.73 steals and ~0.21 caught,
# which are the real league marks.
P_STEAL_ATTEMPT = 0.096         # runner on 1st, 2nd unoccupied
# **5.6c called this one "known WRONG" and it is not.** The league rate it is
# supposed to carry measures 0.782 against the 0.78 shipped here. What is
# wrong is the SIMULATED success rate, 0.86, and this constant is not what
# sets it: it is only the fallback for a runner with no board profile, and
# every real hitter arrives with his own `steal_success`. The 0.86 comes from
# WHERE the attempts land, not from what they are worth — see `runner_profile`
# — so the fix is the battery, exactly as 5.8 says, and changing this number
# would move nothing but the fallback.
P_STEAL_SUCCESS = _MEASURED_RUN.get("steal_success", 0.78)
# All runners move up one; man on third scores. 17.38 runner-on PAs per
# team-game puts this at ~0.38 events, against a real WP+PB+balk rate of ~0.40.
P_WILD_ADVANCE = 0.022

# ---------------------------------------------------------------------------
# 3. Rate estimation — shrinkage and recency
# ---------------------------------------------------------------------------
# Per-outcome stabilisation points, in plate appearances: the sample at which
# a player's own rate and the prior carry equal weight. K and BB stabilise
# fast, the batted-ball outcomes slowly, which is the whole reason a flat
# "min PA" gate is wrong.
#
# **SPLIT BY SIDE 2026-08-15, and one shared table was badly wrong for
# pitchers.** The old single table was, in effect, the HITTER column applied to
# both sides. Measured two independent ways, each with the opposite failure
# mode, so where they agree the number is solid:
#
#   A. WITHIN-SEASON  var_true = var_observed - E[binomial noise]. The cohort
#      is selected on playing time, which inflates var_obs and UNDERstates M.
#   B. CROSS-SEASON   cov(rate_2025, rate_2026) IS var_true, because the two
#      years' sampling noise is independent — no noise model at all. But true
#      talent moves between seasons, which OVERstates M.
#
#   pitchers      A       B     shipped was
#     K          84      89      60
#     BB        238     230     120
#     GB_OUT    140     111      80
#     AIR_OUT   159     144      80
#     HR        566     668     170
#     1B       1338     524     290
#     2B        inf    1888     350
#     3B        inf   31732     380
#
# K, BB, GB, AIR and HR agree to within ~20% across two estimators that bracket
# the truth from opposite sides. The model was trusting a pitcher's own home-run
# rate 3-4x too much and his contact outcomes 2-6x too much, while trusting his
# K and ground-ball rates too little — **which is precisely SIERA's thesis,
# arriving here as a measurement rather than a borrowed formula.** For doubles
# and triples the within-season estimator finds NO detectable pitcher skill at
# all and the cross-season one finds nearly none.
#
# Where the two agree the geometric mean is taken; where they disagree (1B,
# and the 2B/3B infinities) the CROSS-SEASON figure is used, because it makes
# fewer assumptions and errs toward more shrinkage — and over-trusting is the
# demonstrated failure mode here, not under-trusting.
#
# Hitters carry the within-season column only: `fg_bat_2024/2025.json` are not
# on disk, which is the standing data gap in section 5b. Their numbers were
# already close to shipped except doubles, triples and home runs.
STABILIZE_MAX = 3000.0    # a measured 31,732 is "no skill"; the cap says so
                          # without pretending to that precision

# **MEASURED, and NOT SHIPPED — the numbers below are right and using them made
# the model predict WORSE.** Recorded in full because the finding is the point,
# not the constants.
#
#   pitchers      A       B     shipped   (A within-season, B cross-season)
#     K          84      89      60
#     BB        238     230     120
#     GB_OUT    140     111      80
#     AIR_OUT   159     144      80
#     HR        566     668     170
#     1B       1338     524     290
#     2B        inf    1888     350
#     3B        inf   31732     380
#
# Scored on the real slate at 60 sims/game across three seeds, paired:
#
#                        game total    RMSE      corr
#     shared table         8.8538     4.3786    +0.2847
#     split by side        8.9010     4.3904    +0.2756
#
# The split HALVES the level error (-0.100 -> -0.053) and costs 0.0091 of
# correlation — and the correlation cost is real, not noise: all three paired
# seeds move the same way, t ~ 4. Applying only the hitter table was pure cost,
# moving nothing and losing the same accuracy.
#
# **Why a better talent estimate predicts worse, which is the useful part.** The
# decomposition measures TRUE TALENT. Prediction wants talent PLUS whatever
# else in a pitcher's season line will still be true tonight — his park, the
# defence behind him, his catcher, his role. Those recur, so they are
# legitimately predictive, and regressing to pure talent throws them away.
#
# That makes this a SEQUENCING problem, not a wrong measurement. The persistent
# context has to be modelled explicitly before the rates can be regressed to
# talent: the park term is gone (section 6), defence is in via OAA (section
# 19), and **catcher framing is not modelled at all yet**. Revisit these tables
# once framing lands — at that point the split should cost less and may cost
# nothing.
#
# Do NOT reach for stabilisation as a level knob in the meantime. It is an
# estimator; the residual level gap is the talent-versus-realised-outcome
# question in section 5.9, and it wants the missing MECHANISM.
# **Both estimators, merged — 2026-08-16.** The hitter column used to be
# within-season only, because `fg_bat_2024/2025.json` were not on disk; they
# are now, so the cross-season estimator runs for BOTH sides and the two
# bracket the truth from opposite directions (within UNDERstates, its cohort
# being selected on playing time; cross OVERstates, talent moving between
# years). Geometric mean of the pair, capped at STABILIZE_MAX.
#
# The cross-season figure is itself the mean of TWO independent season pairs
# (2024-25 and 2025-26) which agree closely — batter K 63/55, BB 132/137,
# HR 241/256, AIR_OUT 134/132 — so this is three converging estimates, not one.
#
#   bat        within  cross  MERGED  shipped
#     K            52     59      55       60
#     BB          115    135     125      120
#     HBP         237    263     250      240
#     GB_OUT      105    117     111       80
#     AIR_OUT     131    133     132       80
#     1B          297    263     279      290
#     2B         2565   2125    2335      350   <- 6.7x
#     3B          686    464     564      380
#     HR          239    249     244      170   <- 1.4x
#
# K, BB, HBP and 1B ship correctly for hitters. The contact outcomes do not:
# a hitter's DOUBLES rate is trusted nearly seven times too much.
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

# **SHIPPED 2026-08-16, and the earlier refusal is why it works now.** Putting
# the measured tables in during August made the model predict WORSE (corr
# +0.2847 -> +0.2756), and the recorded diagnosis was SEQUENCING rather than a
# wrong measurement: the decomposition estimates TALENT, while prediction wants
# talent plus the persistent context that recurs — park, defence, catcher,
# role. Regressing to pure talent throws that context away unless it is
# modelled explicitly first.
#
# Since then framing, park run factors and team defence all landed, and this
# session fixed the playing-time prior and the league baseline. Re-scored
# against the CLOSING LINE on 3,856 games with all of it in place, the split
# now HELPS: pooled model-vs-market t -1.24 -> -0.77, 2026 alone +0.14 ->
# +0.46, pooled ROI -2.8% -> -1.5%. Paired on identical games and seeds,
# log-loss +0.001124 +/- 0.000779 (t +1.44).
#
# The lesson is the sequencing one: a better estimate of a PART can hurt until
# the parts it was implicitly standing in for are modelled.
STABILIZE_PA_BAT: Tuple[float, ...] = STABILIZE_PA_MEASURED_BAT
STABILIZE_PA_PIT: Tuple[float, ...] = STABILIZE_PA_MEASURED_PIT


def stabilize_for(side: str) -> Tuple[float, ...]:
    """The stabilisation table for one side. `side` is "bat" or "pit"."""
    return STABILIZE_PA_PIT if side == "pit" else STABILIZE_PA_BAT


def recency_weights(n: int, half_life: float = 500.0) -> List[float]:
    """Exponential-decay weights over `n` plate appearances, oldest first.

    arXiv:2511.17733 found recency weighting produced substantial decision
    value while barely moving log loss. Their schedule puts ~40% of the weight
    on the most recent 500 PA decaying to ~10% on the oldest, which
    `half_life=500` reproduces closely.

    **This docstring used to claim the module "never sums raw season totals",
    and that was false for two years.** Nothing called `weighted_counts`; the
    rate layer ran `outcome_counts` over FanGraphs season totals, which carry
    no ordering, and the only recency was `SEASON_HALF_LIFE` across seasons.
    A documented capability the code does not have is worse than an absent
    one. What is true now: within-season recency runs for HITTERS, over
    windows recovered by differencing the as-of boards (`board_windows`,
    `recency_counts`), and is measured OFF for pitchers — see
    `RECENCY_HALF_LIFE_PIT` and sim_state.md 3d.9.
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
                 league: Sequence[float] = LEAGUE_BASELINE,
                 stabilize: Sequence[float] = STABILIZE_PA) -> List[float]:
    """Empirical-Bayes shrink an outcome-count vector toward the league.

    Each outcome is shrunk with its OWN stabilisation weight, so a hitter with
    200 PA is nearly fully trusted on strikeouts and barely trusted at all on
    triples. Shrinking the whole vector by one factor — the usual shortcut —
    gets both ends wrong at once.
    """
    n = sum(counts)
    out = []
    for i in range(N_OUTCOMES):
        obs = counts[i] / n if n > 0 else league[i]
        w = n / (n + stabilize[i]) if n > 0 else 0.0
        out.append(w * obs + (1.0 - w) * league[i])
    return _normalize(out)


def league_baseline_from_counts(counts: Sequence[float]) -> List[float]:
    """League baseline from raw leaguewide outcome counts. Use this to refresh
    LEAGUE_BASELINE off our own play-by-play rather than trusting the constant.
    """
    return _normalize(list(counts))


def _normalize(v: Sequence[float]) -> List[float]:
    tot = sum(v)
    if tot <= 0:
        return list(LEAGUE_BASELINE)
    return [x / tot for x in v]


# ---------------------------------------------------------------------------
# 4. Matchup — log5 in log space
# ---------------------------------------------------------------------------

# How hard to apply the Morey-Cohen tail damping. 1.0 = the full
# `4*l*(1-l)` factor; 0.0 = plain log5, which is the standard method and what
# arXiv:2511.17733 uses.
#
# **1.0 was indefensible and is the defect this constant exists to fix.** At
# the home-run rate the factor is 0.116, so a slugger facing a homer-prone
# pitcher kept 12% of his edge, and a triples edge 1%. It compressed the
# model's game-to-game spread to 0.35 of a real full-slate market while adding
# nothing to correlation. 0.25 keeps 58% of a home-run edge.
#
# **This value is a judgement call, not a fitted one — say so before quoting
# it.** Two market samples disagree about the optimum and neither is big
# enough to settle it:
#
#   sample                       n    market sd   best alpha by MAE
#   Bovada full slate 08-15     10      0.781     0.25-0.40
#   OddsPortal, 2+ books        13      0.432     1.00
#
# The disagreement is a MEASUREMENT artifact, not a real one: requiring 2+
# books drops the thinly-quoted games, which are disproportionately the
# extreme totals, so that sample's market sd (0.432) is far narrower than a
# real slate's (0.781) and every sd-ratio computed against it is inflated.
# Correlation is flat (+0.44 to +0.52) across every alpha in both samples, so
# it discriminates nothing. Settling this needs a few hundred games of
# full-slate closing lines — not ten.
#
# **Set to 0.0 = plain log5, the standard method.** Hedging at 0.25 was
# unjustified: it cost a third of the recovered spread (sd/market 0.65 vs 0.84)
# to buy a correction that no measurement supports at any strength. Prefer the
# standard method until a correction earns its place on real data.
LOG5_TAIL_ALPHA = 0.0

# Gain on the log5 DEVIATION. 1.0 is plain log5 and is what ships.
#
# **The one lever in the engine that is targeted at mismatches by
# construction.** `dev` is log(b) + log(p) - 2 log(l): it is ~0 when a batter
# and pitcher are both league-average and grows with the mismatch, so scaling
# it moves the extreme cell and leaves balanced games alone. Every other
# amplitude change measured in 4e — the hitter prior, OAA — moves player RATES
# and therefore widens all 4,025 games, which is why they overshoot the middle
# and stall at ~27% of the gap (4f).
#
# It exists because of the sharpest result in 4e: bucketing the favourite's run
# error by the UNDERDOG STARTER'S QUALITY, the model is EXACT against good and
# average starters (+0.002, t +0.02, twice) and wrong only against bad ones
# (+0.339, t +3.58; +0.651, t +3.50 in lopsided games). Sample size does not
# discriminate — this is quality, i.e. the good-offence x bad-starter cell of
# the matchup function, not either player's own rate vector.
LOG5_GAIN = 1.0


def log5(batter: Sequence[float], pitcher: Sequence[float],
         league: Sequence[float] = LEAGUE_BASELINE,
         tail_correction: bool = True) -> List[float]:
    """Combine a batter and pitcher outcome vector against the league.

    Odds-ratio log5 carried out in log space and renormalised, per
    arXiv:2511.17733. Morey & Cohen (JSA 2015) showed the odds-ratio form
    skews increasingly at asymmetric probabilities — which is exactly the HR
    (~3%) and K (~22%) regime we price — so `tail_correction` damps the
    combination as an outcome's league rate moves away from even money.

    The correction is a shrink of the log-space DEVIATION, not of the result,
    so it cannot reorder two hitters; it only stops the tails running away.
    """
    out = []
    for i in range(N_OUTCOMES):
        b, p, l = batter[i], pitcher[i], league[i]
        if b <= 0 or p <= 0 or l <= 0:
            out.append(max(b * p, 1e-12))
            continue
        # log5 in log space: log(b) + log(p) - log(l)
        dev = math.log(b) + math.log(p) - 2.0 * math.log(l)
        if tail_correction and LOG5_TAIL_ALPHA > 0:
            # 4*l*(1-l) is 1 at l=0.5 and falls toward 0 at either tail.
            #
            # **Applied at full strength (alpha=1) this is far too severe and
            # was the single biggest defect in the engine.** At the home-run
            # rate (l=0.03) the factor is 0.116 — it kept 12% of a hitter's
            # home-run edge, and 1% of a triples edge. Those rare outcomes are
            # exactly what separates one game's total from another's, so it
            # crushed the model's game-to-game spread to 0.35 of the market's
            # while adding NOTHING to correlation (+0.444 vs +0.453 with it
            # off). Morey & Cohen's point is that the odds-ratio form
            # overshoots in the tails, not that tail information should be
            # discarded. `LOG5_TAIL_ALPHA` tunes how much of it to believe.
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
# **MEASURED TO BE ZERO (2026-08-15), and it used to be 0.004.** Off 79,483
# starter plate appearances of StatsAPI play-by-play over 1,838 games, taken
# WITHIN pitcher so it cannot read pitcher quality, and restricted to the 1,403
# starts that reached 24 batters so survivorship is removed inside the window:
#
#     measured slope   -0.00019 +- 0.00034 RV/batter    t -0.56
#     0.004 implies    +0.00135 RV/batter               t +4.20
#
# (The second line is this module's own conversion, checked against the engine:
# one unit of the multiplier is 0.3378 runs per PA, so 0.004/batter is 0.00135
# — against 0.00141 measured independently from the play-by-play. The two agree
# to 4%, which is what makes the comparison above a like-for-like one.)
#
# So the shipped value sat 4.2 standard errors off the data, and it was not a
# harmless 4 sigma: centred at bf 11.5 it handed the starter a 4% BONUS for the
# first two batters of the game and a 5% penalty by batter 24. On the real
# slate that alone put inning 1 at 0.460 against a real 0.531 — the sim's
# LOWEST-scoring inning where reality has its HIGHEST.
#
# What survives from the literature is the SHAPE, not the size: fatigue must
# stay smooth in batters faced with no step at batter 19 (Brill, Deshpande &
# Wyner, arXiv:2210.06724). This measurement says the slope is flat; it does
# not say the curve may have a discontinuity. The term is kept rather than
# deleted because zero is a MEASURED VALUE here, not a term that failed —
# unlike the park term (section 6 of sim_state.md), which was scored on its own
# claim and removed. `python mlb_sim.py calibrate-fatigue` re-derives it.
FATIGUE_DECLINE_PER_BF = 0.0

# Forces the per-PA fatigue call even when the shipped gradient is zero, so
# `calibrate_fatigue` can score a variant that acts somewhere other than on the
# slope. Set by that probe and nothing else — the hot loop skips the call
# entirely at a zero gradient, which is why a probe cannot simply swap the
# function out.
_FATIGUE_FORCE = False


def fatigue_multipliers(bf: int, decline_per_bf: Optional[float] = None,
                        ref_bf: float = FATIGUE_REF_BF) -> Dict[int, float]:
    """Continuous within-game decline, as a multiplier bundle.

    Deliberately smooth in batters faced. Brill/Deshpande/Wyner show the
    apparent times-through-the-order discontinuity does not survive
    controlling for pitcher quality and selection, so there is NO step at
    batter 19 here and there must not be one.

    **Centred on `ref_bf`, and that is required, not cosmetic.** A pitcher's
    season rates already contain his own average fatigue — they are the mean
    over every batter he faced, late ones included. A multiplier that starts
    at 1.0 and only ever rises therefore charges the fatigue twice and
    inflates league offence: uncentred, this alone put league batting average
    6 points high and walks 4% high with every other input at league level.
    Centring leaves only the within-start GRADIENT, which is the real effect,
    and lets the level stay where it belongs — in the pitcher's own rates.

    The gradient itself is `FATIGUE_DECLINE_PER_BF`, measured at zero — so this
    returns a flat bundle unless a caller passes its own slope, which is what
    the calibration probe does.
    """
    d = 1.0 + (FATIGUE_DECLINE_PER_BF if decline_per_bf is None
               else decline_per_bf) * (bf - ref_bf)
    d = max(d, 0.5)
    return {HR: d, S1B: d, S2B: d, BB: d,
            K: 1.0 / d, GB_OUT: 1.0 / d, AIR_OUT: 1.0 / d}


# --- PITCHES, and why the hook needs them --------------------------------
# **A manager hooks on the PITCH COUNT and this engine hooked on BATTERS
# FACED**, which cannot tell 75 pitches through six from 105 through four.
# Measured on 3,728 real starter stints (2026 PBP, pitch counts attached
# 2026-08-18):
#
#   * pitches is the TIGHTER constraint — CV 0.2248 against BF's 0.2366, and
#     0.1110 against 0.1244 on starts of 4+ innings. The manager holds pitches
#     more nearly constant than batters, which is what "hooks on pitches" means
#     as a measurement rather than a belief;
#   * 71% of starts end between 80 and 99 pitches — the ~100-pitch convention,
#     visible in the histogram;
#   * and the decisive one: P/BF WITHIN a start has sd 0.417 (p10 3.35, p90
#     4.41), nearly 3x the 0.148 spread ACROSS starters. At a fixed 100-pitch
#     hook that is a 7.2-batter swing, close to two innings, and a BF-indexed
#     hazard is blind to all of it.
#
# That blindness is measurable in the output: simulated starter BF sd 4.63
# against a real 5.13, with 4-inning starts at 27.8% against a real 16.1% and
# 7-inning starts at 4.5% against a real 11.0%. The MEAN is right (21.43 vs
# 21.63), which is why every aggregate check has passed.
#
# Pitch cost per outcome, fitted across 721 arm-seasons with 200+ TBF
# (2024-25 boards), no intercept because every plate appearance costs pitches:
# R2 99.47%, and the values land on the known league figures.
# **These are the BASE means of the pre-floor draw, not the fitted values.**
# The fit gives 4.891 / 6.749 / 3.243, but the floors below (a strikeout cannot
# take fewer than 3 pitches, a walk fewer than 4) truncate the left tail and
# push the realised mean up ~0.14. Solved back by fixed point so the POST-FLOOR
# mean lands on the fitted number: verified 4.893 / 6.754 / 3.246.
PITCHES_PER_K = 4.685
PITCHES_PER_BB = 6.678
PITCHES_PER_BIP = 3.108      # ball in play, plus HBP


# **PER-START FRAILTY on the hook, which is what the deep-start tail needs.**
# A marginal hazard applied independently at each batter gives every start the
# average pull probability, and the survival product then decays too fast to
# reach 27 outs: the sim produced complete games at 0.190% against a real
# 0.431% (2026) and 0.693% pooled over 2021-26, and 6.9% seven-inning starts
# against a real 11.0%.
#
# Real deep starts happen because a LATENT state — he has it tonight — lowers
# the hazard at every batter simultaneously. That is unobserved heterogeneity
# in a survival model, and the standard treatment is a per-subject frailty
# multiplier. One draw per start, lognormal so it is positive and multiplicative.
#
# **Score is deliberately NOT a second dimension.** Raw, the hook pitch count
# runs 91.0 with a big lead against 79.8 in a close game — but conditioned on
# how deep the start got it is FLAT (86.0/85.4/88.2 at 4-5 IP, 90.9/90.9/93.5
# at 6 IP), so the raw spread is the confound "he is ahead because he is
# dealing", which the engine already reproduces structurally. Adding a score
# term would count it twice — section 5.4's rule, and the sign was the tell.
# **Measured 2026-08-18 on 12,000 simulated starts per setting.** No single
# value fits every target, and that is the finding rather than a tuning
# failure:
#
#   frailty   BF     sd    IP    >=7IP   >=8IP     CG
#   REAL     21.65  5.12  5.10  11.00%  1.93%   0.35-0.43%
#   0.00     21.52  5.25  4.97   6.67%  0.94%   0.158%
#   0.25     21.70  5.42  5.00   7.92%  1.59%   0.283%
#   0.40     21.93  5.61  5.05   9.48%  2.31%   0.458%
#   0.55     22.26  5.86  5.13  11.68%  3.58%   0.808%
#
# >=7 IP wants ~0.50, >=8 IP wants ~0.33, complete games want ~0.37. Pushed
# high enough to reach 11% seven-inning starts it produces TWICE the real
# complete games. Reality has a sharper cut-off near 100 pitches than a
# lognormal frailty can make, so one parameter buys the tail at the cost of
# the marginal spread (sd 5.25 -> 5.61 against a real 5.12).
#
# 0.40 is the value the A/B arm uses: it lands the complete-game rate (0.458%
# against a board 0.431%) and the innings level (5.05 against 5.10), which are
# the quantities the tail was wrong about. Ships OFF until a price says
# otherwise, like everything else here.
HOOK_FRAILTY_SD = 0.0        # 0 disables
USE_PITCH_HOOK = False       # A/B decides, like every other term here
# Starts at or below this many batters are OPENERS, which carry their own
# hazard, and must not also sit in the ordinary-starter curve.
OPENER_BF_MAX = 10
# Bucket width for the pitch hazard. One entry per pitch is 120 near-empty
# bins on 3,700 starts; five keeps each bin populated without blurring the
# 80-99 band where the decision actually lives.
PITCH_HOOK_BUCKET = 5


def hook_hazard_pitches(pitch_distribution: Sequence[float],
                        bucket: int = PITCH_HOOK_BUCKET) -> List[float]:
    """Discrete-time hazard of being pulled, indexed by PITCHES thrown.

    Same construction as `hook_hazard` one variable over, so the two cannot
    drift apart on a definition: h[k] = P(pulled in bucket k | still in).
    """
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

    **The engine has been building this from a 15-element hardcoded list**
    — `[18, 20, 21, 22, 22, 23, 23, 24, 25, 26, 27, 19, 21, 24, 20]`, repeated
    at ten call sites — whose sd is 2.49 against a real 5.12 over 3,728 starts.
    A stand-in 2.1x too tight compresses every simulated start toward the
    middle, which is most of why the sim produced 27.8% four-inning starts
    against a real 16.0% and 4.5% seven-inning ones against a real 11.0%.

    The real distribution has been on disk since 5.6 built `reliever_stints`
    for the RELIEVER shape; nothing ever read the starter half of it.
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
    return hook_hazard_pitches(got) if len(got) >= 500 else None


# **Per-PA pitch counts must be STOCHASTIC, and this is the whole reason the
# deep-start tail exists.** A deterministic cost per outcome makes a start's
# pitch count a fixed function of its outcome mix, which carries almost none of
# the real spread: measured over 3,489 real starts, P/BF has sd 0.437, of which
# 0.148 is across-pitcher and **0.411 is WITHIN a start**. At ~23 batters that
# needs a per-PA sd of ~1.96 pitches — fouls, deep counts, quick first-pitch
# outs — none of which the outcome type alone knows about.
#
# Without it the hook has no latent state to condition on, every start gets the
# average hazard, and the survival product decays too fast to ever reach 27
# outs. That is unobserved heterogeneity (frailty) in a survival model, and it
# is why the sim produced HALF the real complete games (0.190% against 0.349%)
# and half the 8-inning starts even with a correctly-shaped marginal curve.
PITCH_PA_SD = 1.96


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
#
# **It is a cross-FILE contract, which is why it is a named constant rather
# than four inline `zip((1, 2, 4), ...)` sums.** `mlb_ml.pa_rows_from_plays`
# writes the same encoding into `savedata/pa/v2` off StatsAPI's
# postOnFirst/Second/Third, the ML residual is TRAINED on that column, and
# `simulate_game` SERVES it from here. Two copies in two files agreeing today
# is exactly the shape that drifts silently — a model trained on one bit order
# and served another still returns nine plausible probabilities. `mlb_ml`
# imports `BASE_STATE_BITS` from here and a test pins the two together.
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
            _r0 = _runner(lineup, b[0])
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
            _r1 = _runner(lineup, b[1])
            if rng.random() < scale_odds((_r1.adv or {}).get("second_scores")
                               if _r1 and _r1.adv else
                               scale_odds(P_SECOND_SCORES_ON_1B,
                                          _speed(lineup, b[1])), arm):
                scorers.append(b[1])
            else:
                third = b[1]
        if b[0] is not None:
            _r2 = _runner(lineup, b[0])
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


def _runner(lineup: Optional[List["Batter"]], slot: Optional[int]
            ) -> Optional["Batter"]:
    if lineup is None or slot is None:
        return None
    return lineup[slot % len(lineup)]


def _speed(lineup: Optional[List["Batter"]], slot: Optional[int]) -> float:
    r = _runner(lineup, slot)
    return r.speed if r is not None else 1.0


def running_game(state: HalfInningState, rng: random.Random,
                 lineup: Optional[List["Batter"]] = None) -> List[int]:
    """Steals, wild pitches and passed balls, resolved BETWEEN plate appearances.

    This has to sit outside `advance()`. A caught stealing can be the third
    out, and when it is, the batter at the plate never completes his plate
    appearance — he leads off the next inning instead. Folding the running
    game into the PA resolution would credit him a PA and an outcome that
    never happened, and skip him in the order next time round.

    Returns `(scorers, events)` — the batting-order slots that scored (these
    runs carry NO RBI), and what actually happened.

    **The events are not decoration; they fix a miscount.** The caller used to
    infer a stolen base from the state change — "a man who was on first and is
    now on second with no out made" — and the WILD PITCH branch satisfies that
    condition exactly, because it also moves the man on first to second without
    an out. Every wild pitch with a runner on first was therefore credited as a
    STOLEN BASE. Reporting the event removes the inference and the bug with it.
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
        runner = _runner(lineup, b[0])
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


def draw_outcome(rates: Sequence[float], rng: random.Random) -> int:
    u = rng.random()
    acc = 0.0
    for i, p in enumerate(rates):
        acc += p
        if u < acc:
            return i
    return AIR_OUT


# ---------------------------------------------------------------------------
# 7. The lineup, the staff, and one simulated game
# ---------------------------------------------------------------------------

@dataclass
class Batter:
    name: str
    rates: List[float]                     # shrunk, recency-weighted
    player_id: Optional[int] = None
    # --- the running game, PER PLAYER ---
    # These default to the league marks so a Batter built without them still
    # simulates, but they should be filled from `runner_profile` (section 9).
    # A league-constant running game makes every runner De La Cruz and every
    # runner Salvador Perez at the same time, which is wrong in both
    # directions at once and worst exactly where it is most bettable: steals,
    # runs scored, and first-to-third on a single.
    bats: str = ""                         # "L", "R" or "S"
    steal_attempt: float = P_STEAL_ATTEMPT
    steal_success: float = P_STEAL_SUCCESS
    speed: float = 1.0                     # odds multiplier on taking a base
    # His OWN advancement rates, blended from PBP history + XBR + speed by
    # `runner_advance_rates`. None falls back to the league constants.
    adv: Optional[Dict[str, float]] = None
    # Per-outcome context for THIS hitter in TONIGHT's conditions. Applied
    # AFTER log5, because folding it into his rates first would let the log5
    # tail correction damp it as though it were a skill claim.
    #
    # The park x weather home-run term used to live here too, on a `park_hr`
    # field. It was REMOVED on 2026-08-15 — see section 10 — because it was
    # measured worse than nothing on both game totals and home-run park
    # factors. Do not reintroduce a per-hitter park multiplier without a
    # measurement that beats leaving it out.
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
    # unlike the umpire, framing belongs to ONE side and does not cancel
    # within a game.
    #
    # **Per CATCHER when one is known, per CLUB otherwise.** The club figure
    # is a roster property and framing is a player skill: Patrick Bailey split
    # CLE 3,360 / SFG 2,053 inside a single season, so a club aggregate
    # carries the framing of men who have left. `catcher_id` is tonight's
    # posted catcher, from the lineup card's `primaryPosition`.
    framing: float = 0.0
    catcher_id: Optional[int] = None


@dataclass
class GameResult:
    batters: Dict[str, PlayerLine]
    pitchers: Dict[str, PitcherLine]
    runs_home: int = 0
    runs_away: int = 0
    # Runs in each HALF-INNING, in order. The engine draws every plate
    # appearance independently from a fixed matchup vector, so it has no
    # mechanism for an inning getting away from a pitcher — and the game-level
    # form draw is per team-GAME and cannot make a big inning. Whether that
    # leaves the runs-per-inning distribution too thin in the upper tail is
    # measurable, and was not being measured.
    half_runs_home: List[int] = field(default_factory=list)
    half_runs_away: List[int] = field(default_factory=list)


# --- Leverage, MEASURED from our own play-by-play ------------------------
# Nothing here is a chosen number. The leverage of a game state is read from
# `savedata/pbp/season_2026_v2.json` — the same win-expectancy accumulator
# EffortMLB builds its LI from — and the thresholds that decide which arm a
# manager reaches for are QUANTILES of that table's own distribution rather
# than invented cut-offs.
#
# State key matches EffortMLB's `we_key`: (inning capped at 10, is_top,
# lead clipped to +-4, runners ON, outs). Deliberately coarse — one season
# cannot support the full grid, and coarse-and-estimable beats fine-and-noisy.

_LI_TABLE: Optional[Dict[tuple, float]] = None
_LI_QUANTILES: Optional[Tuple[float, float]] = None
MIN_STATE_N = 30          # transitions before a state's leverage is believed


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
    path = path or (SAVE_DIR / "pbp" / "season_2026_v2.json")
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


def game_leverage(inning: int, is_top: bool, lead: int, on_base: int,
                  outs: int) -> float:
    """Leverage of the current state, looked up in the measured table.

    `lead` is from the PITCHING side's perspective. Falls back along the axes
    the table is thinnest on — blowouts and deep extras — before giving up and
    returning the league mean.
    """
    table, (med, _) = load_leverage_table()
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


# How hard a manager chases the platoon, as a function of the arm's measured
# entry inning. MEASURED off 2,606 real pitching changes — the percentage-point
# lift in P(batter is left-handed | a left-handed pitcher enters) versus the
# same probability when a right-hander enters:
#
#   avg entry inning 6 -> +20.2    7 -> +14.5    8 -> -3.0    9 -> -19.5
#
# Middle relievers ARE matchup pieces. **Closers are not** — they enter on the
# inning regardless of who is due up, which is why the lift goes NEGATIVE at 9.
# A flat platoon rule would have a manager passing over his closer to bring in
# a lefty specialist for one at-bat in the ninth, which is not what happens.
PLATOON_LIFT = ((6.0, 0.202), (7.0, 0.145), (8.0, -0.030), (9.0, -0.195))


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


def _choose_reliever(side: TeamSide, used: set, lev: float,
                     rng: random.Random,
                     ready: Optional[set] = None,
                     inning: Optional[int] = None,
                     run_diff: Optional[int] = None,
                     bat_hand: str = "",
                     is_home: bool = False) -> Optional["Pitcher"]:
    """Which arm comes in.

    Driven by MEASURED deployment traits rather than a rank order:

    * `avg_inning` — the inning insidethepen records him actually entering.
      Every identified closer in the league reads 9.0, setup 8.0, middle 6-7.
      This is a direct observation of the manager's decision, not a proxy.
    * `avg_run_diff` — the score margin he is trusted in, which is what keeps
      a closer out of a blowout without needing a leverage threshold.
    * `gm_li` — the leverage he is used in, as a tiebreak.

    An arm with no traits falls back to gmLI alone, so a pen assembled without
    the CSV still simulates.
    """
    avail = [p for p in side.bullpen
             if p.name not in used and (not ready or p.name in ready)]
    if not avail:                      # everyone rested or burned: go anyway
        avail = [p for p in side.bullpen if p.name not in used]
    if not avail:
        return None

    def score(p: "Pitcher") -> float:
        """P(this arm enters | this state), factored the way the decision is
        actually made. Availability is already a HARD GATE above; what is left
        decomposes cleanly and each factor is measured, not tuned:

            P(enters here) = P(he pitches at all)          <- base rate
                           x P(this inning | he pitches)   <- role
                           x P(this margin  | this inning) <- situation
                           x handedness

        **The base rate is the term that used to be missing, and it is why the
        pen read too flat.** `deployment_score` is a product of two CONDITIONAL
        distributions, each normalised over the pitcher's OWN appearances, so
        it says where an arm is used but nothing about how OFTEN — two arms
        with the same inning shape scored identically whether one pitched 46%
        of games or 15%. Oakland's Medina simulated 56.3% against a real 32.5%
        for exactly that reason.
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
            pw = platoon_weight(p.avg_inning)
            if pw > 0:
                same = (p.throws == bat_hand)
                sc *= (1.0 + pw) if same else max(1.0 - pw, 0.05)
        return max(sc, 1e-9)

    # **Sample proportionally, do not take the argmax.** Winner-take-all put
    # the top-scoring arm in essentially every game he was available for —
    # Tanner Scott simulated at 70% against a real 43%, above the league's
    # busiest reliever (53.4%). Proportional sampling makes an arm with twice
    # another's score appear twice as often rather than always, which is what
    # the real appearance rates look like.
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


# Scales each arm's real appearance rate into a per-game availability draw.
# Availability alone is not usage: an available arm still has to be SELECTED,
# so the probability of being available must exceed the target appearance
# rate. Solved by measurement in `validate_bullpen_usage()`, not chosen.
# **3.0, and the value is inseparable from the SCORER.** Availability models
# rest, not rationing: a real pen has 6-7 of 8 arms usable on a given day and
# the ROLE decides who pitches. At 1.0 only ~3.2 of 8 were available against
# ~3.2 changes needed, so the "nobody is ready" fallback fired constantly and
# whoever was left pitched regardless of role — the closer took 14% of his
# entries in the 6th/7th, where the real one has taken none.
#
# Measured against Mason Miller's own distribution (9th 84% / 8th 14% /
# 6th-7th 0%):
#   boost 1.0 -> 56% / 27% / 14%
#   boost 2.2 -> 78% / 18% /  1%
#   boost 3.0 -> 85% / 12% /  0%   <-
#
# Note this REVERSES an earlier finding: with the old hand-tuned formula
# scorer, raising the boost made everything worse, because a high-gmLI arm
# simply won more draws. With empirical per-pitcher distributions the extra
# availability is what lets the histograms do the routing.
#
# **RETIRED 2026-08-15 — it was double-counting `app_rate`.** Section 5.3
# flagged that this constant was doing two jobs, routing and rest, and that
# splitting them was worth doing. It was worse than that: once the base rate
# was correctly added to `_choose_reliever`'s score (section 5.5a), `app_rate`
# entered the decision TWICE — once as this availability gate and once as a
# multiplicative weight — so a marginal arm was suppressed roughly
# quadratically. Measured share of a club's relief work:
#
#                       real     app_rate x 3.0     flat
#     top 3 arms       42.5%          47.9%        42.4%
#     ranks 4-7        35.3%          40.7%        36.6%
#     ranks 8-13       19.0%          11.2%        20.0%
#
# The innings the sim took off ranks 8-13 are the WORST innings in a bullpen,
# so the league run environment came out too low — see section 5.9.
# `PEN_AVAILABLE_P` replaces it: availability is now REST ONLY, a flat draw,
# and `app_rate` lives solely in the selection score where it belongs. The
# constant is REMOVED rather than set to 1.0 — a neutralised knob is dead code
# with a switch on it, and this one would be turned back on for the reason
# recorded above, which no longer holds.

# Rest. A real pen has ~7.0 of 8 arms on hand on a given day with sd ~0.7
# (section 5.3), so this is 7/8 and is NOT a free parameter — raising it does
# not make arms pitch more often, because the selection score is normalised
# over whoever is available. It only decides how often the pen is short.
PEN_AVAILABLE_P = 0.875

# **`ENTRY_INNING_SCALE` and `ENTRY_DIFF_SCALE` were REMOVED 2026-08-23.**
# They read as live, fitted and load-bearing — "FITTED against the two
# validation targets ... not chosen" — and were never referenced anywhere in
# the tree. They were orphaned when the hand-tuned entry scorer was replaced
# by `deployment_score`'s empirical histograms, which read the manager's real
# entry distribution per arm instead of penalising a distance from it.
#
# Removed rather than left at their old values, on this file's own precedent
# twelve lines up: "a neutralised knob is dead code with a switch on it".
# A constant that cannot reach anything is worse than dead — the next person
# to tune the bullpen reads the docstring, changes the number, measures no
# effect, and concludes the mechanism does not matter. `sim_state.md` 5.12.


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

    Two decisions, deliberately separated because managers make them
    differently:

    * the STARTER is pulled on a hazard over batters faced, at any point in an
      inning — that is what a hook looks like;
    * a RELIEVER is almost always changed at an inning BOUNDARY, having gone
      about an inning. The old rule swapped arms after exactly four batters
      wherever that fell, which manufactured mid-inning changes that do not
      happen and gave every reliever the same workload regardless of role.

    Who replaces him is `_choose_reliever`, keyed on the measured leverage of
    the current state.
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

    # A reliever getting hit is pulled MID-INNING. Without this the sim can
    # only change arms at an inning boundary, so a reliever who cannot get
    # outs stays in forever — one simulated game had Will Klein face 10 men
    # and give up 6 runs while recording three outs, untouched. 14.8% of real
    # entries arrive with inherited runners, i.e. they are exactly this
    # rescue, and our sim was making none of them.
    #
    # Both scales are FITTED against the measured stint shape (§5.6), not
    # chosen: `collect_reliever_stints` gives 11,969 real appearances and
    # `validate_stint_shape` scores the sim through the same code. The first
    # version of this hazard was set by eye against the inherited-runner
    # figure alone and pulled relievers mid-inning 50.6% of the time against a
    # real 31.1% — which then also shortened appearances, so the arms that
    # survived had to cover more innings.
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

    # A reliever hands over between innings, once he has worked one.
    #
    # **The slack is what decides how many appearances span two innings.** At
    # 1.0, an arm whose `bf_per_outing` is the league's ~4.5 needs 3.5 batters
    # to be handed over, so retiring the side IN ORDER — three batters, the
    # single most common clean inning there is — did not qualify and he went
    # back out. That alone put 41.4% of appearances into 2+ innings against a
    # real 30.0%. Fitted against the measured shape (§5.6).
    if (inning_start and bf_by_pitcher.get(cur.name, 0)
            >= cur.bf_per_outing - RELIEF_HANDOVER_SLACK):
        if not (cur.multi_inning and lev < load_leverage_table()[1][0]):
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
# Extracted out of `simulate_game` on 2026-08-20, and the reason is the ML
# experiment in `mlb_ml.py` rather than tidiness. That experiment's whole
# method is to train a correction against "the vector the incumbent would have
# produced", which means the training script has to REBUILD this composition
# — and a second copy of it would drift from this one silently, putting a
# constant into every residual that the model would learn and report as skill.
# There is now one definition and both callers use it.
#
# The order is not arbitrary and has been wrong before:
#   * PLATOON first, on the hitter's own rates, BEFORE log5 combines him with
#     the pitcher — it is a property of this matchup, not a multiplier on the
#     combined result;
#   * fatigue before defence, because it is a property of the arm;
#   * the CATCHER belongs to the fielding side and suppresses THIS lineup only
#     — unlike the umpire, framing does not cancel within a game;
#   * `tilt` carries the game-form draw, the weather and the park on ONE axis,
#     which is deliberate (see `GAME_FORM_SD`).

# Which rate layer prices a plate appearance. "baseline" is shrinkage + log5 +
# context, the incumbent, and is what ships. The other two exist so the ML
# experiment can be an A/B ARM rather than a fork of the engine — see
# `mlb_ml.py`. Uppercase strings, so `_slate_overrides` carries them into a
# pool worker; a callable could not travel and the worker would silently run
# the baseline while the parent reported it as the variant.
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
# "shipped" is the hand-chosen `mlb_ml.LGB_NODE_PARAMS`, "tuned" is the result
# of the search in `mlb_ml` section 5b. It lives HERE rather than in `mlb_ml`
# so an A/B arm can select it and `_slate_overrides` carries it into a pool
# worker — a module-level switch in `mlb_ml` would be re-imported back to its
# default by forkserver and the arm would silently run the other one
# (sim_state.md trap 6).
#
# The default is "shipped" on purpose: `hier25` is a RECORDED result and must
# keep meaning what it meant when it was measured. `hier25tuned` is the new
# arm, and the two coexist on disk because the model path carries the
# configuration's fingerprint.
ML_NODE_PARAMS = "shipped"        # "shipped" | "tuned"

# **Which per-PA STATE columns the ML residual is allowed to read.** "" is the
# incumbent — the adjuster is memoised on (batter, pitcher, side, is_starter)
# and every state column multiplies that key space, so state was masked out of
# every deployable model.
#
# Measured 2026-08-23 on TEST seasons, joint nine-outcome log loss, both folds:
# ALL state is +76% on the residual's whole contribution, and BASE-OUT alone is
# +33% at a key-space cost of only 24 (8 base states x 3 out states). A game
# has ~270 distinct matchups, so that is ~6,500 rows in ONE batched predict —
# the "152,000 evaluations" objection in `GameAdjuster` was about predicting
# per-PA UNBATCHED and does not apply.
#
# `tto` measured +22% alone and is deliberately NOT offered here: it is a
# data-driven re-introduction of `FATIGUE_DECLINE_PER_BF`, which is a
# DELIBERATE null (Brill/Deshpande/Wyner), and the residual's training data
# carries exactly the quality-and-selection confound that paper warns about.
# It needs its own control, as a challenge to a documented result.
#
# Lives on `mlb_sim` and not `mlb_ml` so `_slate_overrides` carries it into a
# forkserver worker — trap 6.
ML_STATE_COLS = ""                # "" | "baseout"


ML_MODEL_FOLD = ""                # which walk-forward fold's model


def game_adjuster(season: int, as_of: str, row: dict,
                  home: "TeamSide", away: "TeamSide",
                  save_dir: Optional[Path] = None):
    """The trained rate correction for ONE game, or None when off.

    Built ONCE per game and handed to `simulate_many`, never looked up per
    plate appearance: a gradient-boosted model called 76 times a game times
    2,000 sims is four orders of magnitude more work than the simulation it is
    meant to inform. The returned callable memoises on (batter, pitcher,
    side) — the matchup, which is what its features are made of — so a real
    game costs a few hundred rows of prediction rather than 152,000.

    It is a PARAMETER rather than a module lookup on purpose. `RATE_MODEL` is
    a string and travels to a forkserver worker; a callable would not, and the
    worker would silently run the baseline while the parent reported it as the
    variant. That failure has happened three times in this file already
    (§_slate_overrides), and the way to not have it a fourth time is to make
    the thing that cannot travel be an argument.
    """
    # **A non-baseline arm that cannot build an adjuster RAISES.** This used
    # to be one `or not (...)` returning None, and a hierarchy arm — which
    # names its nodes in `ML_HIER_NODES` and has no flat `ML_MODEL_TAG` at all
    # — fell straight through it. The arm ran the incumbent and reported it
    # under its own name: `hier25` came out byte-identical to `base` on all
    # 1,750 games. That is the third time in this file a silently-disabled
    # variant has been caught by two result blocks agreeing exactly, and the
    # fix each time is the same one: make the impossible state loud.
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

    `context` optionally carries per-side outcome multipliers keyed "home"/
    "away" (park x weather, umpire, defence) applied to the BATTING side.

    The game STRUCTURE is modelled, not just nine fixed innings, because
    plate-appearance count is where the pricing value is and the structure is
    what determines it: the bottom of the ninth is not played when the home
    side already leads, a walk-off ends the half-inning mid-rally, and a tie
    goes to extras under the automatic-runner rule. Playing a flat nine hands
    every home batter roughly half an extra PA he does not really get.
    """
    rng = rng or random.Random()
    res = GameResult(batters={}, pitchers={})
    context = context or {}

    order = {"away": 0, "home": 0}
    mound = {"away": MoundState(), "home": MoundState()}
    # Rest state for tonight: who is PHYSICALLY available, and nothing else.
    # It must NOT depend on `app_rate` — the selection score already carries
    # that as the base rate, and gating on it here charged it twice and
    # starved the back of the pen (see `PEN_AVAILABLE_P`).
    # `p.availability` is the rest state carried in from outside — 1.0 fully
    # available, 0.0 not tonight. Declared and documented on `Pitcher` since
    # the class was written and, until now, read NOWHERE: the ITP rest filter
    # deleted resting arms from the roster instead of marking them.
    #
    # The draw is ALWAYS taken, so an all-1.0 pen — every path except the ITP
    # one — consumes the random stream exactly as before and is bit-identical.
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
    #
    # Weather rides the SAME axis, but deterministically and shared by both
    # sides, because the conditions are the same for everyone on the field.
    # Keeping them on one axis is deliberate: it makes the double-count
    # explicit, and `GAME_FORM_SD` must be re-calibrated whenever the weather
    # coefficients move, or the two model the same variance twice.
    wx = weather_tilt(weather, venue)
    pk = {"home": park_run_tilt(venue, True),
          "away": park_run_tilt(venue, False)}
    form = {"away": (draw_form(rng) + wx + pk["away"]
                     + team_quality_tilt(away.team_quality)),
            "home": (draw_form(rng) + wx + pk["home"]
                     + team_quality_tilt(home.team_quality))}
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
        # Runs the RUNNING GAME has scored since the last logged plate
        # appearance — a man on third brought home by a wild pitch. They are
        # real runs in `runs[half]` either way; this exists so `re24_report`
        # can attribute them to the state they were scored FROM. Without it
        # the sim's run expectancy is short by exactly the quantity the real
        # table it is compared against is also short by, for a different
        # reason, and neither error would have been visible.
        pending_runs = 0
        half_rows = 0
        while state.outs < 3:
            # Leverage from the PITCHING side's perspective, read off the
            # measured table rather than a formula.
            pit_half = "home" if half == "away" else "away"
            lev = game_leverage(inning, half == "away",
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
            # The running game happens BETWEEN plate appearances, so it is
            # invisible in a log that only records them — a reader sees a
            # runner teleport from first to second. Surfaced on its own list
            # rather than interleaved into `log`, whose row shape several
            # consumers depend on (`sim_stints`, the RE24 table, the
            # runs-by-inning vectors).
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
            # The base-out state reaches `pa_rates` only for the ML residual;
            # nothing else in the composition reads it. Computed inside the
            # guard because the shipped configuration is `ml is None`, and
            # `base_mask` on every one of ~600M plate appearances in a full
            # backtest is ~1.4% of the engine's wall clock for a value that
            # would be discarded.
            rates = pa_rates(bat, pit, faced=faced, oaa=pit_side.oaa,
                             framing=pit_side.framing,
                             is_home=(half == "home"), tilt=form[half],
                             mult=mult, ml=ml,
                             bases=(base_mask(state.bases)
                                    if ml is not None else 0),
                             outs=state.outs)

            outs_before = state.outs
            before_bases = list(state.bases)
            outcome = draw_outcome(rates, rng)
            scorers, rbi, sac_fly = advance(state, outcome, slot, rng,
                                            bat_side.lineup,
                                            arm_factor(pit_side.of_arm))
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
            _record(bline, outcome, rbi, sac_fly)
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
                    + pa_pitches(outcome, rng))
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


def _slate_worker(job):
    """Simulate one game in a worker process and return a COMPACT summary.

    MUST stay at module level and free of pandas — `multiprocessing` pickles
    the callable by qualified name, the same constraint `_bank_worker` and
    `homerunwidget`'s offline tools carry.

    The worker returns numbers, never the `GameResult` list: 25,000 simulated
    games is tens of megabytes to pickle back, which costs more than the
    simulation it was meant to parallelise. Ask the worker every question you
    need answered while the distribution is still in its own memory.
    """
    (label, home, away, n, seed, alpha, hfa, totals, handicaps) = job
    global LOG5_TAIL_ALPHA, HFA
    LOG5_TAIL_ALPHA, HFA = alpha, hfa      # fork inherits, spawn does not
    res = simulate_many(home, away, n=n, seed=seed)
    gt = game_totals(res)
    return {
        "label": label,
        "implied_line": implied_line(res),
        "mean_total": sum(gt) / len(gt),
        "p_home_win": p_home_win(res),
        "p_over": {L: price_over(gt, L) for L in (totals or ())},
        "p_home_cover": {H: p_home_covers(res, H) for H in (handicaps or ())},
    }


def simulate_slate(jobs: Sequence[tuple], n_sims: int = 20000,
                   seed: int = 17, workers: Optional[int] = None
                   ) -> List[dict]:
    """Simulate a whole slate across processes.

    `jobs` is [(label, home_side, away_side, totals, handicaps), ...] where the
    last two are the market lines to price. Games are independent and the sim
    is pure-Python CPU work, so this is processes rather than threads — the
    GIL makes threads worthless here.
    """
    if not jobs:
        return []
    full = [(lbl, h, a, n_sims, seed + i, LOG5_TAIL_ALPHA, HFA, t, hc)
            for i, (lbl, h, a, t, hc) in enumerate(jobs)]
    workers = workers or max(1, min(len(full), (os.cpu_count() or 4) - 2))
    if workers == 1:
        return [_slate_worker(j) for j in full]
    with multiprocessing.Pool(workers) as pool:
        return list(pool.imap(_slate_worker, full))


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


SAVE_DIR = Path(__file__).resolve().parent / "savedata"

# Weight of a season relative to the most recent one, halving each year back.
# One season is ~600 PA, so this is the season-level analogue of the ~500-PA
# half-life in arXiv:2511.17733.
SEASON_HALF_LIFE = 1.0

# Which seasons feed each side's blend. Empty means "every board on disk",
# which is the shipped behaviour. These exist so a board ADDITION can be
# A/B'd against its own absence without moving files around — the slate
# harness captures every uppercase constant, so they travel to the pool.
RATE_SEASONS_BAT: Tuple[int, ...] = ()
RATE_SEASONS_PIT: Tuple[int, ...] = ()


def rate_seasons(side: str) -> Tuple[int, ...]:
    return RATE_SEASONS_BAT if side == "bat" else RATE_SEASONS_PIT


# Whether each side blends OLDER seasons at all. `RATE_SEASONS_*` cannot
# express this: it is an absolute list, so switching the blend off for a
# backtest that replays 2025 would need a different value from one that
# replays 2026, and an A/B arm is one constant for every season it runs.
#
# **The blend has never been scored.** It ships on for both sides and it is
# not obviously free — an older season is a different player, re-expressed in
# this season's run environment by `rebase_to_season`, and the decay is a
# chosen half-life rather than a fitted one. Section 5b recorded the hitter
# side as switched OFF for want of boards; the boards arrived on 2026-08-16/18
# and nobody has asked what having them is worth.
USE_SEASON_BLEND_BAT = True
USE_SEASON_BLEND_PIT = True


def use_season_blend(side: str) -> bool:
    return USE_SEASON_BLEND_PIT if side == "pit" else USE_SEASON_BLEND_BAT


# League BABIP by batted-ball type, used ONLY to split outs in play into
# ground and air. Ground balls and line drives convert to outs at very
# different rates, so splitting by raw batted-ball share would put far too
# many outs on the line-drive side.
BABIP_GB = 0.239
BABIP_FB = 0.128    # includes infield flies, which are near-automatic outs
BABIP_LD = 0.630

# Extra-base mix of NON-HOME-RUN hits, league-wide. The pitching board carries
# only H and HR, so doubles and triples have to be imputed there — but the
# BATTING board carries the real 1B/2B/3B split, which makes it ground truth
# for exactly this constant. Measured off fg_bat_2026: of 0.1858 non-HR hits
# per PA, 76.0% singles / 22.1% doubles / 1.94% triples. Re-derive with
# `python mlb_sim.py rates`, which prints both baselines side by side; if the two
# 1B/2B rows drift apart, this pair is what has gone stale.
LG_XB_SHARE_2B = 0.221
LG_XB_SHARE_3B = 0.0194
LG_AIR_SHARE = 0.55     # league air share of balls in play, the pivot below


def _num(row: dict, key: str, default: float = 0.0) -> float:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else default


def _innings(row: dict, key: str = "IP", default: float = 0.0) -> float:
    """Innings off the board, which are written in OUTS notation.

    **`65.2` is 65 and TWO THIRDS, not 65.2.** Verified on the 2026 pitching
    board: the fractional part of `IP` takes only three values — `.0` (315
    rows), `.1` (239) and `.2` (246), and nothing else. A uniform decimal
    would put ~20% of rows on each of ten values, so this is unambiguous.

    Read as a plain float the number is short by up to 0.467 innings per
    pitcher, always in the same direction. Both consumers are in the
    start-length path and they COMPOUND: `ip_per_outing` comes out low, which
    under-nets the relief innings in `start_bf_estimate`, which inflates the
    implied start. Small, systematic, and free to fix.
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
    air_bip = max(fb - hr, 0.0) + ld
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


def projected_league_baseline(board: Sequence[dict], side: str,
                              prior_board: Optional[Sequence[dict]]
                              ) -> List[float]:
    """The FULL-season league environment, projected from a partial board.

    **Season-to-date is the wrong target, and on an as-of board it is wrong by
    a lot.** Measured over 2026's weekly cutoffs, the board's on-base is
    accurate throughout (-0.4% to +1.1% of the full season) but its HOME-RUN
    rate reads **-15.4% at 7 April**, -13.2% a week later, converging only by
    July. That is the real cold-weather effect — and the engine already prices
    temperature in `weather_tilt`, centred on each park's own mean. Feeding it
    an April-depressed baseline as well charges the cold TWICE, which is the
    same double-count as uncentred fatigue and the uncentred park term.

    It also does far more damage than one term's worth, because
    `rebase_to_season` maps every player's 2024 and 2025 evidence onto this
    baseline: the estimator's error is multiplied across the whole rate layer.

    So the quantity wanted is the full season's environment. Having observed a
    fraction `f` of it, the rest is unobserved and its best leakage-free
    estimate is the season before:

        baseline = f * observed + (1 - f) * prior season

    `f` is measured as playing time per club against the prior season's, so it
    needs no calendar and no free parameter, and at f = 1 it reduces exactly to
    the season-to-date behaviour.

    Chasing season-to-date is not merely noisy, it is worse than a constant:
    over the same 20 cutoffs, corr(season-to-date, the runs actually scored in
    the week each cutoff priced) is **-0.43**, and a flat season constant beats
    it on MAE (0.505 against 0.561).
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


def season_weights(seasons: Sequence[int],
                   half_life: float = SEASON_HALF_LIFE) -> Dict[int, float]:
    """Recency weight per season, 1.0 on the most recent."""
    if not seasons:
        return {}
    newest = max(seasons)
    decay = math.log(2.0) / half_life
    return {s: math.exp(-decay * (newest - s)) for s in seasons}


# ---------------------------------------------------------------------------
# The shrinkage prior depends on PLAYING TIME — sim_state.md 5.9
# ---------------------------------------------------------------------------
# `shrink_rates` pulls every player toward the league mean, which assumes the
# player is a random draw from the league. **He is not. Playing time in MLB is
# selected on performance**, so the population a fringe player belongs to is
# far from league average, and the direction is opposite on the two sides.
# Read straight off the 2026 boards, on-base per PA against league:
#
#     playing time   ~15    ~95/144   ~250     ~490
#     pitchers      +0.078   +0.026   -0.006   -0.009      (allowed)
#     hitters       -0.087   -0.023   -0.012   +0.019
#
# Shrinking a 40-batter reliever toward league says he is a 0.331 arm; arms
# with that little work threw 0.368. The error is 9.7% of league innings, and
# because those innings are RELIEF innings it lands almost entirely after the
# 5th — which is where the sim's per-inning deficit was.
#
# **This is not a claim that shrinkage is wrong.** A regressed estimate is the
# right FORECAST for one player; what is wrong is the target it regresses to.
# Using a playing-time prior keeps the estimator and fixes the population, and
# it leaves well-sampled players untouched (their bins sit on ~0.000).
#
# Read off the board rather than fitted, the same choice `deployment_score`
# makes: no smooth curve reproduces a relationship that crosses zero because
# good players accumulate playing time.
#
# **PITCHERS ONLY, and that is not "apply it where it helps".** The engine
# selects the two sides differently, and the hitter side already carries the
# selection structurally:
#
#   * the pen IS the population. A reliever with 40 batters faced who comes in
#     is exactly the fringe arm the low bin describes, so that is the right
#     thing to regress him toward.
#   * a hitter arrives through the POSTED LINEUP, a second and strong
#     selection the sim already applies. A 150-PA hitter who is starting
#     tonight is not a random draw from "hitters with 150 PA" — he is the
#     subset good enough to start. Applying the population prior on top counts
#     the selection twice, which is the fatigue opening penalty again (5.4).
#
# Measured on the real slate: pitcher prior **+0.047** runs a game, batter
# prior **-0.178**. The sign is the tell — a correction that is right for a
# population and wrong for a sample already selected on the same axis.
PRIOR_SIDES = ("pit",)

# --- the HITTER playing-time prior -----------------------------------------
# **OFF by default and only meaningful CENTRED.** 4e localises the whole
# heavy-favourite gap to games where the underdog's posted nine is thin: the
# gap is +1.271 runs (t +3.65) at a market price of 0.65+, against +0.216
# (t +0.60) when the underdog runs an established lineup, and the split holds
# at every threshold. `PRIOR_SIDES = ("pit",)` leaves those hitters shrunk
# toward LEAGUE AVERAGE with nothing pulling them to replacement level.
#
# The naive flip — adding "bat" to `PRIOR_SIDES` — was measured before and
# rejected: +7.2 points on PHI and -0.3 on NYY, its value tracking lineup
# ASYMMETRY while its suppression was a constant level shift.
#
# **The curve is already centred on the wrong population, and that is the
# whole bug** (trap 7, sixth instance). Its bins are PA-weighted, so the
# across-bin average target equals league — but the prior is not applied as an
# average, it is applied as a SHRINKAGE TARGET, and the weight on that target
# is `1 - n/(n+stab)`. Fringe hitters carry a target ~25% below league AND the
# heaviest weight toward it; regulars carry +6% and almost no weight. Summed
# over a real lineup that is a net downward push, which is why turning it on
# reads as a level shift.
#
# `USE_BAT_PRIOR` therefore ships with `bat_prior_offset`, which re-centres on
# the population the prior is actually applied to, weighting each board player
# by his playing time TIMES the weight the shrinkage will really give the
# target. After it, the prior can only redistribute between thin and
# established hitters; it cannot move the league's run level.
USE_BAT_PRIOR = False
BAT_PRIOR_CENTRED = True
BAT_PRIOR_CENTRE_ITERS = 40

# Linear weights, for centring the hitter prior on RUN VALUE rather than on
# on-base. `offence_tilt` moves mass between hits and outs PROPORTIONALLY, so
# it preserves the hit mix — but the curve does not: a fringe hitter is weaker
# in slugging as well as in on-base. Centring the on-base rate alone leaves
# -0.066 runs a game on the table, which is a correction that measures as
# perfect on the quantity it was solved for and is wrong on the one that
# matters. `WOBA_W` supplies the hit weights this file already uses.
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

    **The prior curve must be indexed by a SHARE of playing time, not a
    count.** The curve encodes "MLB gives playing time to good players", which
    is a rate, and a raw count silently carries how much SEASON the board
    covers. On a season-final board 57 TBF is a fringe arm; ten days into a
    season it is a workhorse starter with two starts, and the curve built off
    that board duly reads its top bin as 5% BETTER than league — so every
    backtested starter was regressed toward a prior that made him good. That
    was worth ~0.9 runs a game on the early cutoffs.

    Dividing by this makes the index season-length invariant, and on a
    full-season board it is a single constant divisor, so the bins, the
    values and the log interpolation are all unchanged.
    """
    tot = 0.0
    for row in rows or []:
        tot += outcome_counts(row, side)[1]
    return (tot / N_CLUBS) if tot > 0 else 0.0


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
    return _prior_bins(rows)


def prior_curve(side: str, season: int = 2026, save_dir: Path = SAVE_DIR,
                rows_override: Optional[List[dict]] = None
                ) -> List[Tuple[float, List[float]]]:
    """The playing-time prior's SHAPE, taken from a COMPLETED prior season.

    **A partial board cannot produce this curve, and it fails in the direction
    that flatters the model.** The curve encodes "MLB gives playing time to
    good players", which is a season-long selection effect. Ten days in it has
    not happened yet, so cumulative playing time separates relievers from
    starters instead — and starters allow more baserunners per PA. Built off
    the 2026-04-07 board the curve reads its TOP bin 5% BETTER than league and
    its bottom bin 12% better, an exact inversion of the full-season +27.6% /
    -2.2%. Every backtested starter was then regressed toward a prior that
    made him good: ~0.9 runs a game on the early cutoffs, and INVISIBLE to the
    in-sample harness, which never builds a partial board.

    The shape is persistent, which is what makes this fix legitimate rather
    than a convenience — measured across 2024/2025/2026, per share:

        pit  fringe (0.002)   +21.8% / +30.1% / +27.6%
             workhorse (0.09)  -2.0% /  -2.3% /  -2.2%
        bat  fringe (0.002)   -25.5% / -27.6% / -25.3%
             regular (0.12)    +6.4% /  +7.0% /  +6.0%

    Only the run ENVIRONMENT moves between seasons, and `rebase_to_season`
    maps each bin onto the target league, which is taken from `rows_override`
    when the as-of path supplies one. So the shape is leakage-free by
    construction and the level is current.

    Falls back to the board itself when no earlier season is on disk.
    """
    board = (rows_override if rows_override is not None
             else load_board(side, season, save_dir) or [])
    earlier = [s for s in available_seasons(side, save_dir) if s < season]
    key = (side, int(season), str(save_dir))

    shape = _PRIOR_CURVE.get(key)
    if shape is None:
        src = (load_board(side, max(earlier), save_dir) if earlier else board)
        shape = _curve_from_board(src or [], side)
        src_league = league_baseline(src, side) if src else None
        _PRIOR_CURVE[key] = shape
        _PRIOR_LEAGUE[key] = src_league
    src_league = _PRIOR_LEAGUE.get(key)

    if not shape or src_league is None or not board:
        return shape
    target = league_baseline(board, side)
    return [(pt, _normalize(rebase_to_season(v, src_league, target)))
            for pt, v in shape]


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


def playing_time_prior(share: float, side: str, league: Sequence[float],
                       season: int = 2026, save_dir: Path = SAVE_DIR,
                       rows_override: Optional[List[dict]] = None,
                       centre_tilt: Optional[float] = None
                       ) -> List[float]:
    """The outcome vector a player with this much playing time comes from.

    `share` is his playing time as a fraction of ONE club's — see
    `board_pa_per_club`. It is deliberately NOT the summed multi-season PA
    that drives shrinkage: how much evidence we have and what role he fills
    are different questions, and a part-timer with three seasons on the board
    is still a part-timer.

    Falls back to `league` when the board is unavailable, so the module still
    runs on synthetic sides, and for any side the prior does not apply to
    (`PRIOR_SIDES`, plus "bat" when `USE_BAT_PRIOR` is on).
    """
    if side not in _prior_sides():
        return list(league)
    curve = prior_curve(side, season, save_dir, rows_override)
    if not curve:
        return list(league)
    got = _curve_at(curve, share)
    if side == "bat" and BAT_PRIOR_CENTRED and centre_tilt:
        got = offence_tilt(got, centre_tilt)
    return got





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


def solve_bat_prior_tilt(pop: Sequence[Tuple[float, Sequence[float]]],
                         curve: Sequence[Tuple[float, List[float]]],
                         league: Sequence[float],
                         stab: Sequence[float]) -> float:
    """The tilt that stops the hitter prior from moving the league's run level.

    **Centre on the population you actually apply it to** — trap 7, and this
    one took three wrong answers to get right, each of which measured as a
    clean success on the quantity it was solved for:

    1. The curve's own bins are PA-weighted, so its across-bin mean equals
       league. That is the wrong invariant: what reaches a rate is
       `shrink_rates(counts, prior)`, and the weight on the prior is
       `1 - n/(n+stab)` — largest exactly for the fringe hitters whose target
       sits furthest below league.
    2. Centring per OUTCOME is over-determined. Nine weighted means each using
       their own stabiliser do not form a probability vector and cannot all be
       matched to a league vector summing to one; the additive form DIVERGES
       under the renormalisation it forces, and the multiplicative one leaves a
       uniform ~0.5% scale. The defect is a LEVEL shift, the level is one
       dimension, and `offence_tilt` is the axis HFA, the form draw, weather
       and the park term all already move along.
    3. Centring ON-BASE is not centring RUNS. `offence_tilt` moves mass between
       hits and outs proportionally and so preserves the hit MIX, but the curve
       makes a fringe hitter weaker in slugging too. On-base came out exactly
       neutral while run value was still -0.066 runs a game.

    And the population itself is the fourth: `pop` must carry the BLENDED
    multi-season counts the engine really shrinks against, not the newest
    board's. On an April as-of board a regular has ~50 PA there and ~700
    blended, so a solver reading the board alone thinks the prior carries 0.8
    of the weight for everyone when it really carries 0.22 for the established
    and 0.8 for the callups — the asymmetry it exists to cancel. Solved off the
    board it cost **-0.79 runs a game** on the April cutoffs and -0.25 in
    August, and NONE of it was visible on the full-season board, where the two
    populations nearly agree.

    `pop` is [(playing-time share, blended counts)].
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


def rebase_to_season(counts: Sequence[float], season_league: Sequence[float],
                  target_league: Sequence[float]) -> List[float]:
    """Re-express one season's outcome counts in ANOTHER season's environment.

    **Blending raw counts across seasons imports their run environments, and
    they are not the same environment.** Measured on the pitching boards, league
    on-base per PA runs 0.31106 in 2024, 0.31398 in 2025 and 0.31680 in 2026 —
    so a pitcher who was exactly league-average in 2024 carries a line that
    reads 1.8% BETTER than league when it is shrunk toward 2026. He is not
    better; the league was.

    The asymmetry made it worse than a wash. Pitchers blend 2024-26 at
    0.25/0.5/1.0 while the hitters only have 2026 on disk, so the bias landed
    on one side of every matchup: the arms the sim used came out 0.79%
    (starters) too good, and the model gave back ~0.11 runs a game it should
    have scored.

    Scaling by the ratio of league rates maps a season-average player onto a
    target-season-average player exactly, and renormalising back to the
    original PA preserves SAMPLE SIZE — which is what drives shrinkage and
    must not be invented or destroyed by an era adjustment.
    """
    n = sum(counts)
    if n <= 0:
        return list(counts)
    out = [counts[i] * (target_league[i] / season_league[i])
           if season_league[i] > 0 else counts[i]
           for i in range(N_OUTCOMES)]
    scale = n / sum(out) if sum(out) > 0 else 1.0
    return [c * scale for c in out]


def blend_seasons(by_season: Dict[int, Tuple[List[float], float]],
                  half_life: float = SEASON_HALF_LIFE,
                  newest: Optional[int] = None
                  ) -> Tuple[List[float], float]:
    """Recency-weighted combination of one player's per-season counts.

    Returns (blended counts, effective PA). The effective PA is weighted too,
    so a player whose only recent sample is small stays properly shrunk —
    crediting him the raw multi-season total would treat three-year-old
    evidence as though it were current.

    **`newest` must be the newest season on the BOARD, not the newest this
    player has.** Without it `season_weights` anchors on his own last season,
    so a player absent from the current board has his older years re-weighted
    as though they were current — a 2025 line counted at 1.0 instead of 0.5.
    It inflated the effective PA of everyone who did not play this year, and
    it is worse on an as-of board, where "not on the board yet" is the normal
    state in April rather than a retirement.
    """
    seasons = list(by_season)
    if newest is not None and newest not in by_season:
        seasons.append(newest)
    w = season_weights(seasons, half_life)
    counts = [0.0] * N_OUTCOMES
    pa = 0.0
    for season, (c, p) in by_season.items():
        wt = w[season]
        for i in range(N_OUTCOMES):
            counts[i] += c[i] * wt
        pa += p * wt
    return counts, pa


# ---------------------------------------------------------------------------
# PITCH-CHARACTERISTIC repeatability — sim_state.md 0.1 Objective 1
# ---------------------------------------------------------------------------
# A pitcher's own line is a far worse estimate of him than a hitter's is of
# himself: HR stabilises at 634 batters faced against a hitter's 244, singles
# at 749 against 279 (`STABILIZE_PA_PIT`). And a starter faces ~23 of the ~38
# batters in a game, so that error carries more of the run model than the
# hitter side does.
#
# **The seam that failed twice is NOT this one, and that is the whole
# hypothesis.** Sections 3d.6 and 3d.7 made a CONTACT estimate the shrinkage
# target for observed counts, and both were built from the same batted balls,
# so a hitter's own data entered twice and partly undid the stabilisation gain.
# Stuff+, Location+ and PitchingBot's stuff/command are computed from PITCH
# CHARACTERISTICS — velocity, movement, release point, location — which are
# disjoint from the outcomes being shrunk. Nothing here is derived from a
# result, which is why xERA, SIERA and xFIP are deliberately NOT features: they
# are outcome statistics wearing expected-stat clothes and would re-introduce
# exactly the double count.
#
# BallparkPal states the intended use directly: *"The Pitch Model plays an
# important role in determining the REPEATABILITY of a pitcher's outcomes based
# on how effective his pitches appear."* Repeatability is a WEIGHT on how far
# to trust what he has done, not a target to shrink him toward — so this is a
# two-source empirical Bayes:
#
#     estimate = w * observed + (1 - w) * (playing-time prior + stuff delta)
#     w        = n / (n + M_eff)        M_eff = M / (1 - rho2)
#
# `M` is the measured stabilisation point, which is `sigma^2 / var_true`. A
# prior that already explains `rho2` of the true talent leaves only
# `var_true * (1 - rho2)` for the observations to resolve, so the SAME
# arithmetic that produced M produces M_eff — a better prior earns MORE
# shrinkage toward itself, not less. At rho2 = 0 this reduces exactly to the
# shipped behaviour, which is what makes it safe to leave on a pitcher the
# model has no stuff data for.
STUFF_FEATURES: Tuple[str, ...] = (
    "sp_stuff",        # FanGraphs Stuff+
    "sp_location",     # FanGraphs Location+
    "pb_stuff",        # PitchingBot stuff
    "pb_command",      # PitchingBot command
    "FBv",             # fastball velocity
)

# --- the ARSENAL block: spin, break and velocity separation ----------------
# The board carries release spin PER PITCH TYPE as `pfxsp<TYPE>`, alongside
# per-type velocity (`pfxv<TYPE>`), horizontal and vertical break
# (`pfx<TYPE>-X` / `-Z`) and usage (`pfx<TYPE>%`) — 145 columns of it, none of
# which anything read. `pfxspFA` averages 2,290 rpm across 436 arms with 100+
# TBF, which is the league fastball number, so these are real release spin and
# not an index.
#
# Every one of them is a PITCH CHARACTERISTIC, so the disjointness argument
# that makes this whole section work covers them unchanged. Stuff+ is a model
# built ON these inputs; carrying the inputs as well lets the estimate see a
# pitcher his particular model happens to price badly.
#
# **They cannot go in as 17 raw columns.** A pitcher with no curveball has a
# null there, and `_stuff_feats` is all-or-nothing on missing values by design
# — imputing a mean would hand an arm we know nothing about a confident
# looking number. So they are collapsed into a DENSE, usage-weighted block:
# one number per pitch FAMILY rather than per pitch type, and a family he does
# not throw falls back to his OWN arsenal average rather than the
# population's. "His breaking-ball spin, or his general spin if he has none"
# is a fact about him; the league mean is not.
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
# **There is deliberately no DRIFT feature here.** The obvious next idea is a
# delta — his trailing-window fastball velocity and spin MINUS his season
# figures, so an arm who has lost 1.5 mph is not priced as the pitcher his
# season line describes. It was built and measured and it is null: the
# correlation with the residual of his future rate flips SIGN between seasons
# at every outcome that moves (BB +0.036 / -0.148, HR -0.039 / +0.149). The
# power arithmetic says why there is little to find — fastball velocity varies
# 2.29 mph BETWEEN pitchers and drifts 0.47 mph within a season on a 250-pitch
# window, so drift is 4.2% of the cross-sectional variance. A flag on the ~4%
# of arms who move a full mph is a different test and this one does not refute
# it, but there is no population-scale effect. Adding an unused constant for it
# would be the same defect section 3d.9 exists to complain about.
# **SHIPPED True.** It is 4.6x the five-column version on the closing line
# (paired t +1.14 against +0.25) and the run-value proxy said it would be a
# WASH — so the proxy was the wrong instrument, not the feature. Turning this
# off requires putting the five-column STUFF_RELIABILITY back.
STUFF_USE_ARSENAL = True


def _arsenal_block(row: dict) -> Optional[List[float]]:
    """The dense arsenal features for one board row, or None if unusable.

    Usage-weighted across the pitch types he actually throws, so a two-pitch
    reliever and a six-pitch starter produce comparable numbers.
    """
    use: Dict[str, float] = {}
    for fam, types in PITCH_FAMILIES.items():
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
# estimate explains — measured by `measure_stuff_reliability`, never assumed.
# Zero means "this prior knows nothing about this outcome", and at zero the
# estimator reduces exactly to the shipped one.
#
# Measured on two seasons independently, each scoring the rest of a pitcher's
# season from an as-of cutoff, with the model fit on strictly earlier seasons
# (2025 on 2024; 2026 on 2024-25):
#
#                 corr(own rate)   corr(stuff)     rho2
#   outcome        2025    2026    2025    2026   2025   2026   SHIPPED
#     K           +.623   +.632   +.554   +.599   .515   .667    .515
#     BB          +.387   +.372   +.450   +.390   .566   .424    .424
#     HBP         +.264   +.267   +.074   +.155   .024   .090    .024
#     GB_OUT      +.629   +.492   +.107   +.009   .018   .000    .000
#     AIR_OUT     +.627   +.490   +.414   +.437   .271   .361    .271
#     1B          +.410   +.242   +.242   +.274   .151   .316    .151
#     2B          +.307   +.245   +.360   +.377   .417   .630    .417
#     3B          +.338   +.213   +.287   +.340   .257   .570    .257
#     HR          +.241   +.170   +.257   +.237   .330   .374    .330
#
# **On BB, 2B, 3B and HR his stuff predicts his own future better than his own
# results do.** That is the SIERA thesis again from the other direction: the
# outcomes with the longest stabilisation points are exactly the ones where a
# pitch-characteristic estimate has the most to add.
#
# The MINIMUM of the two seasons ships, not the mean. Every demonstrated
# failure in this file has been over-trusting a new term (fatigue, the park
# term, the log5 tail, BMIELKE), so the conservative direction is the smaller
# rho2 — less movement away from what the rate layer already does.
#
# GB_OUT is zero on purpose. Stuff+ is trained on run value, not on batted-ball
# type, and the measurement says it has nothing to say about ground-ball outs
# on either season. Leaving it at the two-decimal noise floor would move the
# most common outcome in the vector on nothing.
# **These are the ARSENAL-fit values, because STUFF_USE_ARSENAL ships True.**
# The five-column table is kept in the comment above; the two constants must
# move together or `stuff_predict` raises on the feature width.
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

# **SHIPPED True 2026-08-16**, on the arsenal feature set only. Pooled over
# 3,877 leak-free games, paired on identical games and seeds: log-loss 0.68210
# -> 0.68138, paired t +1.14 against base, model-vs-market t -0.69 -> -0.24 —
# the closest to the closing line this engine has been. Same sign on BOTH
# seasons (+0.60 on 2025, +1.08 on 2026), which is the criterion every other
# candidate this session failed. Not significant on its own; shipped because it
# is consistent, cheap and directionally right on every metric at once.
#
# The five-column version is worth only t +0.25 — see STUFF_USE_ARSENAL.
USE_STUFF_PRIOR = True

# Batters faced a pitcher needs on the board before his stuff columns are used
# at all. Stuff+ over a handful of starts is itself an estimate; below this the
# feature noise swamps the signal it is meant to add.
STUFF_MIN_TBF = 40.0
# and the count at which the stuff delta is trusted half against nothing. A
# pitch-characteristic average stabilises far faster than any outcome — every
# pitch contributes to it, not every plate appearance — which is why this is a
# small number next to STABILIZE_PA_PIT.
STUFF_SHRINK_TBF = 100.0


# --- ROLLING arsenal: the same columns, over a trailing window -------------
# A season-to-date arsenal average hides the thing most worth knowing about a
# pitcher tonight: that his fastball is down 1.2 mph and 90 rpm since June.
# **And it is recoverable from the boards already on disk**, because every
# per-type column is a MEAN and the board carries the count it was taken over
# (`Pitches` times that type's usage share). Two cumulative snapshots
# therefore un-average into the window between them:
#
#     mean_window = (mean_2 * n_2 - mean_1 * n_1) / (n_2 - n_1)
#
# which is the same differencing trick `board_windows` uses on counts, applied
# to averages instead. No new source, no Savant fetch.
#
# This is NOT the same question section 3d.9 answered. That measured recency
# on OUTCOMES and found nothing for pitchers, for a reason that does not
# transfer: a pitcher's outcome sample is small, so discarding a third of its
# weight costs more than the staleness it removes. Pitch characteristics are
# measured on every PITCH — two orders of magnitude more evidence per unit of
# calendar — so a trailing window of them is barely noisier than the season.
STUFF_ROLLING_PITCHES = 0.0     # 0 = season to date; else the trailing window
# Below this many pitches of a TYPE inside the window, that type falls back to
# its season-to-date average rather than being computed from a handful.
STUFF_ROLLING_MIN_TYPE = 25.0


def _type_pitches(row: dict, pt: str) -> Optional[float]:
    """How many pitches of one type this cumulative board row was taken over."""
    total = row.get("Pitches")
    share = row.get(f"pfx{pt}%")
    if not isinstance(total, (int, float)) or not isinstance(
            share, (int, float)):
        return None
    return float(total) * float(share)


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
        n2, n1 = _type_pitches(now, pt), _type_pitches(earlier, pt)
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
        n2, n1 = _type_pitches(now, pt), _type_pitches(earlier, pt)
        if n2 is None or n1 is None:
            continue
        out[f"pfx{pt}%"] = max(n2 - n1, 0.0) / n_win_total
    out["Pitches"] = n_win_total
    return out


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


def fit_stuff_model(seasons: Sequence[int], save_dir: Path = SAVE_DIR,
                    min_tbf: float = 100.0) -> dict:
    """Per-outcome linear model: stuff columns -> rate ABOVE the playing-time prior.

    The target is the RESIDUAL against `playing_time_prior`, not against
    league, so the model cannot take credit for what the rate layer already
    knows. Relievers have better stuff than starters and also less playing
    time; regressing on the raw deviation from league would let the same fact
    be paid for twice — the recorded double-count trap, one axis over.

    Rows are weighted by sqrt(TBF): a 30-batter line is a noisy target, and
    unweighted least squares would let a few of them set the slope.
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
    fits = _lstsq(aw, [[r[1][i] * ww for r, ww in zip(rows, w)]
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
    key = (int(season), str(save_dir))
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
    return rolling_board("pit", season, board, STUFF_ROLLING_PITCHES,
                         as_of, save_dir)


def stuff_deltas(board: Sequence[dict], model: Optional[dict],
                 min_tbf: Optional[float] = None) -> Dict[int, List[float]]:
    """{pid: centred, sample-shrunk rate delta} for every arm the model can see.

    **Centred on the population it is applied to**, weighted by the evidence
    behind each row. The model is fit on a completed season and applied to a
    partial one, so its intercept is not this board's intercept; leaving it
    uncentred would move the whole league's run level by whatever the two
    populations differ by. That is the fifth instance of this trap in the file
    (fatigue, the park term, the platoon gap, the fatigue opening penalty,
    BMIELKE), so it is done by construction rather than checked afterwards.

    Shrunk by TBF as well: Stuff+ on 50 batters faced is itself an estimate.
    **The shrink is applied BEFORE the centring, not after.** The two do not
    commute: the shrink weight rises with playing time and so does the delta
    (starters and relievers differ on both), so centring first and shrinking
    second puts a correlation back in and leaves the applied population 0.0007
    of a walk per PA off league — small, and exactly the level bias this
    centring exists to prevent.
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


def measure_stuff_reliability(season: int = 2026, save_dir: Path = SAVE_DIR,
                              min_pre: float = 100.0, min_post: float = 100.0
                              ) -> dict:
    """How much of a pitcher's PREDICTABLE variance his stuff explains.

    Two covariances, both against what he did AFTER the cutoff, so neither
    shares a sampling error with its predictor:

        var_pred = cov(rate before, rate after)     -- what is predictable at
                   all, talent plus whatever recurs (park, defence, catcher,
                   role). Section 5.10's own argument for why pure talent is
                   the wrong target.
        rho2     = cov(delta, rate after)^2 / (var(delta) * var_pred)

    `rho2` is the share of that predictable variance the stuff delta accounts
    for, which is exactly what `stuff_stabilize` needs. It is capped at 0 from
    below: a negative covariance means the model has nothing for that outcome,
    and the honest encoding of that is zero rather than a sign flip.
    """
    rows = stuff_future_rows(season, save_dir, min_pre, min_post)
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
            stuff_source_board(
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
        vp = _cov(pre, post)
        vd = statistics.pvariance(dl)
        cdp = _cov(dl, post)
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


# ---------------------------------------------------------------------------
# WITHIN-SEASON RECENCY — sim_state.md 0.1 Objective 2
# ---------------------------------------------------------------------------
# `recency_weights` and `weighted_counts` have been in section 3 since the
# module was written, and the docstring cites arXiv:2511.17733 for using them
# "rather than season totals" — **but nothing called `weighted_counts`.** The
# rate layer ran `outcome_counts` over FanGraphs season totals, which carry no
# ordering at all, so the only recency in the engine was `SEASON_HALF_LIFE`
# ACROSS seasons. A documented capability the code does not have is worse than
# an absent one.
#
# The ordering the board will not give up is recoverable from the AS-OF boards
# already on disk: they are cumulative, so DIFFERENCING consecutive cutoffs
# yields the counts inside each window. That is the same sequence
# `weighted_counts` wants, at weekly rather than per-PA granularity, and it
# costs no new fetch.
#
# Ages are measured in PLATE APPEARANCES, not days, so the half-life means the
# same thing here as in `recency_weights` — a reliever's April is much less
# stale than a starter's by the calendar, and the calendar is the wrong clock.
#
# **The residual was measured before any of this was wired, per section 0.1's
# own instruction, and it does NOT apply to both sides.** Predicting the rest
# of a player's season from an as-of cutoff, each variant shrunk at its OWN
# effective sample size (a recency-weighted estimate has genuinely seen less
# and shrinkage has to be told so), paired and CLUSTERED BY PLAYER — one arm
# contributes a row at every cutoff and those rows share his talent, his park
# and most of his future window, so treating them as independent would shrink
# the standard error by roughly sqrt(cutoffs) and turn a coin flip into a
# finding:
#
#   pooled improvement in weighted squared error, + = recency better
#     half-life      150       250       500      1000
#     PITCHERS     -0.18     +0.12     +0.39     +0.54     <- and the two
#                                                             seasons have
#                                                             OPPOSITE signs
#     HITTERS      +1.92     +2.53     +3.09     +3.40     <- same sign both
#
# So recency ships for HITTERS ONLY. For pitchers 2025 says -0.84 and 2026
# says +1.66 at the same half-life, which is what noise looks like, and the
# reason is not mysterious: a pitcher's line is a smaller sample to begin with
# (`STABILIZE_PA_PIT` is 2-6x the hitter table), so discarding a third of its
# effective weight costs more than the staleness it removes.
#
# **The half-life is NOT fitted here.** The pooled t rises monotonically as the
# half-life lengthens while the effect SIZE falls, so picking either end off
# this data would be fitting it. 500 PA is the value already in
# `recency_weights` from arXiv:2511.17733 — chosen before the measurement, and
# it sits in the middle of the range where both seasons agree in sign.
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

    Each window is one cached as-of board minus the one before it, and
    `terminal` closes the sequence — the board the caller is actually using,
    passed in rather than re-derived. That is deliberate: the terminal board
    is the newest and most important window, and looking it up by date would
    silently drop it whenever no board happened to be cached on that day,
    leaving a rate layer that quietly ignored the last three weeks.

    `as_of` bounds which cutoffs are eligible; cutoffs on or after it are
    dropped, which is the backtest's one rule applied here too.
    """
    cuts = [c for c in available_asof_cutoffs(season, save_dir)
            if as_of is None or c < as_of]
    # **A cutoff board NEWER than the terminal one is not a window, it is a
    # contradiction.** On the live path the terminal board is whatever
    # `fg_bat_<season>.json` was last refreshed to, and if that is older than
    # the newest cached cutoff the differencing would hand the rate layer a
    # season the rest of it has never seen — silently, because the sequence
    # still looks well formed. Drop those cutoffs instead.
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


def measure_recency(side: str = "pit", season: int = 2026,
                    half_lives: Sequence[float] = (250.0, 500.0, 1000.0),
                    save_dir: Path = SAVE_DIR,
                    min_pre: float = 100.0, min_post: float = 100.0,
                    trim: int = 4) -> dict:
    """Does weighting a player's season by recency predict his FUTURE better?

    **The residual, measured before anything is built** — §0.1's own
    instruction, because part of what recency would capture is lineup turnover
    and bullpen state, which a plate-appearance simulator already carries
    structurally.

    Scored on the run-value summary against what he actually did after the
    cutoff, weighted by the batters he faced, and reported BOTH ways:

    * `raw` — the unshrunk rate. This is the comparison that shows the
      mechanism, and it is unfair to recency by construction: a weighted
      estimate has genuinely seen less (the effective sample is ~60% of the
      raw one at a 150-PA half-life), so some of any RMSE loss is just noise.
    * `shrunk` — each variant regressed toward the same league prior at ITS
      OWN effective sample size, which is what the rate layer would actually
      run and which prices that noise instead of ignoring it. **This is the
      one to read.** If recency is a real improvement it has to survive
      paying for its own smaller sample.
    """
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


def _cov(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 2:
        return 0.0
    ma, mb = statistics.mean(a), statistics.mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a)


def score_stuff_prior(season: int = 2026, save_dir: Path = SAVE_DIR,
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
    rows = stuff_future_rows(season, save_dir, min_pre, min_post)
    if not rows:
        return {"n": 0, "season": season}
    by_cut: Dict[str, List[dict]] = {}
    for r in rows:
        by_cut.setdefault(r["cutoff"], []).append(r)

    global USE_STUFF_PRIOR, STUFF_RELIABILITY
    was_use, was_rho = USE_STUFF_PRIOR, STUFF_RELIABILITY
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


# ---------------------------------------------------------------------------
# Board loading
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# AS-OF boards — the seam a leakage-free backtest needs
# ---------------------------------------------------------------------------
# A season board on disk is a season-FINAL snapshot: using it to project a May
# game feeds the model the rest of that season, including the game itself. That
# is why `mlb_sim.py clv` over completed games has only ever been a plumbing
# check.
#
# **FanGraphs will serve the board as of a date**, which was previously
# recorded here as impossible. `month=1000` with `startdate`/`enddate` returns a
# genuine partial-season leaderboard — verified: Pete Crow-Armstrong reads 546
# PA full season, 374 through 30 June, and the April board is topped by Judge
# instead.
#
# Two differences from the full-season board, both checked rather than assumed:
#   * the pitching response omits `1B`/`2B`/`3B` — and so does the full-season
#     one, which is why `outcome_counts` already derives them from H, HR and the
#     batted-ball mix. No regression.
#   * the batting response omits `XBR`. That one is real: `runner_advance_rates`
#     weights XBR 0.6 against Spd 0.4, so an as-of run falls back to speed alone
#     for taking the extra base. `_num` returns 0.0, which is XBR's neutral
#     value, so it degrades rather than breaking.
#
# `/api/leaders` 403s a plain request — Cloudflare — so this needs the same
# headless-Firefox transport `EffortMLB` uses, and selenium is imported LAZILY
# so the module stays importable without it.
ASOF_DIR = SAVE_DIR / "asof"

FG_ASOF_PATH = ("/api/leaders/major-league/data?age=&pos=all&stats={stats}"
                "&lg=all&qual=0&season={season}&season1={season}&ind=0&type=8"
                "&pageitems=5000&pagenum=1"
                "&month=1000&startdate={start}&enddate={end}")


def asof_board_path(side: str, season: int, as_of: str,
                    save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / "asof" / f"fg_{side}_{season}_{as_of}.json"


# The same board WITHOUT a date window. `month=0` is the full season, which is
# what `fg_{bat,pit}_<season>.json` on disk are.
FG_SEASON_PATH = ("/api/leaders/major-league/data?age=&pos=all&stats={stats}"
                  "&lg=all&qual=0&season={season}&season1={season}&ind=0"
                  "&type=8&pageitems=5000&pagenum=1&month=0")


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


def fetch_boards_asof(dates: Sequence[str], season: int = 2026,
                      sides: Sequence[str] = ("bat", "pit"),
                      save_dir: Path = SAVE_DIR,
                      force: bool = False, verbose: bool = True
                      ) -> Dict[tuple, int]:
    """Fetch and cache season-to-date boards for each cutoff in `dates`."""
    todo = [(side, d) for d in dates for side in sides
            if force or not asof_board_path(side, season, d, save_dir).exists()]
    got: Dict[tuple, int] = {}
    if not todo:
        if verbose:
            print("[asof] all cutoffs already cached")
        return got

    with _fg_driver() as driver:
        for side, as_of in todo:
            path = FG_ASOF_PATH.format(stats=side, season=season,
                                       start=f"{season}-01-01", end=as_of)
            rows = _fg_rows(driver, path, f"{side} {as_of}", verbose)
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


def fetch_season_boards(seasons: Sequence[int],
                        sides: Sequence[str] = ("bat", "pit"),
                        save_dir: Path = SAVE_DIR,
                        force: bool = False, verbose: bool = True
                        ) -> Dict[tuple, int]:
    """Fetch and cache FULL-SEASON boards — `savedata/fg_<side>_<season>.json`.

    Hitters carried 2026 only while pitchers carried 2024-26, so every matchup
    blended three years of arm against one year of bat. `build_rates` picks up
    whatever seasons are on disk, so this is a data fetch and not a model
    change — but it moves every hitter's effective sample, so A/B it on the
    slate rather than assuming it helps.
    """
    todo = [(side, s) for s in seasons for side in sides
            if force or not (Path(save_dir) / f"fg_{side}_{s}.json").exists()]
    got: Dict[tuple, int] = {}
    if not todo:
        if verbose:
            print("[boards] all seasons already cached")
        return got

    with _fg_driver() as driver:
        for side, season in todo:
            path = FG_SEASON_PATH.format(stats=side, season=season)
            rows = _fg_rows(driver, path, f"{side} {season}", verbose)
            if rows is None:
                continue
            dest = Path(save_dir) / f"fg_{side}_{season}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w") as fh:
                json.dump(rows, fh)
            got[(side, season)] = len(rows)
            _BOARDS.pop((side, int(season), str(save_dir)), None)
            if verbose:
                print(f"[boards] {side} {season}: {len(rows)} rows")
    return got


_ASOF_BOARDS: Dict[tuple, Optional[List[dict]]] = {}


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
    path = asof_board_path(side, season, as_of, save_dir)
    data = None
    if path.exists():
        with open(path) as fh:
            got = json.load(fh)
        data = got if isinstance(got, list) else None
    _ASOF_BOARDS[key] = data
    return data


def build_rates_asof(side: str, season: int, as_of: str,
                     half_life: float = SEASON_HALF_LIFE,
                     save_dir: Path = SAVE_DIR
                     ) -> Tuple[Dict[int, dict], List[float]]:
    """`build_rates` with the newest season TRUNCATED at `as_of`.

    Older seasons are used whole, which is correct — they finished before the
    game being predicted — and `rebase_to_season` maps them onto the AS-OF
    environment, because the target league baseline is computed from the
    truncated board like everything else.
    """
    rows = load_board_asof(side, season, as_of, save_dir)
    if rows is None:
        raise FileNotFoundError(
            f"mlb_sim: no cached as-of board for {side} {season} {as_of}. "
            f"Run fetch_boards_asof([...]) first — it needs headless Firefox.")
    older = ((list(rate_seasons(side)) or available_seasons(side, save_dir))
             if use_season_blend(side) else [])
    boards = {s: load_board(side, s, save_dir) for s in older if s < season}
    boards[season] = rows
    # The contact prior is as-of too: pitches strictly before the cutoff, with
    # the PRIOR season whole underneath it (it finished before any game being
    # priced, so it leaks nothing).
    return build_rates(side, half_life=half_life, save_dir=save_dir,
                       boards=boards,
                       bmielke_season=(season if USE_CONTACT_PRIOR else None),
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
    path = save_dir / f"fg_{side}_{season}.json"
    data = None
    if path.exists():
        with open(path) as fh:
            raw = json.load(fh)
        data = raw if isinstance(raw, list) else None
    _BOARDS[key] = data
    return data


def available_seasons(side: str, save_dir: Path = SAVE_DIR) -> List[int]:
    out = []
    for p in save_dir.glob(f"fg_{side}_*.json"):
        try:
            out.append(int(p.stem.rsplit("_", 1)[1]))
        except (ValueError, IndexError):
            continue
    return sorted(out)


def _row_id(row: dict) -> Optional[int]:
    v = row.get("xMLBAMID")
    return int(v) if isinstance(v, (int, float)) and v else None


# --- platoon splits --------------------------------------------------------
# FanGraphs' splits API. One POST returns the WHOLE LEAGUE for one split, so
# vs-LHP and vs-RHP cost two requests each for hitters and pitchers.
#
# **Deliberately DUPLICATED from `EffortMLB.fetch_fg_split_sync`**, for the
# same reason `VENUE_ALIASES` is: importing EffortMLB drags in Qt and this
# module must stay headless. If the endpoint or the split ids change, both
# need the edit.
#
# The payload is COLUMN-oriented ({"k": [names], "v": [[row], ...]}) — unlike
# every other FanGraphs endpoint, which returns a list of dicts.
#
# Rows key on FanGraphs' `playerId`, not MLBAM. The leaders board carries both
# (`playerid` and `xMLBAMID`), which is the only reason this joins at all — no
# separate id map is needed here because `load_board` already has the row.
FG_SPLITS_URL = "https://www.fangraphs.com/api/leaders/splits/splits-leaders"
FG_SPLIT_VS_LHP = 1
FG_SPLIT_VS_RHP = 2
FG_SPLIT_STANDARD = "1"      # G PA AB H 1B 2B 3B HR R RBI BB IBB SO HBP SB..
FG_SPLIT_BATTED = "3"        # PA GB/FB LD% GB% FB% IFFB% HR/FB Pull% ...


def fetch_fg_split(split_id: int, stat_type: str = FG_SPLIT_STANDARD,
                   position: str = "B", season: int = 2026,
                   save_dir: Path = SAVE_DIR, refresh: bool = False,
                   timeout: float = 45.0) -> Dict[int, dict]:
    """One split for the whole league, keyed by FanGraphs playerId.

    Cached to disk — splits move once a day at most. Returns {} on failure,
    which callers must treat as "unavailable", never as "zero PA".
    """
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
        r = requests.post(FG_SPLITS_URL, json=body, timeout=timeout)
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


# Share of a hitter's plate appearances taken against a LEFT-handed pitcher.
# MEASURED off the splits themselves: 40,387 of 139,125 league PA = 0.290, and
# per hitter mean 0.279 sd 0.063 (range 0.072-0.467). The PA-WEIGHTED league
# share (0.290) is the one to use: it is what the sim actually realises
# (measured 0.290 over 220 real matchups), and using the per-hitter mean
# instead left a -0.021 run residual on league scoring.
#
# The exact version is each hitter's OWN share — the splits carry his PA vs
# each hand and the spread is real (sd 0.063) — but the aggregate is already
# neutral at the league value, so it is not worth the extra plumbing yet.
#
# **This is the CENTRING constant, and it is the whole reason the split can be
# applied at all.** A season rate is not a neutral-opponent rate — it is
# already ~71% the vs-RHP number. Adding a raw platoon gap on top would count
# the handedness twice, which is the identical mistake to uncentred fatigue
# and the uncentred park term (sim_state.md §10). With G the vs-LHP-minus-
# vs-RHP gap:
#
#     rate_vs_LHP = overall + (1 - w_L) * G
#     rate_vs_RHP = overall -      w_L  * G
#
# so a hitter's PA-weighted average over his real opponent mix returns exactly
# his season rate, by construction.
PLATOON_PA_SHARE_VS_LHP = 0.290

# How much of a PLAYER's OWN deviation from his handedness' league gap to
# believe. **Measured, and it is small**: split-half reliability of the
# deviation, 264 hitters with >=40 PA vs LHP and >=150 vs RHP, is
#
#     K   observed sd 0.0552  noise 0.0474  -> true 0.0283   reliability 0.26
#     BB  observed sd 0.0399  noise 0.0349  -> true 0.0194   reliability 0.24
#     HR  observed sd 0.0191  noise 0.0197  -> true 0.0000   reliability -0.06
#
# **Individual home-run platoon skill is ZERO over a season.** Anyone reading a
# hitter's own vs-LHP home-run rate is reading noise, and it looks perfectly
# reasonable while doing it. The LEAGUE gap by handedness is the signal; the
# player's personal departure from it is mostly not.
PLATOON_OWN_RELIABILITY = 0.25


def league_platoon_gaps(season: int = 2026, save_dir: Path = SAVE_DIR,
                        refresh: bool = False) -> Dict[str, List[float]]:
    """{bats: 9-vector of (vs LHP - vs RHP) rate gaps}, PA-weighted.

    Derived from the splits boards rather than hardcoded, so it tracks the
    league. Switch-hitters ("S") get their own row: they bat from the opposite
    side, so their gap is small and must not inherit either pure row.
    """
    path = save_dir / f"platoon_gaps_{season}.json"
    if path.exists() and not refresh:
        try:
            with open(path) as fh:
                return {k: list(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError):
            pass

    board = load_board("bat", season, save_dir)
    bats = {int(r["playerid"]): str(r.get("Bats") or "")
            for r in board if r.get("playerid") is not None}
    vs = {"L": fetch_fg_split(FG_SPLIT_VS_LHP, FG_SPLIT_STANDARD, "B",
                              season, save_dir),
          "R": fetch_fg_split(FG_SPLIT_VS_RHP, FG_SPLIT_STANDARD, "B",
                              season, save_dir)}
    if not vs["L"] or not vs["R"]:
        return {}

    # PA-weighted league rate vector per (batter hand, pitcher hand)
    acc: Dict[tuple, List[float]] = {}
    tot: Dict[tuple, float] = {}
    for hand, table in vs.items():
        for pid, rec in table.items():
            b = bats.get(pid)
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
    for b in ("L", "R", "S"):
        kl, kr = (b, "L"), (b, "R")
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


_PLATOON_GAPS: Optional[Dict[str, List[float]]] = None


def platoon_rates(rates: Sequence[float], bats: str, throws: str,
                  season: int = 2026,
                  w_l: float = PLATOON_PA_SHARE_VS_LHP) -> List[float]:
    """A hitter's rates against THIS pitcher's hand, centred on his own mix.

    No-ops when either hand is unknown, which is the honest default — the
    alternative is to guess a handedness and apply a real effect off it.
    """
    global _PLATOON_GAPS
    if not bats or not throws:
        return list(rates)
    t = throws.upper()[:1]
    if t not in ("L", "R"):
        return list(rates)
    if _PLATOON_GAPS is None:
        try:
            _PLATOON_GAPS = league_platoon_gaps(season)
        except Exception:
            _PLATOON_GAPS = {}
    gap = _PLATOON_GAPS.get(bats.upper()[:1])
    if not gap:
        return list(rates)
    # Centred: the season rate already contains his real opponent mix.
    f = (1.0 - w_l) if t == "L" else -w_l
    out = [max(0.0, r + f * g) for r, g in zip(rates, gap)]
    s = sum(out)
    return [v / s for v in out] if s > 0 else list(rates)


def build_rates(side: str, seasons: Optional[Sequence[int]] = None,
                half_life: float = SEASON_HALF_LIFE,
                save_dir: Path = SAVE_DIR,
                boards: Optional[Dict[int, List[dict]]] = None,
                bmielke_season: Optional[int] = None,
                bmielke_asof_date: Optional[str] = None,
                as_of: Optional[str] = None
                ) -> Tuple[Dict[int, dict], List[float]]:
    """Every player on the board as a shrunk outcome vector.

    Returns ({mlbam_id: {name, rates, pa}}, league_baseline).

    `boards` overrides what is loaded from disk, keyed by season. That is the
    seam the AS-OF path uses: pass a partial newest season and the full older
    ones, and everything downstream — the league baseline, the season rebasing
    and the playing-time prior — is computed against the partial board, because
    each of them is derived from `boards` rather than read separately.

    `as_of` bounds which cached as-of boards may contribute a WITHIN-SEASON
    recency window (`board_windows`). It is a cutoff rule, not a data source:
    the newest season's counts still come from `boards`, which is what keeps
    the recency path from having a second, differently-frozen view of the
    season.
    """
    if boards is None:
        # An explicit `seasons` is an INSTRUCTION and is honoured whole; the
        # flag only decides what the default is. Tested on `seasons` itself
        # rather than on the resolved list — testing the resolved list
        # truncates a caller's explicit request too, which is the opposite of
        # what the comment above it claimed.
        asked = bool(seasons)
        seasons = (list(seasons) if seasons
                   else list(rate_seasons(side))
                   or available_seasons(side, save_dir))
        if seasons and not asked and not use_season_blend(side):
            seasons = [max(seasons)]
        # `boards` is not gated here at all: the as-of path builds it and has
        # already applied the gate.
        boards = {s: load_board(side, s, save_dir) for s in seasons}
    boards = {s: b for s, b in boards.items() if b}
    if not boards:
        raise FileNotFoundError(
            f"mlb_sim: no cached fg_{side}_*.json boards in {save_dir}")

    newest = max(boards)
    older = [s for s in boards if s < newest]
    # A PARTIAL newest board is not the season's environment — it is the
    # environment of the weeks played so far, and in April that is 15% short
    # on home runs while the weather term charges the same cold again.
    league = projected_league_baseline(
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
            stuff_source_board(newest, boards[newest], as_of, save_dir),
            stuff_model_for(newest, save_dir))
        if st_delta:
            st_stab = stuff_stabilize(stabilize_for(side))

    # Triple-A lines and their fitted translation. The rule is DATE-AWARE and
    # not season-aware — count what was played before the game being priced,
    # which is the correction 5.11.1 named as the highest-value follow-up.
    #
    # It replaces a season rule that was wrong in BOTH directions at once: it
    # threw away a callup's Triple-A record, which all precedes his debut and
    # is the case the feature exists for, while a demoted veteran's line
    # postdated the replayed game and leaked the outcome backwards.
    #
    # **The prior season counts too, and measurably so.** Every Triple-A game
    # of season t-1 precedes every game of season t, so it is legal at any
    # cutoff — and on the 2026 as-of boards, restricting to the current season
    # covers 18% of thin arms at an April cutoff with a MEDIAN of 0 batters
    # faced. The feature would be missing exactly where it was built to help.
    # Adding the prior seasons takes that to 62%, median 25. Weighted by the
    # same `season_weights` the major league boards use, because it is the
    # same question about the same decay.
    #
    # Absent measurement or absent data this is simply off; there is no
    # default level factor, by design.
    milb_tabs: Dict[int, dict] = {}
    milb_sw: Dict[int, float] = {}
    milb_fac: List[float] = []
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
            milb_sw = season_weights(sorted(milb_tabs), half_life)

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
            # Including the NEWEST season, because `league` is now the
            # projected full-season environment rather than that board's own.
            # Leaving the newest un-rebased would keep a busy April hitter in
            # the April environment while a 20-PA one was shrunk toward the
            # projected one — the same player priced two ways. On a complete
            # board the projection IS the observed baseline, so this is an
            # identity and nothing shipped moves.
            counts = rebase_to_season(counts, season_league[season], league)
            # **Strip the player's OWN park before the shrink.** The
            # stabilisers estimate TALENT (three converging measurements),
            # but the line handed to them carries the park he played in —
            # roughly half his PAs at his club's field, on RAW counts. See
            # `decontaminate_counts`. Off by default; a park-neutral input
            # only makes sense together with the matching change to
            # `park_run_tilt`, so the two are gated on the same flag.
            if USE_PARK_DECONTAM:
                counts = decontaminate_counts(
                    counts, pa, pid, side, season, save_dir=save_dir)
            per_player.setdefault(pid, {})[season] = (counts, pa)
            names.setdefault(pid, row.get("PlayerName") or str(pid))
            # Handedness, from the newest board row that carries it. Needed by
            # `platoon_rates`; STARTERS had no `throws` at all before this.
            h = row.get("Throws") if side == "pit" else row.get("Bats")
            if h:
                hands[pid] = str(h)

    bm_lg: List[float] = []
    if side == "bat" and bmielke_season is not None:
        bm_rel, bm_lg = contact_profiles(list(per_player), bmielke_season,
                                         bmielke_asof_date, save_dir)

    # Blend ONCE. The Triple-A centring below needs every player's MLB sample
    # before the per-player loop can start, and blending twice is the same
    # work done twice on the hot path of the backtest.
    blended: Dict[int, Tuple[List[float], float]] = {
        pid: blend_seasons(by_season, half_life, newest)
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
                _c, _n = _milb_level_evidence(_tab.get(str(pid)), _lvf, milb_fac)
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
            # that is not a detail.** What has to vanish is the population's
            # net movement, which is sum(w_p * (tr_p - c)), NOT sum(tr_p - c).
            # A plain mean leaves it non-zero because `w` is player-specific
            # and correlates with the line — a man with 400 Triple-A batters
            # moves further than one with 60, so the heavy-sample players set
            # the level. Measured: the unweighted centre moved the pitcher
            # population -1.05% and the hitters +1.29%, both WORSE than not
            # applying the prior at all. Weighting by `w` makes the net shift
            # zero by construction.
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
        sw = season_weights(list(by_season) + ([newest] if newest not in
                                               by_season else []), half_life)
        wsum = sum(sw[s] for s in by_season) or 1.0
        # RAW playing time here, never the recency-weighted count: the curve
        # encodes what ROLE this much work implies, and a recency weight would
        # index every regular as a part-timer.
        return sum(sw[s] * (raw_pa[pid].get(s, 0.0) / per_club[s]
                            if per_club.get(s) else 0.0)
                   for s in by_season) / wsum

    # The hitter prior's centring is solved HERE, over the same blended counts
    # the loop below shrinks against — see `solve_bat_prior_tilt`. Solving it
    # from the board instead was worth -0.79 runs a game in April.
    centre_tilt = 0.0
    if side == "bat" and "bat" in _prior_sides() and BAT_PRIOR_CENTRED:
        _curve = prior_curve(side, newest, save_dir, boards[newest])
        if _curve:
            centre_tilt = solve_bat_prior_tilt(
                [(_share(pid, bs), blended[pid][0])
                 for pid, bs in per_player.items()],
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
        # otherwise regress to league on doubles and home runs, and against the
        # measured stabilisation (§3d.5) that prior carries 80% of the weight
        # even for a 600-PA regular — so "no player-specific prior" is a much
        # bigger assumption than it looks. Out of sample, BMIELKE predicts a
        # hitter's NEXT xwOBAcon at corr +0.70 against league's +0.00 and a
        # naive past-xwOBAcon's +0.56 (§3d.6).
        # **What he did at TRIPLE-A DISPLACES the playing-time prior, and does
        # not stack on it.** Both encode "this player is below league", and for
        # a callup they encode it for the SAME REASON — he has few plate
        # appearances *because* he was in Triple-A. Composing them marks him
        # down twice. Measured: stacked, the hitter side alone cost 0.130-0.146
        # runs a game on the as-of boards, which is the whole of the level
        # regression the first A/B showed (-0.141).
        #
        # The playing-time curve is a proxy for NOT KNOWING WHO SOMEONE IS.
        # Once his record one level down is in hand the proxy should step
        # aside, so the blend runs against the LEAGUE baseline and the weight
        # decides how far aside: a big Triple-A line replaces the proxy
        # outright, a thin one barely moves it. Applied here, ahead of the
        # contact and stuff priors, because those are independent evidence
        # that should refine whatever prior survives rather than be diluted
        # by it. Section 9c.
        # The Triple-A line, as a DEVIATION from what a player like him looks
        # like — never as a replacement for the playing-time prior's level.
        # The gate is already applied in building `milb_ev`.
        got_milb = milb_ev.get(pid)
        if got_milb is not None and milb_center:
            prior = milb_prior(prior, got_milb[0], got_milb[1], milb_cred,
                              stabilize_for(side), center=milb_center)
        got = bm_rel.get(pid)
        if got is not None:
            prior = contact_prior(prior, got[0], got[1], bm_lg)
        # An arm whose PITCHES say something his results have not had time to.
        # The weight moves with the prior: a prior that explains part of his
        # talent leaves less for his own line to resolve, so `stab` grows.
        stab = stabilize_for(side)
        delta = st_delta.get(pid)
        if delta is not None:
            prior = stuff_prior(prior, delta)
            stab = st_stab or stab
        rec = {
            "name": names[pid],
            # Stabilisation is PER SIDE — a pitcher's own home-run and contact
            # rates are far noisier than a hitter's and must be regressed far
            # harder. See `STABILIZE_PA_PIT`.
            "rates": shrink_rates(counts, prior, stab),
            "pa": pa,
            "hand": hands.get(pid, ""),
        }
        # The running game is read off the MOST RECENT season only. Legs go
        # first and clubs change their instruction — blending three years of
        # steal attempts describes a player who no longer exists.
        if side == "bat":
            latest = newest_rows.get(pid)
            if latest is not None:
                rec["run"] = runner_profile(latest)
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
# **MEASURED, and 8 was badly wrong.** A real club uses **24.2 distinct
# relievers** across a season (min 15, max 31) while the sim carried 8, so
# those 8 had to absorb ALL of the bullpen work. Checked against Oakland's
# 2026: 28 relievers, 3.40 bullpen appearances a game, of which the top 8
# covered 2.50 (73%) and the other twenty covered 0.90 (27%). The sim put
# 3.55 through its eight — over-using every modelled arm by roughly 20 points
# of appearance rate (Alvarado 30% real against 54% simulated, Perkins 15%
# against 38%).
#
# It is not only a usage-fidelity problem. Those other twenty arms are WORSE,
# so a real club's late innings are regularly covered by someone outside its
# best eight and the sim's never were — which is the most likely reason the
# sim's eighth inning scores 0.468 against a real 0.521.
#
# Depth alone does not fix usage: an arm that pitched yesterday must also be
# less likely to pitch today. No Oakland reliever pitched three days in a row
# all season (max streak 2), and the sim, which plays each game independently,
# has no way to represent that.
PEN_DEPTH = 14   # raising it changes nothing: the board yields ~14 per club


def build_side(abbr: str, bat_table: Dict[int, dict],
               pit_table: Dict[int, dict], season: int = 2026,
               hazard: Optional[List[float]] = None,
               save_dir: Path = SAVE_DIR):
    """A ready-to-simulate TeamSide for one club, straight off the boards.

    Lineup is the nine most-used bats; the starter is the club's highest-GS
    arm and the pen is the rest by innings. This is the OFFLINE stand-in for
    a posted lineup and a named probable — good enough to validate the engine,
    and the seam where the live `EffortMLB` lineup/probable path plugs in.
    """
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
        # None.** The lineup and the pen were both given this treatment after
        # dropping an entity turned out not to be neutral (5.5a, 5.6a); the
        # starter was the one that never was, and it returned a TeamSide whose
        # `.starter` was None. Nothing downstream expects that — `_game_side`
        # reads `base.starter.hazard` to decide the hook before it has decided
        # anything else — so it did not degrade, it raised.
        #
        # It has never fired on the shipped model, and that is the interesting
        # part: it takes a starter with essentially no CURRENT-season sample,
        # and the multi-season blend supplies him a row from an earlier year.
        # Measured over 6 as-of cutoffs x 30 clubs, 0 with the blend on and 2
        # with it off. **The blend is load-bearing for COVERAGE, not only for
        # accuracy**, which is not something the A/B that found this was
        # looking for.
        sp = Pitcher(name=str(sp_row.get("PlayerName") or "replacement-SP"),
                     rates=replacement_pitcher_rates(),
                     player_id=_row_id(sp_row), is_starter=True,
                     hazard=hazard or [])

    pen_rows = [r for r in pits
                if _num(r, "GS") / max(_num(r, "G"), 1.0) < 0.5][:PEN_DEPTH]
    team_g = _team_games(season, save_dir).get(abbr, 122.0) or 122.0
    pen = []
    for r in pen_rows:
        arm = make_pitcher(_row_id(r), pit_table)
        if arm is None:
            # He is on this club's board, so the club carries him; we simply
            # have no rate row. **Dropping him is not neutral** — it shortens
            # the pen and hands his innings to better arms, the same error as
            # truncating at 8 (5.5a), and `build_pen_from_itp` already refuses
            # to make it. It bites hardest on an AS-OF board, where a reliever
            # who has not pitched yet is absent by construction: the April pen
            # came out 11.4 arms against 13.6, positively selected, because the
            # arms a manager uses first are his best.
            pid = _row_id(r)
            arm = Pitcher(name=r.get("PlayerName") or str(pid),
                          rates=replacement_pitcher_rates(), player_id=pid)
        g = _num(r, "G")
        tr = load_reliever_traits(season).get(arm.player_id or -1) or {}
        # **Do NOT default a missing traits row to league-average usage.**
        # It fed BOTH the old availability gate and the selection score, so a
        # league-average default made an arm we know NOTHING about a workhorse
        # ready every day — exactly backwards. Only visible once PEN_DEPTH went
        # to 14: the five Oakland arms with no traits row ran 27-34% simulated
        # against a real 2-7%. Absence of a row means a fringe arm, so fall
        # back to his own appearance count instead. `app_rate` now drives the
        # selection score alone (see PEN_AVAILABLE_P), which makes this default
        # matter more, not less.
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
        w = g / (g + gmli_stabilizer(season, save_dir))
        arm.gm_li = w * raw_li + (1.0 - w) * 1.0
        # A long man is inferred from innings per appearance, not labelled.
        arm.multi_inning = float(tr.get("ip_per_outing", 1.0)) >= 1.25
        pen.append(arm)
    # Order by the leverage a manager actually uses him in.
    pen.sort(key=lambda a: -a.gm_li)
    # Savant's OAA and catcher-framing leaderboards IGNORE date parameters
    # (§3c), so an as-of run cannot have a partial-season version of either —
    # it gets the FULL season, which for an April game is future information.
    # `TEAM_CONTEXT_LAG = 1` takes the prior season's instead: stale, but it
    # predates every game being priced. Measured to be worth all of the
    # model's apparent advantage over the closing line (§3d.1).
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
# Steal OPPORTUNITY is not "times reached first" — it is the count of PAs with
# the runner on first and second base open.
#
# **This constant is CALIBRATED, not derived, and the two disagree.** Counting
# directly in the sim gives 1.027 eligible PAs per arrival at first; running
# the league at that value produces 1.07 steal attempts per team-game against
# a real 0.92. 1.65 is the value that reproduces the real league attempt rate.
# The gap is opportunity CONCENTRATION: in a nine-man lineup with no bench,
# the high-OBP aggressive runners reach base far more often than their share
# of real league attempts, so a rate that is correct per player over-fires in
# aggregate. Re-solve this against league SB+CS whenever the lineup
# construction changes — it absorbs that, and it is the only place that does.
OPP_PER_TIME_ON_FIRST = 1.65

_GMLI_STABILIZER: Optional[float] = None


def gmli_stabilizer(season: int = 2026, save_dir: Path = SAVE_DIR) -> float:
    """Relief appearances at which a pitcher's gmLI is half-believed.

    MEASURED, not chosen: the league median relief-appearance count, so a
    typical arm is trusted about halfway and a one-game callup is not. Writing
    a number here by hand was the original version and it disagreed with its
    own comment by 2x.
    """
    global _GMLI_STABILIZER
    if _GMLI_STABILIZER is not None:
        return _GMLI_STABILIZER
    rows = load_board("pit", season, save_dir) or []
    apps = [_num(r, "G") for r in rows
            if _num(r, "G") and _num(r, "GS") / max(_num(r, "G"), 1.0) < 0.5]
    apps.sort()
    _GMLI_STABILIZER = float(apps[len(apps) // 2]) if apps else 15.0
    return _GMLI_STABILIZER
LG_STEAL_ATTEMPT = 0.096
LG_STEAL_SUCCESS = 0.78
STABILIZE_STEAL_ATTEMPT = 60.0   # opportunities
STABILIZE_STEAL_SUCCESS = 20.0   # attempts
LG_SPD = 4.5                     # league mean Bill James speed score
SPD_TO_ODDS = 0.188              # Spd 7.0 -> ~1.6x odds of taking a base


def runner_profile(row: dict) -> dict:
    """Per-player running game from a FanGraphs batting row.

    Returns {steal_attempt, steal_success, speed}. Both steal terms are shrunk
    toward the league — a man with three attempts who made them all is not a
    100% base stealer — and `speed` is an ODDS multiplier on every "does he
    take the extra base" roll, not a probability.

    Verified against the 270 everyday regulars: the attempt-weighted mean of
    the derived success values is 0.778 against their real 0.769, so the
    per-player numbers are right.

    **The "known residual" here was a COUNTING BUG, and it is fixed
    (2026-08-20).** This note used to record simulated steal success landing
    near 0.86 against a real 0.769, and blamed the missing BATTERY term —
    catcher pop time, pitcher time to the plate — calling it "a wiring job,
    not a research one". It was neither. `simulate_game` inferred a stolen
    base from the state change "man was on first, is now on second, no out
    made", and the WILD PITCH branch produces exactly that signature, so every
    wild pitch with a runner on first was booked as a steal.

    Measured on 3,000 league-average clone games after crediting the steal
    from the EVENT instead:

        steals            1.427 / game   (real ~1.4)
        caught            0.423 / game   (real ~0.4)
        success rate      0.7714         (real 0.769)
        the same games counted the OLD way:  0.8379   <- the "near 0.86"

    So the discrepancy was the miscount in full, and the battery term is not
    needed to close it. `batter_stolen_bases` is no longer provisional on that
    account. A wild pitch is the battery losing the ball and a steal is the
    runner beating a throw — on most wild pitches the catcher never throws at
    all — so nothing about the two should ever have shared an inference.
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


_BAT_ROWS: Dict[tuple, Dict[int, dict]] = {}


def _bat_row(pid: int, season: int = 2026,
             save_dir: Path = SAVE_DIR) -> Optional[dict]:
    """One hitter's board row, by id. Indexed, not scanned."""
    key = (int(season), str(save_dir))
    tab = _BAT_ROWS.get(key)
    if tab is None:
        tab = {}
        for row in load_board("bat", season, save_dir) or []:
            rid = _row_id(row)
            if rid:
                tab[rid] = row
        _BAT_ROWS[key] = tab
    return tab.get(int(pid))


def make_batter(pid: int, table: Dict[int, dict], season: int = 2026,
                save_dir: Path = SAVE_DIR) -> Optional[Batter]:
    rec = table.get(pid)
    if rec is None:
        return None
    run = rec.get("run") or {}
    # Advancement is looked up HERE for the same reason handedness is: it was
    # attached in `build_side` only, so every hitter arriving through a POSTED
    # LINEUP ran on the league constants while the board-built lineup ran on
    # his own rates. Same silent-fallback shape as the `bats` bug in 5b.1.
    brow = _bat_row(pid, season, save_dir)
    adv = (runner_advance_rates(pid, _num(brow, "XBR"), _num(brow, "Spd"))
           if brow is not None else runner_advance_rates(pid))
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


def starter_pitch_hazard() -> List[float]:
    """League pitch-indexed hook curve, memoised. [] when unavailable."""
    if not _PITCH_HAZ:
        _PITCH_HAZ.append(real_starter_pitch_hazard() or [])
    return _PITCH_HAZ[0]


def milb_only_pitcher(pid: int, season: int = 2026,
                      save_dir: Path = SAVE_DIR, *,
                      is_starter: bool = False,
                      hazard: Optional[List[float]] = None
                      ) -> Optional[Pitcher]:
    """A pitcher with NO major-league board row, built from the minors.

    Returns None when the ladder has nothing on him, in which case the caller
    keeps its existing fallback — this can only ever improve on "price the
    debut as the club's ace", never make it worse by inventing a line.

    His translated minor-league rate is the shrinkage TARGET and his own
    translated counts are the evidence, with the per-outcome credit deciding
    how much a minor-league batter faced is worth. A 333-batter Double-A line
    is real information; it is not 333 major-league batters, and `credit` is
    what encodes the difference.
    """
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
    rates, n = milb_evidence(lv, lvf)
    if rates is None or n <= 0:
        return None
    league = league_baseline(load_board("pit", season, save_dir) or [], "pit")
    stab = stabilize_for("pit")
    if cred and any(cred):
        eff = [cred[i] * n for i in range(N_OUTCOMES)]
        got = [(eff[i] / (eff[i] + stab[i])) * rates[i]
               + (stab[i] / (eff[i] + stab[i])) * league[i]
               for i in range(N_OUTCOMES)]
    else:
        got = list(rates)
    name = f"{_milb_name(pid, season, save_dir) or ('#' + str(pid))} [MiLB]"
    return Pitcher(name=name, rates=_normalize(got), player_id=int(pid),
                   is_starter=is_starter, hazard=list(hazard or []),
                   pitch_hazard=(starter_pitch_hazard() if is_starter
                                 and USE_PITCH_HOOK else []),
                   throws="")


_MILB_NAMES: Dict[int, str] = {}


def _milb_name(pid: int, season: int, save_dir: Path) -> Optional[str]:
    """A minor leaguer's name.

    The MiLB snapshot stores counts only, so this reads the roster cache —
    without it the banner prints `#807739 [MiLB]`, and a starter you cannot
    NAME is one nobody will sanity-check.
    """
    if not _MILB_NAMES:
        try:
            with open(Path(save_dir) / f"mlb_roster_{season}.json") as fh:
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
        import requests
        r = requests.get(f"{STATSAPI}/people/{int(pid)}",
                         params={"fields": "people,id,fullName"}, timeout=10)
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


def make_pitcher(pid: int, table: Dict[int, dict], is_starter: bool = False,
                 hazard: Optional[List[float]] = None) -> Optional[Pitcher]:
    rec = table.get(pid)
    if rec is None:
        return None
    return Pitcher(name=rec["name"], rates=rec["rates"], player_id=pid,
                   is_starter=is_starter, hazard=list(hazard or []),
                   pitch_hazard=(starter_pitch_hazard() if is_starter
                                 and USE_PITCH_HOOK else []),
                   throws=rec.get("hand", ""))


# ---------------------------------------------------------------------------
# `python mlb_sim.py rates` — ingest report
# ---------------------------------------------------------------------------




# ===========================================================================
# 9c. MINOR LEAGUE LINES — the evidence a callup's MLB row does not have
# ===========================================================================
# **The problem this exists to solve, with the game that surfaced it.** On the
# 2026-08-18 board the model priced LAA @ HOU at 10.84 and WSN @ TEX at 10.37
# against actual totals of 4 and 5 — its two worst misses of the slate, and
# both featured a starter with almost no major league record. Jackson Kent had
# faced **19 batters all season** and George Klassen **76**. Both showed a raw
# .421 on-base allowed, which on 19 batters is one bad afternoon, and section
# 5.9's playing-time prior regressed them only to **.394 and .373 against a
# league .316** — a catastrophic-starter estimate, which is most of why those
# totals printed high.
#
# **The prior is not malfunctioning; it is answering a different question.**
# It regresses low-volume arms toward WORSE than league because low volume
# usually means low quality — a 40-batter reliever really does throw .368.
# That is right for a fringe reliever and wrong for a rookie's second start,
# where low volume means NEWLY ARRIVED. Playing time alone cannot separate
# them, and `PRIOR_SIDES` applies the same curve to both.
#
# What separates them is the record the model was not looking at. Klassen has
# **395 batters faced at Triple-A this season** against 76 in the majors.
#
# **It costs nothing to have.** MLB StatsAPI already serves the play-by-play,
# the probables and the schedule here; the same host serves every affiliated
# level, free, keyless, and keyed on the SAME MLBAM player id the boards carry
# — so there is no name match and no id map, which is where every previous
# cross-source join in this file has gone wrong. Five levels x three seasons is
# 34,000 player-seasons in 30 requests.
#
# **What is deliberately NOT done here.** This module collects; it does not
# translate. A Double-A strikeout is not a major league strikeout, and the
# level factors have to be MEASURED off players who appear at both levels
# rather than taken from a published table. Wiring this into `build_rates`
# is a rate-layer change and must be A/B'd against the close like every other
# one. Collect first, measure the translation second, ship third.

MILB_LEVELS: Dict[int, str] = {11: "AAA", 12: "AA", 13: "A+", 14: "A",
                               16: "ROK"}
MILB_CACHE_FMT = "milb_{season}.json"


def milb_cache_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / MILB_CACHE_FMT.format(season=season)


def fetch_milb_split(season: int, sport_id: int, group: str,
                     timeout: float = 90.0) -> List[dict]:
    """One level, one side, one season — every player in the pool.

    `playerPool=ALL` is required: the default returns only qualified players,
    which would drop exactly the thin-sample arms this whole section exists
    for. The limit is set past the largest level (Rookie ball, ~2,100 rows) so
    a silent truncation cannot happen; `totalSplits` is checked against what
    came back rather than trusted.
    """
    r = requests.get(f"{STATSAPI}/stats",
                     params={"stats": "season", "group": group,
                             "sportId": sport_id, "season": season,
                             "playerPool": "ALL", "limit": 5000},
                     timeout=timeout)
    r.raise_for_status()
    blk = (r.json().get("stats") or [{}])[0]
    rows = blk.get("splits") or []
    want = blk.get("totalSplits")
    if want and len(rows) < want:
        raise RuntimeError(
            f"mlb_sim: MiLB {season} sport {sport_id} {group} returned "
            f"{len(rows)} of {want} rows — raise the limit rather than "
            f"shipping a silently truncated level.")
    return rows


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


def _milb_team(sp: dict) -> Dict[str, object]:
    """The affiliate on a StatsAPI split.

    **This used to read `abbreviation` and always got nothing.** The nested
    team object at the league-wide `/stats` endpoint carries `{id, name,
    link}` — there is no `abbreviation` on it — so `or ""` swallowed the miss
    and `team` was empty in 100% of records: 22,684 season rows across five
    levels, and every as-of snapshot. The park item in 5.11.1 was blocked on
    that, not on missing data.

    `id` is stored as the key rather than a name because affiliates rename and
    relocate (the same club is Reno/RNO/2310 depending on who is asking) and
    the id is the only stable join to `/teams?sportId=11`.
    """
    t = sp.get("team") or {}
    out: Dict[str, object] = {}
    if t.get("id") is not None:
        out["team_id"] = int(t["id"])
    if t.get("name"):
        out["team"] = str(t["name"])
    return out


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
        path = milb_cache_path(season, save_dir)
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
                    rows = fetch_milb_split(season, sid, group)
                except Exception as e:
                    print(f"[milb] {season} {level} {group} FAILED: {e}")
                    continue
                for sp in rows:
                    pid = ((sp.get("player") or {}).get("id"))
                    st = sp.get("stat") or {}
                    if pid is None:
                        continue
                    rec = {k: st.get(k) for k in keys if st.get(k) is not None}
                    if not rec:
                        continue
                    rec.update(_milb_team(sp))
                    acc[side].setdefault(str(int(pid)), {})[level] = rec
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


_MILB: Dict[int, dict] = {}


def load_milb(season: int, save_dir: Path = SAVE_DIR) -> dict:
    """The cached minor league season, or {} when it has not been collected."""
    if season in _MILB:
        return _MILB[season]
    try:
        with open(milb_cache_path(season, save_dir)) as fh:
            _MILB[season] = json.load(fh)
    except (OSError, ValueError):
        _MILB[season] = {}
    return _MILB[season]


def milb_line(pid: int, season: int, side: str = "pit",
              save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
    """{level: counts} for one player in one season. Empty when he has none."""
    got = load_milb(season, save_dir).get(side) or {}
    return got.get(str(int(pid))) or {}


# --- Triple-A PARK FACTORS, per outcome (5.11.1) --------------------------
# **Triple-A parks are far more extreme than major league ones, and nothing
# in the translation corrected for it.** Measured 2026-08-20 off per-club
# home/away splits:
#
#     outcome     min     median   max      sd
#     HR          0.547   1.000    1.573    0.235      <- 2.9x spread
#     K           0.856   0.969    1.288    0.093
#     BB          0.815   1.063    1.245    0.106
#
# and they PERSIST — the club run factor correlates +0.586 / +0.754 / +0.493
# year over year, against a single major league season's +0.265 to +0.342
# (§8). Large and persistent is the pair that makes a factor real rather than
# noise. The Pacific Coast League is why: Albuquerque, Salt Lake, El Paso,
# Reno and Las Vegas are all at altitude.
#
# **Why this matters more than it looks.** Hitter home runs carry the LARGEST
# credit in the whole translation (2.00), so the outcome the model trusts most
# from Triple-A is the one the park distorts most. This is not a refinement.
#
# `statSplits` with `sitCodes=h,a` returns every player's home and away line
# league-wide in one request per side — verified complete against
# `totalSplits` (1,702 splits, all 30 clubs). Aggregating those by club gives
# a per-OUTCOME factor directly, which is what a nine-outcome model needs; a
# single run factor would have to be spread across the outcomes by assumption.

MILB_PARK_FMT = "milb_park_{season}.json"
# One season of park factor is mostly noise and averaging is an arithmetic
# improvement rather than a fitted one — the same argument as §8's three-season
# major league window, and the reason `AB_ARMS["window3"]` exists.
MILB_PARK_WINDOW = 3


def milb_park_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / MILB_PARK_FMT.format(season=season)


def _split_counts(st: dict, side: str) -> Tuple[Optional[List[float]], float]:
    """One home/away split as the engine's nine outcomes.

    Mirrors `_milb_counts` exactly — same singles-by-subtraction, same
    ground/air split off the feed's own ratio. Kept as its own function
    because the split rows are shaped like a `stat` block rather than like the
    level records `collect_milb` stores.
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


def fetch_milb_park_splits(season: int, group: str,
                           timeout: float = 180.0) -> List[dict]:
    """Every player's HOME and AWAY line at Triple-A, one request."""
    r = requests.get(f"{STATSAPI}/stats",
                     params={"stats": "statSplits", "group": group,
                             "sportId": MILB_ASOF_SPORT, "season": season,
                             "sitCodes": "h,a", "playerPool": "ALL",
                             "limit": 10000},
                     timeout=timeout)
    r.raise_for_status()
    blk = (r.json().get("stats") or [{}])[0]
    rows = blk.get("splits") or []
    want = blk.get("totalSplits")
    if want and len(rows) < want:
        raise RuntimeError(
            f"mlb_sim: Triple-A {season} {group} home/away returned "
            f"{len(rows)} of {want} splits — raise the limit rather than "
            f"shipping a silently truncated park factor.")
    return rows


# --- MINOR-LEAGUE STATCAST -------------------------------------------------
# **Hawk-Eye is in Triple-A and the Florida State League, and NOT in Double-A.**
# Probed rather than assumed, on two independent three-day samples: of 36
# tracked games, 27 were AAA and 9 were Single-A — every Single-A club a
# Florida State League one, which is the ABS test league. Zero Double-A, and
# Kade Anderson (whose whole record is AA) returns zero rows. So this can
# refine a Triple-A callup and can say nothing at all about a Double-A one;
# the fitted level ladder remains the only instrument there.
#
# Three things the probe cost that would each cost an afternoon:
#   * `minors=true` is the switch. `hfLevel=` does nothing — it returns a
#     valid header and zero rows, which reads exactly like "no data".
#   * Savant caps a response at 25,000 rows and does NOT say so, so a wide
#     date range silently truncates. Paged a few days at a time.
#   * There is NO level column, so AAA has to be separated from the FSL by
#     joining `game_pk` -> `sport.id` through StatsAPI.
#   * The first CSV column carries a UTF-8 BOM, so `row["pitch_type"]` misses
#     and a naive reader concludes the classifier was not run.
#
# **`bat_speed` and `swing_length` are 0% populated at every level**, so bat
# tracking does not exist below MLB and BMIELKE cannot be extended down. That
# is a hard stop, not a scraping problem.
#
# What IS carried, ~97% populated: release speed, spin, pfx_x/z, spin axis,
# extension, arm angle and the pitch classification. Those are aggregated here
# into the SAME column names the FanGraphs board uses (`pfx<TYPE>%`,
# `pfxsp<TYPE>`, `pfxv<TYPE>`, `pfx<TYPE>-X`, `pfx<TYPE>-Z`) so `_arsenal_block`
# reads a minor-league row without modification.
MILB_STATCAST_LEVELS: Tuple[str, ...] = ("AAA",)
MILB_STATCAST_CHUNK_DAYS = 3
_MILB_SPORT_LEVEL = {11: "AAA", 12: "AA", 13: "A+", 14: "A", 16: "ROK",
                     17: "ROK"}
_MILB_GAME_LEVEL: Dict[int, str] = {}


def _milb_game_level(pk: int, timeout: float = 15.0) -> Optional[str]:
    """The level a minor-league game was played at, via StatsAPI."""
    pk = int(pk)
    if pk in _MILB_GAME_LEVEL:
        return _MILB_GAME_LEVEL[pk]
    try:
        import requests
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


def _milb_statcast_chunk(start: str, end: str, timeout: float = 120.0
                         ) -> List[dict]:
    """One date window of minor-league Statcast, as dict rows."""
    import csv as _csv
    import io
    import requests
    r = requests.get("https://baseballsavant.mlb.com/statcast_search/csv",
                     params={"all": "true", "hfSea": f"{start[:4]}|",
                             "game_date_gt": start, "game_date_lt": end,
                             "type": "details", "minors": "true"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    text = r.text.lstrip("\ufeff")          # the BOM, see the header note
    rows = list(_csv.DictReader(io.StringIO(text)))
    if len(rows) >= 25000:
        _progress(f"milb-statcast: {start}..{end} hit the 25,000-row cap — "
                  f"NARROW THE WINDOW, this window is truncated")
    return rows


# Savant's pitch codes are the MODERN Statcast set; the board's `pfx` columns
# are PITCHf/x vocabulary (the prefix means exactly that). The two disagree on
# the most common pitch in baseball: Statcast's four-seam is `FF` and
# PITCHf/x's is `FA`, and `PITCH_FAMILIES` lists `FA`. Emitting `FF` therefore
# drops the four-seam out of the fastball family entirely — 1,048 of 1,164
# Triple-A arms throw one — and `spin_fb` / `velo_sep` are then built from
# whatever sinkers and cutters happen to be left.
#
# `FT` in that family list is the LEGACY two-seam code, which Savant retired
# around 2020 and folded into `SI`; it survives here only because the board
# still carries the column, and it appears once in a full season of Triple-A.
# Savant's `pfx_x`/`pfx_z` are in FEET, and the board's break columns are in
# INCHES — but x12 alone lands 1.7x too big, because the two use different
# BREAK CONVENTIONS (different reference distance / spinless baseline), not
# different units.
#
# FITTED, not reasoned: 47,184 major-league pitches over three windows, every
# arm with 150+ pitches matched to his own board row, slope through the origin,
# usage-weighted, restricted to pitch types he throws 5%+ of the time.
#
#     pfx-X   n=380   board = 0.5968 x mine   resid sd 0.93
#     pfx-Z   n=368   board = 0.5901 x mine   resid sd 0.48
#
# The two axes agreeing to within 1% is what says this is one convention and
# not two coincidences, so a single constant is used. **This is why an earlier
# pass "found" that Triple-A arms have twice the movement of major-league
# ones** — Triple-A had gone through this pipeline and MLB had come off the
# board, so the comparison measured the transform rather than the pitchers.
# Spin, velocity and usage need no such factor; they matched the board to
# within 0.5% on the same test.
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


def milb_arsenal_row(pitches: Sequence[dict]) -> Dict[str, float]:
    """Per-pitch-type aggregates under the FANGRAPHS board's column names.

    Emitting the board's own names is the point: `_arsenal_block` then reads a
    Triple-A arm exactly as it reads a major-league one, with no branch.

    **SPIN and VELOCITY come out on the board's scale; MOVEMENT DOES NOT.**
    Checked against 455 major-league arms with 100+ TBF:

        spin_fb   MLB 2284  AAA 2247   (-37, sd 143 vs 142)
        spin_bb   MLB 2501  AAA 2418   (-83)
        spin_off  MLB 1756  AAA 1699   (-57)
        velo_sep  MLB 6.23  AAA 6.06   (-0.16)
        mov_h     MLB 2.31  AAA 4.14   (+1.82)   <- NOT comparable
        mov_v     MLB 3.14  AAA 6.97   (+3.83)   <- NOT comparable

    The spin figures land on the league fastball number (the board's own
    `pfxspFA` averages 2,290) with matching dispersion, and Triple-A sitting
    slightly below is the right direction. Movement does not: Triple-A arms do
    not have twice the break, so the `x12` feet-to-inches conversion here is
    the wrong transform for the board's convention — MLB's 3.14 cannot be
    inches of induced vertical break, a four-seam alone carries ~15. The two
    axes are off by different ratios (1.79 and 2.22), so it is not one scale
    factor either; sign convention and signed averaging across handedness are
    both in play.

    **So use the spin and velocity columns; calibrate the movement ones against
    major-league arms before trusting them.** The fit is available for free —
    the same pitcher has a Statcast line and a board row in the same season,
    which is exactly how the level ladder was fitted.
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
    return out


def collect_milb_statcast(seasons: Sequence[int] = (2026,),
                          refresh: bool = False,
                          save_dir: Path = SAVE_DIR,
                          levels: Sequence[str] = MILB_STATCAST_LEVELS,
                          start: str = "-03-15", end: str = "-10-05",
                          verbose: bool = True) -> dict:
    """Minor-league Statcast, aggregated per pitcher and stored ALONGSIDE the
    existing minor-league lines in `milb_<season>.json` under `"arsenal"`.

    Deliberately not a separate artifact: it is the same players, the same
    season and the same cache, and a second file would drift out of step with
    the first the moment one of them is refreshed.
    """
    out: Dict[str, dict] = {}
    for season in seasons:
        path = milb_cache_path(season, save_dir)
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
        while cur <= d1:
            hi = min(cur + datetime.timedelta(days=MILB_STATCAST_CHUNK_DAYS - 1),
                     d1)
            try:
                rows = _milb_statcast_chunk(cur.isoformat(), hi.isoformat())
            except Exception as e:                        # noqa: BLE001
                _progress(f"milb-statcast {cur}..{hi}: {type(e).__name__} {e}")
                rows = []
            seen += len(rows)
            for r in rows:
                pk = r.get("game_pk")
                if not pk:
                    continue
                lvl = _milb_game_level(pk)
                if lvl not in levels:
                    continue
                pid = r.get("pitcher")
                if not pid:
                    continue
                by_pitcher.setdefault(str(int(float(pid))), []).append(r)
                kept += 1
            if verbose:
                print(f"[milb-statcast] {season} {cur}..{hi}  "
                      f"{len(rows):6d} rows, kept {kept}", flush=True)
            cur = hi + datetime.timedelta(days=1)
        arsenal = {pid: milb_arsenal_row(v) for pid, v in by_pitcher.items()}
        arsenal = {k: v for k, v in arsenal.items() if v}
        got["arsenal"] = arsenal
        got["arsenal_levels"] = list(levels)
        got.setdefault("season", season)
        with open(path, "w") as fh:
            json.dump(got, fh)
        if verbose:
            print(f"[milb-statcast] {season}: {seen} pitches seen, {kept} at "
                  f"{'/'.join(levels)}, {len(arsenal)} pitchers -> {path}")
        out[str(season)] = got
    return out


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
        path = milb_park_path(season, save_dir)
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
                rows = fetch_milb_park_splits(season, group)
            except Exception as e:
                print(f"[milbpark] {season} {group} FAILED: {e}")
                continue
            tot: Dict[int, Dict[str, List[float]]] = {}
            for sp in rows:
                tid = ((sp.get("team") or {}).get("id"))
                code = ((sp.get("split") or {}).get("code"))
                if tid is None or code not in ("h", "a"):
                    continue
                c, n = _split_counts(sp.get("stat") or {}, side)
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


_MILB_PARK: Dict[int, dict] = {}


def load_milb_park(season: int, save_dir: Path = SAVE_DIR) -> dict:
    if season in _MILB_PARK:
        return _MILB_PARK[season]
    try:
        with open(milb_park_path(season, save_dir)) as fh:
            _MILB_PARK[season] = json.load(fh)
    except (OSError, ValueError):
        _MILB_PARK[season] = {}
    return _MILB_PARK[season]


def milb_park_factor(team_id: Optional[int], season: int, side: str = "bat",
                     window: int = MILB_PARK_WINDOW,
                     save_dir: Path = SAVE_DIR) -> Optional[List[float]]:
    """A club's per-outcome park factor, averaged over `window` seasons.

    Averaged rather than taken from the target season alone for §8's reason:
    the noise falls as sqrt(n) while the true park effect survives, so the
    window is an arithmetic improvement and not a fitted one. Returns None
    when the club has no usable season, which means "do not adjust" — never a
    default factor of 1.0 dressed up as a measurement.
    """
    if team_id is None:
        return None
    acc = [0.0] * N_OUTCOMES
    got = 0
    for s in range(season - window + 1, season + 1):
        f = ((load_milb_park(s, save_dir).get(side) or {})
             .get(str(int(team_id))))
        if not f:
            continue
        for i in range(N_OUTCOMES):
            acc[i] += f[i]
        got += 1
    if not got:
        return None
    return [acc[i] / got for i in range(N_OUTCOMES)]


# --- AS-OF minor league snapshots — the date-aware rule (5.11.1) ----------
# The season-total cache above takes a SEASON as its unit, and section 5.11
# recorded why that is wrong in both directions at once. The two populations
# it lumps together are chronologically OPPOSITE:
#
#   a CALLUP's Triple-A innings all PRECEDE his debut. They are legal evidence
#   for every major league game he pitches, and the season rule throws them
#   away — which is the whole case the feature was built for.
#
#   a DEMOTED veteran's Triple-A innings POSTDATE the games being replayed,
#   and they leak twice over. The total is future information, and worse, the
#   LINE'S MERE EXISTENCE encodes the outcome: a man has July Triple-A innings
#   because he was sent down, and he was sent down because he pitched badly.
#   Read season-final, the model would recover the result and call it a
#   forecast.
#
# The correct unit is a DATE, not a season: count only what was played before
# the game being priced. StatsAPI serves exactly that — `stats=byDateRange`
# takes the same `sportId` / `playerPool=ALL` as the season call.
#
# **The endpoint was verified against the season call before anything was
# built on it**, because a windowed aggregate that quietly differs from the
# unwindowed one would put a second, unmeasured discrepancy underneath the
# feature. Over 2025 Triple-A pitching, a window spanning the whole year
# returns the same 1,264 players with the SAME batters faced for every one of
# them — 0 mismatches, 0 ids on either side alone. It is the same aggregation
# with a date filter, not a different report that resembles it.
# `test_milb_asof_window_is_the_season_call_windowed` pins that.
#
# **AAA only, and the cutoff grid is the boards'.** The snapshot is keyed to
# the same cutoff strings under `savedata/asof/` that the FanGraphs boards
# use, so `asof_cutoff_for` picks one rule for both and the Triple-A line can
# never be fresher than the major league board beside it.

MILB_ASOF_SPORT = 11                                  # AAA — see 5.11's table
MILB_ASOF_FMT = "milb_{season}_{as_of}.json"


def milb_park_report(season: int = 2026, save_dir: Path = SAVE_DIR) -> None:
    """The spread and the persistence — the two things that decide whether a
    park factor is real or a season of noise (§8)."""
    got = load_milb_park(season, save_dir)
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
    prev = load_milb_park(season - 1, save_dir)
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


def milb_asof_path(season: int, as_of: str,
                   save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / "asof" / MILB_ASOF_FMT.format(season=season,
                                                          as_of=as_of)


def fetch_milb_asof_split(season: int, sport_id: int, group: str, as_of: str,
                          timeout: float = 120.0) -> List[dict]:
    """One level, one side, season-to-date through `as_of`.

    Same contract as `fetch_milb_split` — `playerPool=ALL` so the thin arms
    this exists for are not dropped as unqualified, and `totalSplits` checked
    rather than trusted so a silent truncation cannot ship.
    """
    r = requests.get(f"{STATSAPI}/stats",
                     params={"stats": "byDateRange", "group": group,
                             "sportId": sport_id, "season": season,
                             "playerPool": "ALL", "limit": 5000,
                             "startDate": f"{season}-01-01",
                             "endDate": as_of},
                     timeout=timeout)
    r.raise_for_status()
    blk = (r.json().get("stats") or [{}])[0]
    rows = blk.get("splits") or []
    want = blk.get("totalSplits")
    if want and len(rows) < want:
        raise RuntimeError(
            f"mlb_sim: MiLB as-of {season} {as_of} sport {sport_id} {group} "
            f"returned {len(rows)} of {want} rows — raise the limit rather "
            f"than shipping a silently truncated level.")
    return rows


def collect_milb_asof(cutoffs: Sequence[str], season: int = 2026,
                      save_dir: Path = SAVE_DIR, force: bool = False,
                      verbose: bool = True) -> Dict[str, int]:
    """Cache a Triple-A snapshot per cutoff, in `collect_milb`'s shape.

    Stored under the same `{level: counts}` nesting the season cache uses so
    `_milb_counts` reads either one unchanged, and under the SOURCE's key names
    for the reason recorded on `_MILB_PIT_KEYS`: a cache that has already been
    interpreted cannot be re-interpreted when the interpretation moves.
    """
    got: Dict[str, int] = {}
    level = MILB_LEVELS[MILB_ASOF_SPORT]
    for as_of in cutoffs:
        dest = milb_asof_path(season, as_of, save_dir)
        if dest.exists() and not force:
            if verbose:
                print(f"[milb-asof] {season} {as_of}: cached")
            continue
        acc: Dict[str, Dict[str, Dict[str, dict]]] = {"pit": {}, "bat": {}}
        ok = True
        for side, group, keys in (("pit", "pitching", _MILB_PIT_KEYS),
                                  ("bat", "hitting", _MILB_BAT_KEYS)):
            try:
                rows = fetch_milb_asof_split(season, MILB_ASOF_SPORT, group,
                                             as_of)
            except Exception as e:
                print(f"[milb-asof] {season} {as_of} {group} FAILED: {e}")
                ok = False
                continue
            for sp in rows:
                pid = ((sp.get("player") or {}).get("id"))
                st = sp.get("stat") or {}
                if pid is None:
                    continue
                rec = {k: st.get(k) for k in keys if st.get(k) is not None}
                if not rec:
                    continue
                rec.update(_milb_team(sp))
                acc[side].setdefault(str(int(pid)), {})[level] = rec
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
        with open(milb_asof_path(season, as_of, save_dir)) as fh:
            _MILB_ASOF[key] = json.load(fh)
    except (OSError, ValueError):
        _MILB_ASOF[key] = {}
    return _MILB_ASOF[key]


def available_milb_asof(season: int = 2026,
                        save_dir: Path = SAVE_DIR) -> List[str]:
    """Cutoffs with a cached Triple-A snapshot, ascending."""
    d = Path(save_dir) / "asof"
    if not d.is_dir():
        return []
    pre = f"milb_{season}_"
    return sorted(f.name[len(pre):-len(".json")] for f in d.glob(f"{pre}*.json"))


def milb_report(season: int = 2026, save_dir: Path = SAVE_DIR) -> None:
    """How much minor league evidence exists for the arms the model is
    thinnest on — the check that this is worth wiring, before it is wired."""
    milb = load_milb(season, save_dir)
    if not milb:
        raise SystemExit(f"mlb_sim: no MiLB cache for {season}. "
                         f"Run `python mlb_sim.py milb --refresh` first.")
    board = {_row_id(r): r for r in (load_board("pit", season, save_dir) or [])
             if _row_id(r)}
    thin = sorted(((pid, _num(r, "TBF")) for pid, r in board.items()
                   if 0 < _num(r, "TBF") < 150), key=lambda x: x[1])
    covered = [(p, t) for p, t in thin if milb_line(p, season, "pit", save_dir)]
    extra = []
    for pid, tbf in covered:
        m_tbf = sum(v.get("battersFaced", 0)
                    for v in milb_line(pid, season, "pit", save_dir).values())
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
        lv = milb_line(pid, season, "pit", save_dir)
        nm = (board[pid].get("PlayerName") or str(pid))[:23]
        print(f"  {nm:<24s}{tbf:>9.0f}{m_tbf:>10.0f}"
              f"{(m_tbf/tbf if tbf else 0):>6.1f}  "
              f"{'+'.join(sorted(lv))}")




# --- AAA -> MLB translation, MEASURED -------------------------------------
# Section 9c collected the minor league lines. This turns them into a PRIOR,
# and every number in it is measured off matched players. Nothing here is a
# published table and nothing has a plausible-looking default: when the
# measurement is missing the feature is OFF, because a made-up level factor is
# the exact failure section 5.6c spent a session undoing.
#
# **Only AAA.** Measured 2026-08-19, the level signal lives there: 113-114
# players carry a AAA line into an MLB season against 30-32 from AA and single
# digits below, so the lower levels can neither estimate a factor nor move
# enough players to matter. The literature's ROK->A->A+->AA->AAA chain is the
# right build if it ever earns its place; it is not this one.
#
# Two quantities, both fitted, and they answer different questions:
#
#   FACTOR   what a AAA rate becomes in MLB. Measured off players with BOTH a
#            AAA and an MLB line in the SAME season, in log-odds, weighted by
#            `min(n)` because that is what limits a pair's precision. Pooling
#            the two directions matters: a man promoted was hot at AAA and a
#            man demoted was cold in MLB, so promotions alone overstate the
#            level gap by charging regression to the mean as difficulty. This
#            is the standard matched-mover design (James 1985; Davenport;
#            Glazer 2026 gives it its diff-in-diff form).
#
#   CREDIT   how many MLB plate appearances one AAA plate appearance is worth,
#            per outcome and per side. Fitted out of sample: season t's AAA
#            line against season t+1's MLB rate, choosing the credit that
#            minimises squared error. This is where the pitcher/hitter
#            asymmetry lands — measured, a pitcher's AAA home-run rate carries
#            almost nothing (corr +0.044) while a hitter's carries a lot
#            (+0.579), and a single global weight would import the first along
#            with the second.
#
# **Translation and regression are two operations and it is easy to do one
# twice.** The classic MLE observation is that a Triple-A slugger's translated
# home runs fall partly because the level is harder and partly because he was
# at the top of his own range that year. The factor does the first; `credit`
# feeding the EXISTING per-outcome stabiliser does the second. The translated
# line enters as evidence with a sample size, never as a pre-shrunk estimate.

MILB_TRANSLATION_PATH = SAVE_DIR / "milb_translation.json"
# Minimum sample on each side of a matched pair. Low enough to keep the pairs,
# high enough that a logit is meaningful.
MILB_PAIR_MIN = 50
# Target sample for the credit fit — the season t+1 line has to be reliable
# enough to be worth fitting against.
MILB_TARGET_MIN = 150
# **Off until an A/B says otherwise, like every other term in this file.**
# It ships off for a specific reason, not caution: with it on, the pitcher
# population reads **-1.18% of on-base against the board it was built from**,
# past the 1.0% tolerance `test_rate_layer_reproduces_the_board_it_came_from`
# enforces — the same aggregate identity section 5.9 was built around.
#
# The cause is the DISPLACEMENT being total. Blending a thin arm's Triple-A
# line against the league instead of against the playing-time prior makes him
# better, and the playing-time prior encodes something the Triple-A line does
# not fully substitute for: a pitcher who has faced 19 major league batters is
# worse than league whatever he did one level down, because the success does
# not fully carry and because usage is selected on performance. Displacing it
# outright throws that away; composing on top of it double-counts on the
# pitcher side. The right answer is between the two and has not been measured.
#
# Hitters are unaffected by that argument — `PRIOR_SIDES` never applied a
# playing-time prior to them at all (5.11) — so the two sides may well want
# different treatment.
#
# **SHIPPED ON 2026-08-20, after the third A/B and two structural fixes.**
# The two versions that failed were failing for implementation reasons, not
# because the evidence is weak: the prior had no MLB-sample gate (`aaa` was
# applied to everyone, and it is worth +0.2%/+1.2% past 150 PA) and it
# overwrote the playing-time prior's LEVEL instead of carrying a deviation
# from the peer mean. Gated and centred, against the de-vigged close over
# 2025+2026 at 2000 sims a game:
#
#   level bias        -0.127 vs -0.141 | -0.089 vs -0.108   better BOTH
#   corr with line    +0.7470/+0.7434  | +0.7299/+0.7261    better BOTH
#   disagreement sd    0.671/0.673     |  0.753/0.756       tighter BOTH
#   moneyline t vs base   -0.15        |    +0.09           NEUTRAL (pooled -0.06)
#   totals slope       0.866 vs 0.900  |  0.820 vs 0.812    MIXED
#   corr with actual  +0.1730/+0.1783  | +0.1715/+0.1677    MIXED
#
# Three better in both seasons, none worse in both. The displaced ungated
# version was pooled t -2.75 on the moneyline; that damage is gone.
#
# **It ships for ACCURACY, not for edge.** The minor league line is public, so
# the close already prices it — a correct prior here improves calibration
# without creating an edge, and the moneyline coming back NEUTRAL rather than
# positive is the expected result, not a disappointment. Read §3d.1 before
# reading anything else into it.
#
# The 2025 totals slope moving AWAY from 1.0 is the one real negative and is
# recorded as mixed rather than explained away.
USE_MILB_PRIOR = True

# **The Triple-A line is only worth having where the MLB record is thin, and
# the feature has been applied to EVERYONE.** Measured 2026-08-20, out-of-
# sample gain over regressing to league, by how much MLB record the player
# already had (leave-one-out, credit refit outside each held-out row):
#
#     prior MLB sample     hitters     pitchers
#     none                 +43.8%       +24.7%
#     1-49                  +2.5%       +11.5%
#     50-149                +0.5%        +4.8%
#     150+                  +0.2%        +1.2%
#
# 63% of hitter rows and 58% of pitcher rows sit in that last bucket, so most
# applications were injecting an over-weighted prior into players it cannot
# help. A sample gate is also what the published systems do rather than a
# thing invented here: Rotochamp adjusts minor league lines only for players
# under 400 major league PA, and Marcel — which has no gate because it reads
# no minor league data at all — simply projects every rookie at league
# average, which is the behaviour section 5.11 was built to stop.
#
# Set to 0 to disable the gate (the pre-2026-08-20 behaviour: apply to all).
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


def _side_counts(row: dict, side: str) -> Tuple[Optional[List[float]], float]:
    got = outcome_counts(row, side)
    return (list(got[0]) if got[0] else None), float(got[1] or 0.0)


# The rungs, best level first. A player is translated from EVERY level he
# played at, not just the top one — see `milb_evidence`.
#
# **Each rung is fitted on its OWN movers, then composed.** A player with both
# an MLB and a Triple-A line in the same season is common (529 batters / 730
# pitchers over 2024-26); one with an MLB and a DOUBLE-A line in the same
# season is not — 26 and 52, about three players per outcome, which cannot
# support a nine-parameter fit. But AA->AAA movers are plentiful (393 / 518),
# as are A+->AA (433 / 495) and A->A+ (447 / 526), because moving up inside
# the minors is the normal career path and reaching the majors is not.
#
# So the ladder is fitted rung by rung on well-powered within-minors movers and
# composed onto the existing AAA->MLB step. Borrowing the Triple-A factor for a
# Double-A line instead — the obvious shortcut — reads Kade Anderson's AA line
# as a 32.0% MLB strikeout rate against a fitted 25.1%, and prices his debut at
# 69.5% when the market says 51.7%. An 18-point error, which is why the
# AAA-only gate was defensible before this existed.
MILB_CHAIN: Tuple[str, ...] = ("AAA", "AA", "A+", "A")

# Rungs BELOW this are ignored: the fit exists for them but a player whose only
# record is Low-A is not someone the engine can say anything useful about, and
# the translation error compounds multiplicatively along the chain.
MILB_MIN_LEVEL_N = 40.0


def _milb_counts(lv: dict, level: str = "AAA"
                 ) -> Tuple[Optional[List[float]], float]:
    """A player's line AT ONE LEVEL as the engine's nine outcomes.

    Built from the StatsAPI counts stored raw by `collect_milb`, mirroring
    `outcome_counts`: singles are hits minus the extra-base hits, and the
    balls in play are split by the feed's own ground/air out ratio rather than
    by a league constant.
    """
    r = (lv or {}).get(level)
    if not r:
        return None, 0.0
    n = float(r.get("battersFaced") or r.get("plateAppearances") or 0.0)
    if n <= 0:
        return None, 0.0
    h = float(r.get("hits", 0)); d = float(r.get("doubles", 0))
    t = float(r.get("triples", 0)); hr = float(r.get("homeRuns", 0))
    k = float(r.get("strikeOuts", 0)); bb = float(r.get("baseOnBalls", 0))
    hbp = float(r.get("hitByPitch", 0))
    b1 = max(h - d - t - hr, 0.0)
    go, ao = float(r.get("groundOuts", 0)), float(r.get("airOuts", 0))
    outs = max(n - k - bb - hbp - h, 0.0)
    gshare = go / (go + ao) if (go + ao) > 0 else 0.5
    c = [0.0] * N_OUTCOMES
    c[K], c[BB], c[HBP] = k, bb, hbp
    c[GB_OUT], c[AIR_OUT] = outs * gshare, outs * (1.0 - gshare)
    c[S1B], c[S2B], c[S3B], c[HR] = b1, d, t, hr
    return c, n


def milb_step_factor(side: str, lo: str, hi: str,
                     seasons: Sequence[int] = (2024, 2025, 2026),
                     save_dir: Path = SAVE_DIR,
                     n_min: float = MILB_PAIR_MIN
                     ) -> Tuple[List[float], int]:
    """One rung: `logit(rate at hi) - logit(rate at lo)` for same-season movers.

    Fitted exactly the way the AAA->MLB factor is — matched within-season, both
    directions pooled, weighted by the smaller of the two samples — so the
    rungs compose on the same scale.
    """
    num = [0.0] * N_OUTCOMES
    den = [0.0] * N_OUTCOMES
    pairs = 0
    for season in seasons:
        milb = (load_milb(season, save_dir).get(side) or {})
        for lv in milb.values():
            cl, nl = _milb_counts(lv, lo)
            ch, nh = _milb_counts(lv, hi)
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


def milb_level_factors(side: str, aaa_to_mlb: Sequence[float],
                       seasons: Sequence[int] = (2024, 2025, 2026),
                       save_dir: Path = SAVE_DIR
                       ) -> Tuple[Dict[str, List[float]], Dict[str, int]]:
    """{level: factor onto MLB} by composing the rungs onto `aaa_to_mlb`."""
    out = {"AAA": list(aaa_to_mlb)}
    counts: Dict[str, int] = {}
    acc = list(aaa_to_mlb)
    for lo, hi in zip(MILB_CHAIN[1:], MILB_CHAIN[:-1]):
        step, n = milb_step_factor(side, lo, hi, seasons, save_dir)
        acc = [acc[i] + step[i] for i in range(N_OUTCOMES)]
        out[lo] = list(acc)
        counts[lo] = n
    return out, counts


def _milb_level_evidence(lv, factors, fallback):
    """`milb_evidence` returning COUNTS, for `build_rates`'s accumulator."""
    if not lv:
        return None, 0.0
    rates, n = milb_evidence(lv, factors or {"AAA": list(fallback)})
    if rates is None:
        return None, 0.0
    return [r * n for r in rates], n


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
        c, n = _milb_counts(lv, level)
        if c is None or n < MILB_MIN_LEVEL_N:
            continue
        rates = translate_milb([c[i] / n for i in range(N_OUTCOMES)], fac)
        for i in range(N_OUTCOMES):
            tot[i] += rates[i] * n
        n_tot += n
    if n_tot <= 0:
        return None, 0.0
    return [tot[i] / n_tot for i in range(N_OUTCOMES)], n_tot


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
                mc, mn = _side_counts(row, side)
                ac, an = _milb_counts(milb.get(str(pid)))
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
        lvf, lvn = milb_level_factors(side, factor, seasons, save_dir)
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
                    own[_p] = _side_counts(_r, side)
            for row in (load_board(side, b_season, save_dir) or []):
                pid = _row_id(row)
                if pid is None:
                    continue
                tc, tn = _side_counts(row, side)
                ac, an = _milb_counts(milb.get(str(pid)))
                if tc is None or ac is None or tn < MILB_TARGET_MIN:
                    continue
                tr = translate_milb([ac[i] / an for i in range(N_OUTCOMES)],
                                   factor)
                # His OWN MLB line in the SAME season as the Triple-A one —
                # needed by the "applied" specification below.
                oc, on = own.get(pid, (None, 0.0))
                orates = ([oc[i] / on for i in range(N_OUTCOMES)]
                          if oc and on > 0 else None)
                rows.append(([tc[i] / tn for i in range(N_OUTCOMES)], tr, an,
                             orates, on))

        # **TWO specifications, and the difference is not cosmetic.**
        #
        #   "twoway"  — AAA against LEAGUE. What the credit was originally
        #               fitted under, and a contest the Triple-A line wins
        #               easily because league average is a very weak opponent.
        #   "applied" — AAA against league AND his own MLB record, which is
        #               how `build_rates` actually uses it. 5.11.1 flagged this
        #               as a mis-specification and it is one: fitted the first
        #               way, the credits come out 1.3-2.7x too high, because
        #               the fit charges the Triple-A line for information the
        #               player's own MLB line was going to supply anyway.
        #
        # Measured 2026-08-20: under "applied" the out-of-fold gain halves
        # (bat +24.7% -> +8.9%, pit +15.8% -> +7.4%) but stays positive on all
        # 18 outcome-sides, and the credits fall (bat K 0.40 -> 0.15, GB_OUT
        # 1.00 -> 0.50; pit K 0.40 -> 0.30, 1B 1.00 -> 0.70).
        #
        # BOTH are stored. The shipped one is chosen by `MILB_CREDIT_SPEC` so
        # the change is an A/B arm rather than a silent re-fit of a constant
        # every downstream number already depends on.
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


def translate_milb(rates: Sequence[float],
                  factor: Sequence[float]) -> List[float]:
    """A AAA rate vector expressed on the MLB scale."""
    return _normalize([_expit(_logit(rates[i]) + factor[i])
                       for i in range(N_OUTCOMES)])


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
    # it is displacing the playing-time proxy rather than refining it. Falling
    # back to `prior` is the composing behaviour and is kept only so the
    # function can still be called that way.
    #
    # **`center` is the fix for the choice between them (5.11.2).** Displacing
    # the playing-time prior discards a real measured effect; composing on top
    # of it double-counts the pessimism. Both are wrong because both treat the
    # Triple-A line as evidence about the player's LEVEL. It is not — it is
    # evidence about where he sits AMONG PLAYERS LIKE HIM, and the level is
    # what the playing-time prior already knows.
    #
    # So pass `center` = the mean translated Triple-A line of the population
    # this player belongs to, and the prior moves by his DEVIATION from it:
    #
    #     prior + w * (his translated line - what a player like him looks like)
    #
    # The mean deviation is zero by construction, so the population level is
    # left exactly where the playing-time prior put it and only the SPREAD is
    # added. That is the standard empirical-Bayes form — regress toward the
    # CONDITIONAL population mean rather than the grand mean — and it is why
    # this cannot reproduce the -1.18% population bias that displacement did.
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


# ===========================================================================
# 10. BALL FLIGHT, FENCE GEOMETRY AND THE DISTANCE CALIBRATION
#
# What survives here is the physics layer: trajectory banks, per-park fence
# grids, and `calibrate_distance`, which fits `distance_scale` against real
# home-run outcomes. It is used by `python mlb_sim.py calibrate`.
#
# **The park x weather HOME-RUN MULTIPLIER that used to be built on top of
# this was REMOVED on 2026-08-15, along with `park_context`, `apply_park`,
# `hr_multiplier`, `regress_park` and `PARK_RELIABILITY`.** It was measured
# WORSE THAN LEAVING IT OUT on both quantities it could claim to help:
#
#   correlation with the real...    term off   uncentred   centred
#   ...run park factor              +0.491     +0.198      +0.096
#   ...HOME-RUN park factor         +0.276     +0.169      +0.133
#
# Both built home/road per club so the roster is held fixed — raw runs per
# game at a park is NOT a park factor, it is mostly the two clubs who play
# there. A real centring bug was found and fixed first (the multiplier was
# measured against a neutral park but applied to raw season rates that already
# carried the hitter's own park, so the home side ran at ~2x strength); it did
# not rescue the term, which is why the term is gone rather than gated.
#
# Do not reintroduce it without a measurement that BEATS leaving it out.
# ===========================================================================


DATA_DIR = Path(__file__).resolve().parent / "model_data"
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


def _cd_neutral() -> float:
    from homerunwidget import CD_NEUTRAL
    return CD_NEUTRAL


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


def clears_fence(traj: dict, wall_dist_ft: float, wall_h_ft: float,
                 scale: float = 1.0) -> bool:
    """Does this trajectory clear the wall at the point it reaches it?

    Walks the trajectory to the wall's horizontal distance and interpolates
    the height there. Comparing peak height, or total distance against the
    wall distance, both get the wall-scrapers wrong — and the wall-scrapers
    are the entire margin between one park and another.
    """
    xs, ys, zs = traj["x"], traj["y"], traj["z"]
    prev_r = prev_y = None
    for i in range(len(xs)):
        r = math.hypot(float(xs[i]), float(zs[i])) * scale
        y = float(ys[i])
        if prev_r is not None and prev_r <= wall_dist_ft <= r:
            span = r - prev_r
            f = (wall_dist_ft - prev_r) / span if span > 0 else 0.0
            return (prev_y + f * (y - prev_y)) > wall_h_ft
        if y <= 0 and prev_r is not None:
            return False
        prev_r, prev_y = r, y
    return False


def _park(venue: str) -> Optional[dict]:
    """Stadium record, by any of the park's names.

    **Resolves internally**, like `park_run_factor` and `weather_tilt` — the
    file's convention is that a park accessor takes whatever spelling the
    caller has. StatsAPI renames parks between seasons ("Rate Field",
    "Daikin Park", "UNIQLO Field at Dodger Stadium") and an exact-match
    `.get` on the raw name returned None, which reads downstream as altitude
    0 and no azimuth rather than as an error.
    """
    return _wm().STADIUM_DATA.get(resolve_venue(venue or "") or venue)


def park_azimuth(venue: str) -> Optional[float]:
    """Home-plate -> centre-field compass bearing. Resolves internally.

    A miss here does not raise — it returns None, and `weather_tilt` then
    drops the wind term entirely while the ball-flight grid falls back to an
    unrotated bearing. CLAUDE.md records what that costs: every park behaving
    as though centre field pointed due north, 419-421 ft everywhere against a
    102 ft real spread.
    """
    rec = _wm().PARK_ORIENTATION.get(resolve_venue(venue or "") or venue)
    if isinstance(rec, dict):
        return rec.get("azimuth")
    return rec


def wall_at(venue: str, polar_deg: float) -> Tuple[float, float]:
    wm = _wm()
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
# Running the ODE for every batted ball of every hitter is far too slow to do
# per slate. Instead, solve once per (park, weather) for the MINIMUM exit
# velocity that clears the fence at each (spray, launch angle) cell, then every
# batted ball is a table lookup against its own spray and launch angle.

GRID_HLA = tuple(range(-45, 46, 6))      # physics convention
GRID_LA = tuple(range(12, 45, 4))
GRID_EV = tuple(range(84, 123, 2))       # mph, ascending


BANK_DIR = DATA_DIR / "trajectory_banks"


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


class _quiet_solves:
    """Swallow stdout from the flight simulator while solving a bank.

    `BallFlightSimulator.calculate_trajectory` prints its high-altitude
    pressure diagnostic on EVERY call rather than once per weather
    resolution, so one Coors bank emits ~2,300 identical lines. Serially that
    is noise; across 22 worker processes sharing one stdout it is contention
    and interleaved garbage in the log.

    The number itself is correct — verified: with no API pressure the sim
    falls back to ISA STATION pressure (837 hPa at 5,190 ft), and
    `calculate_air_density` treats that as field-level and only rescales it
    for the ball's height, so altitude is not double-counted. Coors comes out
    at 0.825x sea-level density against ISA's ~0.83, and +24 ft of carry.
    The fix belongs in that print statement, but `homerunwidget.py` is out of
    scope here, so this suppresses it at the call site instead.

    stderr is deliberately left alone — a real failure must still surface.
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
        cached_trajectory_bank(weather, venue=venue)
    return venue


def prebuild_banks(venues: Sequence[str], weather: Optional[dict] = None,
                   workers: Optional[int] = None) -> None:
    """Fill the bank cache for many parks at once, across processes.

    Bank building is embarrassingly parallel — each park is an independent
    couple of thousand ODE solves with no shared state — and it is the entire
    cost of a calibration. Serially that is ~15 minutes for 30 parks; across
    a real core count it is about a minute.

    Already-cached parks are skipped before the pool is created, so this is
    RESUMABLE: kill it at any point and the completed parks stay on disk,
    because each bank is written atomically by `cached_trajectory_bank`.
    """

    todo = [v for v in venues
            if not (BANK_DIR / f"{_bank_key(weather, v)}.pkl.gz").exists()]
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
    path = BANK_DIR / f"{_bank_key(weather, venue)}.pkl.gz"
    if path.exists():
        try:
            with gzip.open(path, "rb") as fh:
                return pickle.load(fh)
        except Exception:
            path.unlink(missing_ok=True)      # corrupt cache, rebuild
    with _quiet_solves():
        bank = trajectory_bank(weather, venue, cd)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wb") as fh:
        pickle.dump(bank, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)                          # atomic: no half-written bank
    return bank


def trajectory_bank(weather: Optional[dict] = None,
                    venue: Optional[str] = None,
                    cd: Optional[float] = None) -> Dict[tuple, list]:
    """{(hla, la, ev): [(horizontal_ft, height_ft), ...]} for one weather.

    The expensive object, and the reason this module is usable at all. A
    trajectory depends on the launch conditions and the AIR — not on the park
    and not on the distance calibration. So one bank of ~2,300 solves serves
    every park at once, and re-fitting `distance_scale` costs nothing rather
    than re-solving the whole grid per candidate. Building the fence grid the
    naive way (solve per park per scale) is ~176,000 solves and half an hour.

    Altitude is the exception — it changes the air, so it belongs to the
    bank. Pass `venue` to bake the park's own altitude in.
    """
    weather = weather or {}
    sim = _sim()
    cd = _cd_neutral() if cd is None else cd

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


def fence_grid(venue: Optional[str], weather: Optional[dict] = None,
               scale: float = 1.0, cd: Optional[float] = None
               ) -> Dict[Tuple[int, int], float]:
    """{(hla, la): minimum EV in mph that clears the wall}.

    ~120 cells, bisected in 7 steps each — about 850 trajectories, a few
    seconds. Cache it per park and rounded weather; it does not move within a
    game.
    """
    weather = weather or {}
    sim = _sim()
    cd = _cd_neutral() if cd is None else cd

    temp = float(weather.get("temp_f", NEUTRAL_TEMP_F))
    hum = float(weather.get("humidity", NEUTRAL_HUMIDITY))
    wind = float(weather.get("wind_mph", NEUTRAL_WIND_MPH))
    wdir = float(weather.get("wind_dir_deg", 0.0))
    alt = float(weather.get("altitude_ft",
                            (_park(venue) or {}).get("altitude", 0.0)
                            if venue else NEUTRAL_ALTITUDE_FT))
    press = weather.get("pressure_pa")
    station = bool(weather.get("pressure_is_station", False))
    # A compass bearing must be rotated into the park frame; a dial reading
    # from the drawer is already field-relative. The caller tags which.
    azimuth = (park_azimuth(venue)
               if venue and weather.get("wind_frame") == "compass" else None)

    grid: Dict[Tuple[int, int], float] = {}
    for hla in GRID_HLA:
        polar = hla_to_polar(hla)
        if venue:
            wall_d, wall_h = wall_at(venue, polar)
        else:
            wall_d, wall_h = NEUTRAL_WALL_DIST(polar), NEUTRAL_WALL_HEIGHT
        for la in GRID_LA:
            lo, hi = 80.0, 125.0

            def clears(ev: float) -> bool:
                t = sim.calculate_trajectory(
                    ev, la, hla, wind, wdir, temp, hum, alt,
                    pressure_pa=press, cd_override=cd,
                    park_azimuth=azimuth, pressure_is_station=station)
                return clears_fence(t, wall_d, wall_h, scale)

            if not clears(hi):
                grid[(hla, la)] = float("inf")
                continue
            if clears(lo):
                grid[(hla, la)] = lo
                continue
            for _ in range(7):
                mid = 0.5 * (lo + hi)
                if clears(mid):
                    hi = mid
                else:
                    lo = mid
            grid[(hla, la)] = hi
    return grid


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


def _lookup(grid: Dict[Tuple[int, int], float], hla: float, la: float) -> float:
    h = min(GRID_HLA, key=lambda g: abs(g - hla))
    l = min(GRID_LA, key=lambda g: abs(g - la))
    return grid.get((h, l), float("inf"))


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
        if ev >= _lookup(grid, hla, la):
            hits += 1
    return hits / len(bbe)


# Fraction of balls in play converted per point of team OAA, per game.
#
# **Sized from the OAA definition, not fitted.** The league spread is -50 to
# +57 outs over ~122 games, i.e. 107 outs or ~0.88 outs per game between the
# extremes. Converting a ball in play from a hit into an out is worth roughly
# 0.75 runs, so the true best-to-worst swing is about **0.5 runs per game**.
# At 0.00022 the sim gave 0.75, ~50% hot; 0.00015 lands it on 0.5.
#
# EffortMLB's own study reported ~0.2 runs per START between the extremes, but
# that was the correlation of a defence index with actual-minus-expected wOBA
# on contact — a weaker, noisier signal than the OAA arithmetic, and per
# start rather than per game. The two are not in conflict.
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
# **Framing is NOT the umpire, and the difference decides where it belongs.**
# A tight or loose zone is shared by both teams in a game, so it moves both
# sides together and largely cancels for a side bet. A CATCHER belongs to one
# club, so his framing suppresses only the OPPONENT's offence — it does not
# cancel within a game, and it therefore prices totals, run lines and
# moneylines, not just strikeout props. (Section 5b previously said "props,
# not totals" for both; that was right for the umpire and wrong for framing.)
#
# Measured off Statcast's catcher-framing leaderboard, summed per club over
# 2026 (`rv_tot`, the run value of extra strikes taken):
#
#   sum across 30 clubs  +6.3 runs   <- zero-sum league-wide, as it must be
#   sd                    5.28 runs
#   best TOR +15.7 ... worst LAA -10.6, a 26.3-run spread
#
# Over 122 games that is **0.216 runs a game best-to-worst**, about 42% of the
# team-defence (OAA) spread already modelled — a real effect, and larger than a
# per-catcher reading of the leaderboard suggests, because a club's total sums
# its catchers.
#
# Because it is zero-sum across the league it CANNOT move league run scoring,
# which is why it was ruled out as a cause of the level bias in section 5.9
# before any of it was built.
FRAMING_RUNS_PER_GAME_SD = 0.043      # 5.28 runs / 122 games

# How the run value is delivered. Extra called strikes both create strikeouts
# and prevent walks; this is the share taken on the K side, with the remainder
# on BB. It does not affect the RUN value, which is calibrated as a total, but
# it sets the strikeout and walk props directly.
#
# **MEASURED — and 0.5 was wrong for a reason worth keeping.** It is a share
# of a MULTIPLIER, so what it splits is the two RELATIVE moves, not the two
# absolute ones. A borderline take called a strike instead of a ball moves the
# plate appearance from (b+1, s) to (b, s+1), which is worth +0.23 strikeouts
# and -0.21 walks per chance — near enough symmetric in absolute terms, which
# is what makes 0.5 look right. But walks are a quarter as common as
# strikeouts, so the same absolute move is more than twice the relative move
# on the walk side, and the honest split lands near 0.31.
#
# **sim_state.md 5.6c pointed at the wrong data.** Savant's framing board
# publishes `rv_11`..`rv_19`, read there as run value by COUNT; they are run
# value by ZONE — Statcast's out-of-zone quadrants, which is why 15 is missing
# from the sequence. See `framing_k_share` (section 15b) for what does settle
# it. Sanity mark: the measured absolute effects price out near 0.13 runs per
# extra strike against a published framing run value of about 0.125.
FRAMING_K_SHARE = _MEASURED_RUN.get("framing_k_share", 0.5)

# Runs per unit of the framing tilt, MEASURED the way `RUNS_PER_TILT` is, on
# league-average clones through `simulate_game`'s own context path. Unscaled,
# the K/BB tilt above runs 1.564x too strong: applying a nominal 0.15 runs/game
# of framing to both sides moved team-game scoring 4.4226 -> 4.1848, i.e. 1.564
# runs per unit rather than the 1.0 the units claim. Without this a good
# framing club would be credited with half again the runs it saves.
#
# At the calibrated value an elite framing club (TOR, +0.130 runs/game) lifts
# the opposing strikeout rate by ~0.8 points and cuts its walk rate by ~0.45 —
# the right order for a top framer.
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


def arm_factor(of_arm: Optional[float]) -> float:
    """Odds multiplier on a runner taking the extra base, from OF arm."""
    if not of_arm:
        return 1.0
    return math.exp(-ARM_ODDS_PER_MPH * (of_arm - ARM_MEAN_MPH))


# Home-field advantage, as a symmetric tilt on offence: the home side's rates
# scale up by HFA, the away side's down by the same amount.
#
# **The sim's structure supplies almost none of it.** Batting last and the
# extra-innings ghost runner together produce a home win rate of **0.5014**
# with identical teams, against a real MLB 2026 mark of **0.5264** (968-871,
# StatsAPI standings). Without an explicit term the model was 2.5 points short
# on every game, which is why its moneyline prices skewed to the underdog on
# essentially the whole board.
#
# Calibrated so identical teams reproduce the real home win rate — see
# `calibrate_hfa()`. It is applied to OFFENCE for simplicity; real home-field
# advantage is part offence, part defence and part umpire, but only the net
# effect on run scoring is identifiable from a win rate.
# Calibrated 2026-08-15: identical teams give 0.5005 / 0.5127 / 0.5287 home
# win rate at HFA 0 / 0.010 / 0.020, so 0.018 hits the real 0.5264. The tilt is
# symmetric, so league run scoring is unchanged (8.76 either way).
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
# Season rates are FLAT: every appearance is the player's mean self, so nothing
# varies within a game that is not already in the matchup. Real baseball has a
# large per-team-game shared factor that no forecast can see, and its absence
# is the bulk of the run-distribution deficit (section 5.1/5.2 of sim_state.md).
#
# **Shape, all measured — this is not a free choice:**
#
#   * It attaches to the TEAM-GAME, not the game. The two sides' 8-inning
#     totals correlate -0.053, so it is not a shared environment (umpire,
#     wind) and must not be drawn once for both sides.
#   * It is OFFENCE-side and game-long, not per-pitcher. Decomposing the real
#     covariance by inning-pair window:
#         spanning pairs (different pitchers)  V_o          = 0.0204
#         starter-window pairs (same starter)  V_o + V_sp   = 0.0135
#         bullpen pairs                        V_o + V_pen  = 0.0316
#     gives V_sp = -0.007 and V_pen = +0.011. **The starter's own window is
#     NEGATIVELY correlated beyond the game factor** — that is lineup turnover,
#     which the sim already reproduces — so a per-starter form draw is argued
#     against by the data, not merely unsupported.
#   * Size: the sim already supplies ~0.0045 per inning from matchup spread,
#     so the noise to ADD is ~0.0159 per inning of run variance.
#
# **It adds no handicapping edge.** 81% of the real shared factor is
# unpredictable before first pitch, which is exactly why it belongs here as
# noise rather than in the projection. It widens the distribution correctly,
# which is what totals, run lines and every threshold prop are priced off.
#
# `GAME_FORM_SD` is in offence_tilt units and is CALIBRATED against the
# measured per-inning covariance, not chosen.
#
# **Re-fitted 2026-08-15 on the REAL SLATE** (`calibrate-form --slate`), which
# replaced two things the clone fit had to assume:
#
#   * the clone version targets "real 0.0192 minus ~0.0045 of matchup spread".
#     Matchup, weather and park actually supply **0.00687** — 53% more — so the
#     noise term was over-supplying by the difference, which is why the slate
#     covariance ran 11-23% above real while the clone harness looked calibrated;
#   * removing the fatigue slope (FATIGUE_DECLINE_PER_BF) raised the run level,
#     and a shared MULTIPLICATIVE factor produces covariance in proportion to
#     it. This constant sits on the same axis as weather and park, and must be
#     re-fitted whenever ANY of them moves.
#
# **Re-fitted again 2026-08-15** after the pitcher-rate fixes (section 5.9)
# raised the run level and the pitcher spread. Both matter here: a shared
# multiplicative factor produces covariance in proportion to the level, and
# real matchup spread now supplies **0.00909** of pair-cov on its own against
# 0.00687 before — the fringe arms are properly bad, so more of the covariance
# is EARNED by the matchups and less has to be injected as noise. That is why
# the fitted sd falls even though nothing about the noise model changed.
#
# Fitted 0.1134: pair-cov 0.0186 against a real 0.0189, cov term 1.044 vs 1.060.
GAME_FORM_SD = 0.1134

# Runs are a CONVEX function of offensive rate, so a symmetric tilt does not
# leave the mean alone — it raises it (Jensen). The draw is therefore recentred
# by this much. Leaving it at 0 would reintroduce exactly the class of bug in
# section 10 of sim_state.md: a correction that is right in shape and wrong in
# level.
#
# **It scales with sd^2, so it must be refitted whenever `GAME_FORM_SD` moves**
# — raising the sd 5% and leaving this alone put the mean 0.13 runs high, which
# a test caught.
#
# Fitted on the real slate 2026-08-15 alongside GAME_FORM_SD. Two things the
# earlier value got wrong, both worth about the same amount:
#
#   * **units** — `RUNS_PER_TILT` is a GAME-TOTAL slope while the Jensen lift is
#     measured over innings 1-8, ~90% of a game, so converting one with the
#     other left a tenth of the lift standing;
#   * **coupling** — the shift lowers the run level, which lowers the covariance
#     the sd was fitted against, so the two cannot be solved in sequence. The
#     calibration probes its grid a second time with each candidate's own
#     matched shift.
#
# 0.0065 cancels a +0.049-run lift at the fitted sd, and the check is that the
# probe means go FLAT across the whole grid: 3.8607 / 3.8681 / 3.8671 against a
# form-off 3.8685.
GAME_FORM_MEAN_SHIFT = 0.0065


def draw_form(rng: random.Random, sd: Optional[float] = None) -> float:
    """One team-game's offensive form, centred so it does not move the mean."""
    s = GAME_FORM_SD if sd is None else sd
    return rng.gauss(-GAME_FORM_MEAN_SHIFT, s) if s else 0.0


# ---------------------------------------------------------------------------
# Weather — a DETERMINISTIC shift on the same axis as the form draw
# ---------------------------------------------------------------------------
# Fitted straight against ACTUAL runs, WITHIN park — deviations from each
# yard's own mean, which holds the fence fixed and asks only whether a warmer
# or windier-than-usual night at the same park scores more. 1,840 games of
# 2026, 1,474 of them open-air:
#
#   temperature       +0.0317 runs/degF   t 3.03-3.27
#   wind out to CF    +0.0618 runs/mph    t 3.10
#   wind SPEED alone  +0.0240 runs/mph    t 0.75   <- null, and that matters
#
# The last line is the check that this is real physics and not a fit: raw wind
# speed does nothing, while the component blowing OUT TO CENTRE is strongly
# significant. Direction is the signal, which is what the field-frame rotation
# exists to recover.
#
# **Deliberately NOT built on the trajectory-bank / fence-grid pipeline.**
# That is the machinery behind the park term, which measured worse than
# leaving it out (section 6) — so the physics is entered here as a measured
# run-environment effect instead, scored the way the park term was killed.
#
# **Centred on the PARK's own mean conditions**, not on a league constant,
# because the coefficients came from a within-park fit. Applying them to a
# deviation from the league mean would smuggle park-level climate back in as
# a park factor, which is exactly what was removed.
WEATHER_TEMP_RUNS_PER_F = 0.0317
WEATHER_WIND_OUT_RUNS_PER_MPH = 0.0618

# Runs per unit of `offence_tilt`, applied to BOTH sides. MEASURED: league
# clones give game totals 8.0002 / 8.5732 / 8.8950 at tilt -0.03 / 0 / +0.03,
# so the local slope is 14.9 runs per unit.
RUNS_PER_TILT = 14.9

# --- TEAM QUALITY: the one thing a bottom-up engine cannot say ------------
# **Measured 2026-08-22.** Regressing the actual run differential on the
# model's E[D] and on each club's season-to-date run differential per game
# (leak-free — strictly prior games, both clubs 20+):
#
#                       model E[D] loads    ACTUAL D loads     model captures
#   all games (3,428)     +0.264 +- 0.008    +0.353 +- 0.072        75%
#   market fav >= .65       +0.142 +- 0.027    +0.570 +- 0.240        25%
#
# In ordinary games the bottom-up build carries three quarters of club
# quality. In the heavy-favourite bucket its loading FALLS to 0.142 while
# reality's RISES to 0.570.
#
# **And it absorbs the market.** In that bucket, predicting the actual
# differential (n=321): adding the market to the model gives it t +1.95;
# adding TEAM QUALITY instead gives t +2.32 at a lower rmse; adding both
# collapses the market to t +1.06 while team quality holds at +1.64. What the
# market knows there and the engine does not is largely club quality, and club
# quality is free — it is on the slate already.
#
# **Level-neutral by construction**: league run differential sums to exactly
# zero, so a term proportional to it cannot move the run environment. That is
# the property every other amplitude lever had to have solved for it (4e).
#
# **Subset-targeted by construction too**: the term scales with the club's own
# differential, which is near zero for ordinary clubs and large exactly in the
# mismatches — so it moves the tail without touching the middle, which is the
# test all three amplitude levers failed.
#
# The gain is the RESIDUAL loading, 0.353 - 0.264, not the whole 0.353 —
# applying the full number would double-count the three quarters the roster
# already carries. OFF by default; arm `teamq`.
TEAM_QUALITY_GAIN = 0.089
# Run differential over few games is mostly noise; shrink toward zero by games
# played. 30 is a third of a season and is not fitted — it is a guard, and the
# arm should be re-run at a couple of values before anything ships.
TEAM_QUALITY_SHRINK_G = 30.0


def team_quality_tilt(rd_per_game: float) -> float:
    """Club run differential per game -> `offence_tilt` units."""
    if not TEAM_QUALITY_GAIN or not rd_per_game:
        return 0.0
    per_team = RUNS_PER_TILT / 2.0
    return TEAM_QUALITY_GAIN * rd_per_game / per_team


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
# **MEASURED 2026-08-18 on 7,510 open-air games, 2023-2026, within park.**
# Drag and Magnus are both proportional to air density, so the physically
# correct move is one density term rather than three collinear ones.
#
#   spec                      R2 on a within-park total
#   wind only                 0.307%
#   temp + wind (was shipped) 0.809%
#   DENSITY + wind            0.921%
#   temp + pressure + wind    1.012%
#
# Density is -0.1562 runs per 1% of density, pooled t -6.82, and it replicates
# at |t| > 3 in EVERY season (-3.17 / -4.33 / -3.06 / -3.82). Negative because
# denser air drags more.
#
# **Why NOT temp + pressure separately, even though it fits better.** Per
# standard deviation the measured pressure effect is 0.72x temperature's, where
# the physics allows only 0.27x — it is 2.7x too strong to be a density channel
# and is proxying synoptic weather (storm systems, cloud, wind regime). Raw
# pressure is also only t -0.55 and -1.39 in two of the four seasons. Buying R2
# with a coefficient the mechanism cannot support is exactly the failure section
# 10 records for every defect this engine has had. Density uses PHYSICS weights,
# so it cannot over-fit that confound.
#
# Humidity is a null on its own (t -0.91), which confirms 5b.2's measurement;
# it enters here only through density, where it belongs.
WEATHER_DENSITY_RUNS_PER_PCT = -0.1562
# Off until the closing-line A/B says otherwise, like every other term here.
USE_AIR_DENSITY = False

# Field-relative wind labels -> the component blowing OUT toward centre field.
# StatsAPI's label is ALREADY park-relative ("Out To CF"), not a compass
# bearing, so it needs no azimuth rotation. Do not confuse this with a feed
# bearing, which does (see CLAUDE.md on wind frames).
WIND_OUT_COMPONENT = {
    "out to cf": 1.0, "out to rf": 0.707, "out to lf": 0.707,
    "in from cf": -1.0, "in from rf": -0.707, "in from lf": -0.707,
    "l to r": 0.0, "r to l": 0.0, "varies": 0.0, "calm": 0.0, "none": 0.0,
}

# Per-park WIND RECEPTIVITY — how much of a given wind actually reaches the
# ball at that park. Fitted in `homerunwidget.py calibrate-wind` on batted-ball
# DISTANCE (feet of carry per mph, 500-2600 tracked balls per park, roof-closed
# games excluded), and it is a huge, real lever:
#
#   Sutter Health 0.225 | Wrigley 0.188 | Fenway 0.148 | ... league mean 0.091
#   ... | Dodger Stadium 0.040 | Rogers Centre 0.004
#
# Wrigley is 2.06x the mean and Dodger Stadium 0.44x — a 4.7x ratio between
# them. Applying one league-average runs-per-mph everywhere therefore overstates
# the wind at Chavez Ravine by more than 2x and understates it at Wrigley.
#
# **Validated on RUNS before being used, because it was fitted on DISTANCE and
# the transfer is not automatic.** Within-park fit of the game total over 1,474
# open-air 2026 games:
#
#   flat wind_out                t 3.10   R2 0.00647
#   wind_out x receptivity       t 3.85   R2 0.00994   <- used, full strength
#   half-shrunk receptivity      t 3.59   R2 0.00865
#
# and the direct check — fitting the flat slope SEPARATELY by tier — gives
# +0.0876 runs/mph at high-receptivity parks against +0.0532 at low ones, a
# 1.65x ratio in the predicted direction from data that never saw the distance
# fit. Full scaling beat half-shrunk, so the distance result transfers.
#
# Note this is NOT the machinery that killed the park HR term (section 6):
# that extrapolated a factor from fence geometry, while this MEASURES an
# observed response and only rescales a term already validated on runs.
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
                # `wind_mult` is fitted from how its batted balls respond to
                # the recorded outdoor wind — and under a shut roof they do not
                # respond at all, so the fit there measures ROOF USAGE, not park
                # geometry. It shows: every one of the five retractable parks
                # comes back with a NEGATIVE real wind response (Rogers Centre
                # -0.504, American Family -0.245, Chase -0.180, LoanDepot
                # -0.097), which is physically impossible — wind does not
                # reduce carry — and Chase is closed 70.5% of the time by
                # `park_weather_reference`'s own count.
                #
                # Averaging those into the divisor dragged it from 0.0976 to
                # 0.0911 and inflated EVERY open park's factor by ~7%, Sutter
                # Health Park from 2.305 to 2.470. Each park still keeps its own
                # `wind_mult`; only the scale they are measured against changes.
                # **Divide by the scale the fit SHRANK TOWARD, which the file
                # records and this function was throwing away.** `_global` was
                # popped and DISCARDED, and the divisor rebuilt as the mean of
                # the per-park values — a different and worse quantity.
                #
                # Each park's `wind_mult` is its raw response regressed toward
                # `_global.wind_mult_2pass` (0.098) by its own sample size:
                # correlating the implied shrink weight against n gives **+0.98**
                # for that target and -0.17 for the other, so 0.098 IS the league
                # scale. The mean of the SHRUNK values is not — it is dragged by
                # which parks happen to be thin and by the retractable-roof fits.
                # Sutter Health Park: 0.225 / 0.0911 = 2.470 under the old mean,
                # 0.225 / 0.098 = 2.296 against the real scale.
                #
                # The OPEN-AIR fallback is the same argument by a second route,
                # for a file with no `_global`: a park's response is fitted from
                # how its batted balls answer the recorded OUTDOOR wind, and
                # under a shut roof they do not answer, so the fit measures ROOF
                # USAGE. All five retractable parks come back NEGATIVE (Rogers
                # -0.504, American Family -0.245, Chase -0.180, LoanDepot
                # -0.097) — wind does not reduce carry — and Chase is closed
                # 70.5% of the time by `park_weather_reference`'s own count.
                # The two routes agree to 0.4%.
                mean = float(_glob.get("wind_mult_2pass") or 0.0)
                if mean <= 0.0:
                    _roofs = _wm().STADIUM_DATA
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
# Section 6 removed a park HOME-RUN multiplier extrapolated from fence geometry
# and ball-flight physics; it did not track observed park factors (corr +0.10
# to +0.28) and was worse than nothing. **This is a different quantity**: the
# OBSERVED home/road run ratio, which is by construction the thing that
# actually happened at that park.
#
# Removing the physics term left the engine with NO park effect at all, and
# that is fine on average and badly wrong at the extremes. Sutter Health Park
# is the case that exposed it — 12.23 runs a game at home against 8.08 on the
# road, a raw factor of **1.513**, the most extreme park in baseball — where
# the sim was projecting ~1.5 runs under the market.
#
# **Validated OUT OF SAMPLE**, every factor leave-one-game-out so a game never
# contributes to the factor used to predict it:
#
#   * correlation with the actual game total: **+0.14** (the whole model is
#     +0.17, so this one term is most of that again);
#   * regressing actual runs on the raw LOO factor: slope 4.63, **t = 6.10**.
#
# `PARK_RUN_RELIABILITY` is SOLVED, not chosen: it is the value at which
# regressing actual runs on the centred multiplier gives a slope equal to the
# league mean total, i.e. the multiplier is correctly scaled. Two independent
# estimates agree on the raw factor's reliability (0.52 by regression, 0.57 by
# variance decomposition); centring compresses the term, so the applied value
# is higher.
#
# **Centred**, for the third time in this engine after fatigue and platoon: the
# HOME club's rates already carry this park for ~half its games, so applying
# the full factor to them double-counts. The visitor's barely do.
PARK_RUN_PATH_FMT = "park_run_factors_{season}.json"
PARK_RUN_RELIABILITY = 0.699
PARK_HOME_GAME_SHARE = 0.5
PARK_RUN_CLAMP = (0.80, 1.40)

_PARK_RUN: Dict[tuple, Dict[str, float]] = {}     # keyed on (season, reliability)


def build_park_run_factors(season: int = 2026, save_dir: Path = SAVE_DIR,
                           refresh: bool = False) -> Path:
    """Home/road runs-per-game factor per park, off that season's linescores.

    Computed in-repo rather than fetched, which is what makes a leak-free
    version possible at all — unlike Savant's OAA and framing boards, this can
    simply be built from a season that finished before the games being priced.

    **A park's raw runs per game is NOT a park factor** — it is mostly the two
    clubs who play there. PNC read 10.57 actual runs/game while being one of
    the league's most pitcher-friendly yards. The home/ROAD ratio controls for
    the club, which is why it is the quantity stored.
    """
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


# **A shrinkage weight is only valid for the PREDICTOR it was solved on**, and
# `PARK_RUN_RELIABILITY = 0.699` was solved leave-one-game-out WITHIN a season
# — i.e. for a CONTEMPORANEOUS park factor. The leak-free backtest reads the
# PRIOR season's factor instead (`PARK_RUN_SEASON - TEAM_CONTEXT_LAG`), and a
# lagged predictor is attenuated by however well a park persists year to year.
# Measured on the raw factors on disk:
#
#     2024 -> 2025   corr +0.260   slope +0.265
#     2025 -> 2026   corr +0.389   slope +0.469
#
# so the lagged factor carries roughly a THIRD of the contemporaneous one's
# weight, and applying 0.699 to it made the park term 2-4x too strong in every
# backtest number in section 3d. Two independent routes agree: this persistence
# arithmetic implies 0.186 / 0.328 for the two season pairs, and regressing the
# actual game total on the model's own park contribution implies 0.162 / 0.533.
#
# The consequence is worth stating plainly because it is easy to get backwards:
# **the LIVE path was always fine** — it runs at lag 0 on the current season's
# factor, which is what 0.699 was solved for. Only the backtest was wrong, and
# it was wrong in the direction of making the model look WORSE.
#
# This is also 46% (2025) and 19% (2026) of the measured totals over-dispersion
# (slope 0.68 / 0.64 against a calibrated 1.0), so it is a real part of that
# defect but not the whole of it.
# **ONE SEASON of park factor is mostly noise, so AVERAGE SEVERAL.** §8 already
# recorded that a single season's run park factor is ~50% sampling noise. That
# is why the lagged predictor is so weak, and averaging fixes it arithmetically
# rather than by fitting anything: the noise falls as sqrt(n) while the true park
# effect survives. Measured on the 28 parks present in all three seasons on
# disk, predicting the 2026 factor:
#
#   predictor                 corr    slope     sd
#   2025 alone (was shipped) +0.385   +0.342   0.1158
#   2024 alone               +0.265   +0.240   0.1136
#   2024+2025 mean           +0.410   +0.463   0.0910   <- 35% more signal
#   2024+2025 weighted 1:2   +0.421   +0.459   0.0944
#
# The SLOPE is the operative number — it is how much park signal survives into
# the model — and it goes 0.342 -> 0.463. The mechanism is visible in the last
# column: two seasons averaged drop the sd from 0.1158 to 0.0910, which is the
# sqrt(2) reduction in the NOISE component. On 28 parks the correlation
# difference alone would not be significant (se ~0.19); the variance reduction
# is arithmetic, which is why this is believable and a fitted improvement would
# not be.
#
# Plain mean rather than recency-weighted: the two are indistinguishable
# (slope 0.463 vs 0.459) and the mean has no knob.
# **SHIPPED at 3 — the clearest win of 2026-08-17, and the only change that
# moved every metric the right way in BOTH seasons.** Scored against the
# closing total, which is ~7x the instrument scoring against results is:
#
#                          2025                    2026
#   corr with the line   +0.6457 -> +0.7396     +0.6919 -> +0.7169
#                         (+0.094, ~7 se)        (+0.025, ~2 se)
#   disagreement sd       0.836  -> 0.703        0.854  -> 0.771
#   corr with ACTUAL     +0.1582 -> +0.1798     +0.1535 -> +0.1603
#   calibration slope     0.713  -> 0.839        0.631  -> 0.764
#   (the market itself)   corr +0.1997, 0.953    corr +0.2213, 0.925
#
# On 2025 that takes us from 79% to 90% of the market's own correlation with
# the actual total. The pooled MONEYLINE improved too (log-loss 0.68140 ->
# 0.68086, paired +0.89, and t +0.11 against the close) — unexpected, since
# park largely cancels in a difference, so treat it as a bonus not the finding.
#
# **Why it works is arithmetic, not a fit**, which is why it transferred to the
# sim when nothing else this session did: one season of park factor is ~50%
# sampling noise (§8), so averaging drops the noise as sqrt(n) while the true
# park effect survives. Individual parks swing 0.25-0.43 year to year while the
# true spread ACROSS parks is only ~0.11 sd — the noise is 2-4x the signal in a
# single season.
#
# w=2 was measured too and is roughly half the gain (+0.068 on 2025, flat on
# 2026); w=4 splits between targets. 3 is where both agree.
#
# **Known limitation:** a park that stayed in the data and physically CHANGED is
# averaged across the change. Sutter Health Park (1.103 in 2025 -> 1.513 in
# 2026) is the real case. Camden's 2025 wall move is NOT visible in the run
# factor (0.877 / 0.961 / 1.058 / 0.953 across 2023-26) — §8's Camden note is
# about HR geometry and batted-ball data, where it does matter.
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
    """How far to trust the park factor. **One value at every lag — and the
    attenuation this function used to apply was a DOUBLE COUNT.**

    The argument for attenuating a lagged factor was that a park persists only
    0.34-0.47 year to year, so a stale factor deserves less weight. That
    reasoning is right in general and wrong here, because
    `PARK_RUN_RELIABILITY` had ALREADY been solved for exactly this predictor:
    §5b.3 derives it from "the raw factor's reliability (0.52 by regression,
    0.57 by variance decomposition)", grossed up because centring compresses
    the term. And a persistence slope IS a reliability —
    `cov(y1,y2)/var(y1) = var_true/(var_true+var_noise)` — so the two measure
    the same quantity and multiplying them shrinks the noise out twice.

    **Caught by the closing total, which is the instrument that could see it.**
    Scaling the park term and scoring against the line (se on corr ~0.013):

        scale   applied rel   corr with the line   model sd
        0.00        0.000           +0.5042          0.802
        0.46        0.324           +0.6507          0.874   <- the attenuation
        0.80        0.559           +0.6874          1.001
        1.00        0.699           +0.6919          1.097   <- optimum
        1.20        0.839           +0.6893          1.204

    The optimum is the shipped value, and the attenuated version was ~4 se
    worse. Scoring against realised totals could not have resolved this: it put
    the SLOPE at 0.643 -> 0.714, i.e. apparently better, because shrinking any
    over-dispersed predictor improves its calibration slope while destroying
    its correlation. **Calibration and accuracy move in opposite directions
    under a shrink, so never judge a shrink by its slope alone.**
    """
    return PARK_RUN_RELIABILITY


def park_run_window() -> int:
    """Seasons to average. **The window applies at every lag, including 0.**

    It used to apply only to a LAGGED factor, on the reasoning that "at lag 0
    the current season IS the answer and averaging older ones would only add
    staleness to a predictor that has none". **That reasoning is wrong and §8
    already contained the refutation** — how much of the true park effect a
    window carries:

        window            1        2        3        4
        -> 2025 slope   +0.265   +0.406   +0.611   +0.667
        -> 2026 slope   +0.342   +0.463   +0.597   +0.553

    A single season is mostly NOISE whether or not it is lagged, so the window
    is an arithmetic improvement (noise falls as sqrt(n), the true park effect
    survives) rather than a trade against staleness. There is nothing to trade.

    **What it cost, and why nothing caught it.** `TEAM_CONTEXT_LAG` is 1 in
    `ab_configure` and 0 everywhere else, so every A/B number ever measured
    used a 3-season window while the LIVE path that prices tonight's board
    used one season. The backtest was validating a different model from the
    one shipping, and no amount of A/B could see the difference. Measured at
    2026: Globe Life reads 0.962 on one season against 0.908 on three — 5.5%,
    about 0.55 runs on a ten-run game, and Globe Life is a park whose last two
    completed seasons are 0.896 and 0.867. League-wide the window moves the
    MEAN by +0.0013 and individual parks by up to 0.143, so it is a
    re-ranking, not a level shift, and it cannot be caught by any aggregate.

    No new leak: the window reaches BACK from `season` (see `park_run_factor`),
    so at lag 0 it adds completed prior seasons and nothing else.

    **OPEN, and it makes this fix CONSERVATIVE rather than complete.**
    `PARK_RUN_RELIABILITY = 0.699` was solved leave-one-game-out WITHIN a
    season, i.e. for a window-1 CONTEMPORANEOUS factor, and a shrinkage weight
    is only valid for the predictor it was solved on. A 3-season window is a
    less noisy estimate of the same quantity, so its true reliability is
    HIGHER than 0.699 and the live park term is now slightly UNDER-weighted.
    The direction is still right — a better estimate under-trusted beats a
    noisy one trusted correctly — but the number wants re-solving against the
    closing line the way `park_run_reliability`'s own scale table was built.
    Until then, do not read the live park term as calibrated.
    """
    return max(1, PARK_RUN_WINDOW)


def park_run_factor(venue: Optional[str], season: int = 2026,
                    save_dir: Path = SAVE_DIR) -> float:
    """Regressed home/road run factor for a park. 1.0 when unknown."""
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
                    # **Weighted by the games behind it.** Each season's `raw`
                    # is a home/road run ratio over `home_g` games, and a plain
                    # `mean` over the window counts a 12-game April sample
                    # exactly as heavily as a finished 81-game season. The
                    # count is stored in the file and was never read.
                    #
                    # It bites hardest in April, when the current season is the
                    # noisiest term in the window and still takes a third of
                    # the weight. The clearest case on the 2026-08 board is
                    # Sutter Health Park: 81 games in 2025 against 61 so far in
                    # 2026, and the two disagree 1.1031 to 1.5132 — the widest
                    # split of any park, at the park with the fewest seasons.
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
# **The rate layer estimates TALENT but is fed talent-plus-context.** A board
# row carries the park the player actually played in — roughly half his PAs at
# his club's home field — and `outcome_counts` reads RAW counts, not
# park-adjusted ones. `rebase_to_season` normalises the league run environment
# across seasons and nothing anywhere removes the park.
#
# So the shipped chain is:
#
#     board rate = talent + own park + own defence + own catcher + noise
#       -> shrink toward league     (strips all of it, proportionally)
#       -> add TONIGHT's park at the game level
#
# and the player's own park is both partly destroyed by the shrink and partly
# double-counted by the tilt. `park_run_tilt` already divides the HOME side's
# exposure out, but nothing corrects the VISITOR's hitters or EITHER pitcher —
# an offence tilt cannot reach an arm.
#
# The magnitude is not small: 2026 home/road run factors run 0.808 (Angel) to
# 1.513 (Sutter Health), and a player takes about half his PAs at home.
#
# **Per OUTCOME, not per run.** `park_run_factor` is a run factor, and park
# does not act uniformly: Citizens Bank Park reads 1.181 on runs but only
# 1.049 on home runs, while Busch reads 0.868 on runs and 0.785 on homers.
# Decontaminating a nine-outcome vector with one run number would inject a
# double-digit error into the column that matters most. `park_outcome_factor`
# measures each outcome separately, against THE SAME CLUBS' rates in all their
# other games, so a park shared by a good offence does not read hot.
#
# The factors reproduce known park physics without being told: Coors 3B 2.11
# and 2B 1.26 (huge outfield, thin air), Oracle 3B 1.53 (Triples Alley),
# Fenway 2B 1.14 (the Monster), Yankee HR 1.15 (the short porch).
#
# Year-to-year HR-factor correlation is +0.44/+0.53/+0.50, so one season is
# about half reliable — the window mirrors `PARK_RUN_WINDOW` rather than
# inventing a different one.

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
                # names and those drift between seasons.** Houston's park is
                # "Minute Maid Park" in 2024 and "Daikin Park" after; the White
                # Sox's is "Guaranteed Rate Field" then "Rate Field"; Dodger
                # Stadium becomes "UNIQLO Field at Dodger Stadium" in 2026.
                #
                # `measured_park_exposure` looks a park up across a 3-season
                # window with an exact `.get(venue)`, so a rename silently
                # collapsed that window: Daikin and Rate Field found 2 seasons
                # of 3, and Dodger Stadium found **ONE** — the window exists to
                # stabilise the estimate and two thirds of it was being dropped
                # with no error. 126 player-shares of exposure affected.
                #
                # `park_run_factor` already resolves on the way in; this table
                # did not. Verified no collisions: 30 raw names resolve to 30
                # distinct keys in every season.
                got = {(resolve_venue(k) or k): v["factor"]
                       for k, v in json.load(fh).items()}
        _PARK_OUTCOME[key] = got
    return got


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


# ---------------------------------------------------------------------------
# The two park BUILDERS — offline jobs, `python mlb_sim.py parkbuild`
# ---------------------------------------------------------------------------
# They lived as `build_park_outcome_factors.py` and
# `build_player_park_exposure.py`, which meant the readers below and the code
# that produces what they read were in different files with no import between
# them — so a change to `N_OUTCOMES`, to the PA schema or to the venue key
# would break the pair silently and only at read time. Same rule the weather
# and calibration jobs already follow: a batch job lives in the module that
# owns the data, and its heavy imports are lazy so the GUI path is unchanged.


def build_park_outcome_factors(season: int, reliability: float = 0.70,
                               save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
    """Per-OUTCOME park factors. `park_run_factor` is a RUN factor.

    Park affects home runs far more than strikeouts, so de-contaminating a
    nine-outcome vector with one run number injects error into every column it
    does not fit — Citizens Bank reads 1.181 on runs and 1.049 on home runs.

    Standard home/road ratio, but on the SAME SET OF CLUBS both ways: for park
    P take every PA played there and compare each outcome against the rate
    those same clubs produced in all their OTHER games that season. That
    controls for club quality — a park shared by a good offence would
    otherwise read hot.

    Regressed toward 1.0 by games played, because one season of ~2,400 PA is
    noisy and the shipped run factor is shrunk the same way.
    """
    import gzip
    import numpy as np                      # lazy: the GUI path never needs it
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


def build_player_park_exposure(season: int,
                               save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
    """Each player's ACTUAL park exposure, from his own plate appearances.

    The first version assumed every player took `PARK_HOME_GAME_SHARE` of his
    PAs at his club's home field, read off the board's `Team` tag. That fails
    exactly where it matters: ~9% of rows are `- - -`, traded mid-season, with
    no single home park — and those are the players whose exposure is least
    like the assumption. It is a poor assumption for everyone else too; players
    miss games, sit against same-handed starters, come up in July, and the
    schedule is unbalanced.

    None of it is needed. `savedata/pa/v2/` carries every plate appearance with
    its gamePk and the slate maps gamePk -> venue, so exposure is directly
    observable, per player, per season, both sides.

    **Stores the SHARES, not a baked exposure.** The shares are a fact about
    the schedule; the factors are an estimate with a window on them. Baking
    them together froze a single-season factor into a cache that
    `park_outcome_factor` reads over a 3-season window — two numbers for one
    quantity, which is the shape of every silent-cache defect here.
    """
    import gzip
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
        venue = club_home_park(club, s, save_dir)
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


def _board_club(row: dict) -> Optional[str]:
    """The club tag off a board row.

    FanGraphs wraps it in an anchor, and a player who changed clubs mid-season
    is tagged `- - -`. That placeholder must reach `park_outcome_factor` AS
    ITSELF so it can return a neutral factor — normalising it to "" would make
    it a substring of every club, which is precisely how `export_defense` put
    Cincinnati's whole OAA onto Boston.
    """
    t = str(row.get("Team") or "")
    hit = re.search(r">([A-Za-z]{2,3})<", t)
    if hit:
        return hit.group(1)
    t = t.strip()
    return t or None


_PARK_EXPO: Dict[tuple, dict] = {}          # keyed on (season,) — never bare


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


def measured_park_exposure(pid: int, side: str, season: int,
                           save_dir: Path = SAVE_DIR) -> List[float]:
    """The park multiplier a player's own line already carries, per outcome.

    `sum over parks of (his share of PAs there) * factor[park]`. Falls back to
    neutral only when he has no measured PAs at all — a callup with fewer than
    25, or a season with no PA file.
    """
    shares = player_park_shares(pid, side, season, save_dir)
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


def decontaminate_counts(counts: Sequence[float], pa: float,
                         pid: int, side: str, season: int,
                         save_dir: Path = SAVE_DIR) -> List[float]:
    """Strip a player's OWN park out of his counts, preserving PA.

    The multiplier his line carries is his MEASURED exposure — the parks he
    actually hit or pitched in, weighted by how many plate appearances he took
    there. Dividing it out leaves a park-NEUTRAL line, which is what the
    stabilisers were measured to shrink: they estimate talent (three
    converging measurements), and this is the step that makes the input
    talent-shaped.

    The vector is renormalised to the original PA so nothing downstream sees a
    changed sample size — the shrinkage weight must keep meaning what it meant.
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
    # The home club plays ~half its games here and its RATES already carry
    # that, so the multiplier is divided by the mix. Once
    # `USE_PARK_DECONTAM` strips each player's own park upstream that is no
    # longer true — the rates are park-NEUTRAL and both sides take the full
    # factor. Correcting one without the other double-counts in whichever
    # direction is left uncorrected, which is why they share a flag.
    if USE_PARK_DECONTAM:
        m = pf
    else:
        m = (pf / (1.0 + PARK_HOME_GAME_SHARE * (pf - 1.0))) if is_home else pf
    return (REAL_MARKS["game_total_mean"] * (m - 1.0)) / RUNS_PER_TILT


_PARK_WX_REF: Dict[int, Dict[str, dict]] = {}


_PARK_WX_REF_OM: Dict[int, Dict[str, dict]] = {}


# Seasons already warned about a missing weather reference — once each.
_PARK_WX_WARNED: set = set()


def park_weather_reference(season: int = 2026, save_dir: Path = SAVE_DIR
                           ) -> Dict[str, dict]:
    """{venue: {temp_f, out_component}} — each park's own typical conditions.

    **The reference has to be built from the SAME series it centres.** The
    shipped file is measured off StatsAPI's observations, whose wind arrives as
    a coarse eight-way LABEL; the forecast arms read Open-Meteo BEARINGS, and
    the two disagree enough (corr +0.71-0.73 on the resulting tilt) that
    centring one on the other's mean leaves a standing bias — measured at
    +0.006 of tilt, about +0.09 runs a game, i.e. a systematic lean to the
    over. That is the "centre on the population you actually apply it to" trap
    this file records five times, so the forecast arms get their own reference.

    It is built from the DAY-0 series for both lags on purpose: a reference is
    climatology, not information, so using day 0 for the day-1 arm centres it
    without leaking tomorrow's forecast into it.
    """
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

    # **A missing reference must NEVER degrade to {}.** `weather_tilt` reads
    # it as `ref.get("temp_f", temp)`, so an empty reference makes the term
    # `(temp - temp) = 0` — weather switches off ENTIRELY and SILENTLY, with
    # no error and perfectly plausible output. Verified: with the season
    # passed explicitly, 2025 got a non-zero tilt on 0 of 1,500 games against
    # 97% of 1,492 in 2026, because only `park_weather_ref_2026.json` exists.
    #
    # There is no builder for the observed reference in this module (only
    # `build_park_weather_ref_om`), so the missing seasons cannot simply be
    # generated. Fall back to the NEAREST season that does exist, which is
    # what the callers were already getting by accident when `weather_tilt`
    # defaulted its season to 2026 — the difference is that it is now visible
    # and recorded rather than an artifact of a default argument.
    # **The stems overlap and the naive glob is wrong.** `park_weather_ref_*`
    # also matches `park_weather_ref_om_2025.json`, so the observed lookup
    # "found" seasons that only exist for Open-Meteo, picked one, and then
    # failed to open it — landing back on {} , i.e. the silent-off it was
    # written to prevent. Match the stem EXACTLY.
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


def build_park_weather_ref_om(season: int = 2026, lag: int = 0,
                              save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
    """Each park's mean conditions AS OPEN-METEO SEES THEM -> a matched
    reference for the forecast arms. See `park_weather_reference`."""
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


def air_density(temp_f: Optional[float], pressure_hpa: Optional[float],
                humidity_pct: Optional[float]) -> Optional[float]:
    """Air density in kg/m3 from station pressure, temperature and humidity.

    Drag and Magnus are both proportional to density, so this is the physically
    correct way to combine the three thermodynamic variables — one term instead
    of three collinear ones. Wind stays separate: it is a velocity, not a
    density effect.

    Humid air is LESS dense than dry air, because water vapour (18 g/mol) is
    lighter than the nitrogen/oxygen mix it displaces (~29 g/mol). So humidity
    HELPS offence, which is the opposite of the intuition that muggy air is
    heavy — and getting that sign backwards is the obvious way to wire this
    wrong.

    Same formulation as `homerunwidget.BallFlightSimulator`: ideal gas with a
    Tetens saturation-vapour correction. `pressure_hpa` must be STATION
    pressure at the park's own elevation, never sea-level.
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
    dens = None
    if USE_AIR_DENSITY:
        dens = air_density(temp, weather.get("pressure_hpa"),
                           weather.get("humidity_pct"))
    ref_dens = ref.get("density")
    if dens is not None and ref_dens:
        runs += WEATHER_DENSITY_RUNS_PER_PCT * (dens - ref_dens) / ref_dens * 100.0
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

def load_scale(default: float = 1.0) -> float:
    """The fitted distance scale. 1.0 until `calibrate_distance()` has run."""
    try:
        with open(CALIB_PATH) as fh:
            return float(json.load(fh)["distance_scale"])
    except (OSError, KeyError, ValueError, TypeError):
        return default


def save_scale(scale: float, note: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(CALIB_PATH, "w") as fh:
        json.dump({"distance_scale": scale, **note}, fh, indent=2)


def load_bbe_frame(season: int = 2026, path: Optional[Path] = None):
    """Real batted balls with the columns this module needs, park-labelled."""
    import pandas as pd
    TEAM_TO_PARK, STADIUM_DATA = _wm().TEAM_TO_PARK, _wm().STADIUM_DATA

    path = path or (Path(__file__).resolve().parent
                    / f"savant_bbe_{season}.csv")
    df = pd.read_csv(path, usecols=[
        "events", "launch_speed", "launch_angle", "hc_x", "hc_y",
        "home_team", "batter"], low_memory=False)
    df = df.dropna(subset=["launch_speed", "launch_angle", "hc_x", "hc_y"])
    df["park"] = df["home_team"].map(TEAM_TO_PARK)
    df = df[df["park"].isin(STADIUM_DATA.keys())]

    dx = df["hc_x"] - 125.42
    dy = 198.27 - df["hc_y"]
    df = df[dy > 0]
    import numpy as np
    df["hla"] = np.degrees(np.arctan2(dx[dy > 0], dy[dy > 0]))
    df["is_hr"] = (df["events"] == "home_run").astype(int)
    return df


def calibrate_distance(season: int = 2026, sample: int = 60000,
                       seed: int = 7, workers: Optional[int] = None) -> dict:
    """Fit `distance_scale` so predicted home runs match REAL ones.

    The raw physics runs ~45 ft short, and a home run is a hard threshold
    against a fence, so that bias does NOT cancel in a ratio — left alone,
    almost nothing clears and the multiplier becomes tail noise. One scalar on
    the trajectory's horizontal distance is fitted here against actual
    `events == "home_run"` at real parks, which is ground truth we already
    have on disk.

    Fitting the COUNT rather than per-ball accuracy is deliberate: the
    multiplier is a ratio of rates, so what has to be right is where the
    fence sits in the distance distribution, not which individual ball went
    out.
    """
    df = load_bbe_frame(season)
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
    prebuild_banks(parks, {}, workers=workers)
    banks = {}
    for i, park in enumerate(parks, 1):
        banks[park] = cached_trajectory_bank({}, venue=park)
        print(f"[calib]   loaded {i}/{len(parks)} {park}", flush=True)

    by_park = {park: air[air["park"] == park] for park in parks}

    def predicted(scale: float) -> int:
        total = 0
        for park in parks:
            grid = fence_grid_from_bank(banks[park], park, scale)
            sub = by_park[park]
            for ev, la, hla in zip(sub["launch_speed"], sub["launch_angle"],
                                   sub["hla"]):
                if ev >= _lookup(grid, hla, la):
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
    save_scale(scale, note)
    print(f"[calib] wrote distance_scale={scale:.4f} to {CALIB_PATH}")
    return {"distance_scale": scale, **note}


# ===========================================================================
# 11. PROJECTION — END TO END
# ===========================================================================


DEFAULT_SIMS = 20000


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def project_game(home_abbr: str, away_abbr: str, venue: Optional[str] = None,
                 weather: Optional[dict] = None, n_sims: int = DEFAULT_SIMS,
                 season: int = 2026, seed: Optional[int] = 1,
                 hazard: Optional[List[float]] = None,
                 verbose: bool = False, with_pen: bool = True,
                 date: Optional[str] = None, live: bool = True,
                 game_number: Optional[int] = None) -> dict:
    """Simulate one game and return the priced board.

    **`venue` and `weather` DO alter the simulation.** What was removed on
    2026-08-15 is the park x weather HOME-RUN INTERACTION term (section 10);
    the park run factor and the weather tilt both still ride the form axis and
    both are large. Measured on CHC @ SEA, 2026-08-21, 2,000 sims: forcing
    Coors Field moves the total 8.12 -> 10.05 (+1.93 runs) and a 95F / 18 mph
    out-to-CF override moves it 8.12 -> 9.55 (+1.43).

    This docstring previously said they were "carried for reporting only",
    which is how trap 12 happens: `run_clv` passed neither and priced every
    live game at a neutral park with no weather, costing 1.7 runs on
    CLE @ COL, and the LIVE path is not covered by the A/B harness.
    """
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
            weather = game_weather(card["game_pk"], date)
            # **A SCHEDULED game has no observation, and that was silent.**
            # `game_weather` reads StatsAPI's game-time reading, which does not
            # exist until the game does, so every forward projection priced at
            # `weather_tilt = 0.0` — a neutral park on a 95F day. The forecast
            # is the information set a projection legitimately has.
            if weather is None:
                weather = forecast_game_weather(
                    venue or resolve_venue(card.get("venue") or ""),
                    card.get("start"))
            if weather and verbose:
                print(f"  weather: {weather.get('condition')}, "
                      f"{weather.get('temp_f')}F, wind {weather.get('wind_mph')} "
                      f"mph {weather.get('wind_label')}"
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
            # which only asks whether a nine was found at all — so Rotowire's
            # beat-writer PROJECTION printed as "posted". `probable_for`
            # already tags the row `lineup_source`, and the whole point of
            # that tag is that a projection is never folded in silently; the
            # banner was silently folding it in. Caught on 2026-08-22 when
            # both games read "lineup posted" at 01:40 on game day and
            # StatsAPI had zero cards filed for either.
            lu_tag = (("posted" if card.get(f"{_sk}_lineup_source") == "posted"
                       else "PROJECTED") if u["lineup"] else "board FALLBACK")
            print(f"  {tag}: SP {sd.starter.name}{sp_tag}"
                  f"   lineup {lu_tag}   pen {u['pen']}{note}")

    # **The rate correction, on the LIVE path too.** This call used to omit
    # `ml=` entirely, so `project` ran the incumbent whatever `RATE_MODEL`
    # said while `clv` and `backtest` — the two paths that DO pass it — ran the
    # variant. Inert while `RATE_MODEL = "baseline"` (`game_adjuster` returns
    # None), which is exactly why it would have survived until the residual
    # shipped and then printed a different price from `clv` for the same game
    # with nothing to say so. Trap 12 in its original costume: the live path is
    # not covered by the A/B harness, so an optional argument defaulting to
    # None is a silent divergence there and nowhere else.
    #
    # `as_of` is "" — LIVE, the season to date, never a frozen snapshot. A
    # projection of tonight's game legitimately has today's board.
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


def _fmt(v):
    return f"{v:+d}" if isinstance(v, int) else "  --"


# ===========================================================================
# 12. COMMAND LINE
# ===========================================================================


def league_side(tag: str) -> TeamSide:
    """A flat league-average side: nine league hitters, a league starter on the
    real hook curve, and EIGHT league relievers.

    **There were five byte-identical copies of this** — in `re24_report`,
    `validate_vs_reality`, `_form_probe`, `validate_dispersion` and
    `multiplier_run_value` — which is how two subtly different synthetic sides
    come to exist without anyone deciding. It is deliberately NOT `_demo_side`,
    which tilts the lineup by `quality` and carries only six arms; the two are
    for different jobs and the names now say so.

    **What it cannot do, stated here rather than rediscovered.** Every arm is
    identical and none carries deployment traits, so this side structurally
    cannot express anything margin- or leverage-conditional. A probe of the
    bullpen's score-awareness built on it measures zero and reads as a clean
    null (sim_state.md trap 5, three instances). Use real sides for that.
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
        for pid, rec in ranked:
            r = rec["rates"]
            print(f"  {rec['name']:<24s} PA {rec['pa']:6.0f}  "
                  f"K {r[K]:.3f} BB {r[BB]:.3f} HR {r[HR]:.3f} "
                  f"1B {r[S1B]:.3f} GB {r[GB_OUT]:.3f} AIR {r[AIR_OUT]:.3f}")


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
    # **The MEAN and the MEDIAN are different numbers and only one of them is
    # comparable to a book's line.** Game runs are right-skewed, so the line a
    # book hangs — the one whose over and under sit closest to even money — is
    # the MEDIAN of its predictive distribution, measured 0.40-0.47 BELOW the
    # mean total. Printing only the mean invites differencing it against the
    # market and reading the skew as a half-run disagreement; that trap is
    # recorded three times in sim_state.md and was walked into again on
    # 2026-08-20. Both are printed, and which is which is named.
    #
    # **Not the sample median** — a game total is a whole number, so its median
    # is quantised to integers and jumps in steps of a full run. What a book
    # hangs is a HALF-POINT line, so the comparable quantity is the same one
    # `market_total` reads out of the book: the half-point line whose over and
    # under sit closest to even money.
    totals = [r.runs_home + r.runs_away for r in res]
    t_mean = (ra + rh)
    lines = [x + 0.5 for x in range(0, 30)]
    fair_line = min(lines, key=lambda L: abs(price_over(totals, L) - 0.5))
    p_over = price_over(totals, fair_line)
    print(f"\n  projected score   {args.away} {ra:.2f} — {rh:.2f} {args.home}")
    print(f"  total             {t_mean:.2f} MEAN   |   fair line "
          f"{fair_line:.1f}  (P over {p_over:.3f})"
          f"   <- compare the LINE to a book's, never the mean")
    print(f"                     a book's number is the even-money point, and "
          f"runs are right-skewed, so it sits ~0.4 under the mean")
    print(f"  home win prob     {wins:.1%}   fair {_fmt(to_american(wins))}")

    print(f"\n  {'player':<24s} {'market':<22s} {'line':>5s} "
          f"{'mean':>6s} {'P(o)':>6s} {'over':>6s} {'under':>6s}")
    for row in price_board(proj):
        if row["mean"] < 0.02:
            continue
        print(f"  {row['player']:<24s} {row['market']:<22s} "
              f"{row['line']:>5.1f} {row['mean']:>6.2f} "
              f"{row['p_over']:>6.3f} {_fmt(row['fair_over']):>6s} "
              f"{_fmt(row['fair_under']):>6s}")


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "rates":
        rates_report()
    elif cmd == "calibrate":
        ap = argparse.ArgumentParser(prog="mlb_sim.py calibrate")
        ap.add_argument("--workers", type=int, default=None,
                        help="parallel bank builders (default: cores - 2)")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--sample", type=int, default=60000)
        a = ap.parse_args(argv[1:])
        calibrate_distance(a.season, a.sample, workers=a.workers)
    elif cmd == "project":
        project_cli(argv[1:])
    elif cmd == "clv":
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
                  f"fold {fold} (trained {ml_fold_span(fold)})")
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
                  f"bias {b['mean_diff']:+.2f} runs "
                  f"(median {b['median_diff']:+.2f}, "
                  f"model over on {b['over_share']:.0%})")
            if abs(b["mean_diff"]) > 0.25:
                print("  ** A standing bias this size IS the CLV number below. "
                      "Fix it before reading anything into the edge buckets. **")
        summary = summarize_clv(picks, a.edge)
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
    elif cmd == "calibrate-form":
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
        r = (calibrate_form_on_slate(a.season, reps=a.reps,
                                     workers=a.workers) if a.slate
             else calibrate_form(a.target, a.sims))
        print(f"\n  paste into mlb_sim.py:")
        print(f"    GAME_FORM_SD = {r['GAME_FORM_SD']:.4f}")
        print(f"    GAME_FORM_MEAN_SHIFT = {r['GAME_FORM_MEAN_SHIFT']:.4f}")
    elif cmd == "marks":
        ap = argparse.ArgumentParser(prog="mlb_sim.py marks")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        m = measure_real_marks(a.season, refresh=a.refresh)
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
    elif cmd == "dispersion":
        ap = argparse.ArgumentParser(prog="mlb_sim.py dispersion")
        ap.add_argument("--sims", type=int, default=6000)
        ap.add_argument("--season", type=int, default=2026)
        a = ap.parse_args(argv[1:])
        r = validate_dispersion(a.sims, season=a.season)
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
        print(f"\n  by lag (flat => shared per-game factor; "
              f"decaying => momentum):")
        for lag, c in sorted(r["by_lag"].items()):
            print(f"    lag {lag}  {c:+.5f}")
        print(f"\n  by window   (real: starter 1-5 +0.0135, "
              f"bullpen 6-8 +0.0316, spanning +0.0204)")
        for k, v in r["window"].items():
            print(f"    {k:12s} {v:+.5f}" if v is not None else
                  f"    {k:12s}      -")
    elif cmd == "calibrate-fatigue":
        ap = argparse.ArgumentParser(prog="mlb_sim.py calibrate-fatigue")
        ap.add_argument("--season", type=int, default=2026)
        ap.add_argument("--reps", type=int, default=12)
        ap.add_argument("--workers", type=int, default=None)
        a = ap.parse_args(argv[1:])
        calibrate_fatigue(a.season, reps=a.reps, workers=a.workers)
    elif cmd == "asof":
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
        got = fetch_boards_asof(dates, a.season, force=a.force)
        print(f"\nfetched {len(got)}; cached under {ASOF_DIR}")
    elif cmd == "backtest":
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
    elif cmd == "closing":
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
        r = clv_vs_closing(bt, a.season, edge=a.edge, price=a.price)
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
        print(f"\n  by disagreement with the close — a real edge GROWS with it;"
              f"\n  a flat profile with one good bucket is what noise looks like")
        for b in r["buckets"]:
            print(f"    {b['lo']:.0%}-{b['hi']:.0%}  n {b['n']:5d}   "
                  f"hit {b['hit']:.4f}   mkt fair {b['mkt_fair']:.4f}   "
                  f"model {b['model_implied']:.4f}   "
                  f"ROI {b['roi']:+.4f} +/- {b['roi_se']:.4f}")
    elif cmd == "forecastwx":
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
                build_park_weather_ref_om(season, 0)
    elif cmd == "clvopen":
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

            print(f"\n  MONEYLINE — CLV in de-vigged probability")
            show("all picks", _clv_summary(ml))
            print(f"\n  TOTALS — CLV in probability at the opening line, "
                  f"and in RUNS")
            show("all picks", _clv_summary([t for t in tot if t["priced"]]))
            show("line move", _clv_summary(tot, "clv_runs"), unit="runs")

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
            show2("MONEYLINE", _clv_summary(pooled_ml))
            show2("TOTALS", _clv_summary([t for t in pooled_tot if t["priced"]]))
            show2("TOTALS line move",
                  _clv_summary(pooled_tot, "clv_runs"), unit="runs")

            # What the de-vig is worth, shown rather than claimed. The raw row
            # is what this test would have reported without it.
            vr = vig_report(pooled_ml)
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
            print(f"\n  by disagreement with the OPEN — a real edge GROWS with "
                  f"it. A flat profile\n  with one good bucket is noise, "
                  f"however good that bucket looks (3d.1).")
            print(f"    {'moneyline':14s} {'n':>6s} {'CLV':>10s} {'se':>9s} "
                  f"{'t':>7s} {'our way':>9s}")
            for b in clv_open_buckets(pooled_ml):
                if not b["n"]:
                    continue
                print(f"    {b['lo']:.0%}-{b['hi']:.0%}".ljust(18)
                      + f"{b['n']:6d} {b['clv']:+10.5f} {b['se']:9.5f} "
                      f"{b['t']:+7.2f} {b['hit']:9.4f}")

        # --- the ablation table: every arm through the SAME harness --------
        if len(by_arm) > 1:
            print(f"\n\n  THE LOOK-AHEAD ABLATION — same games, same seeds, "
                  f"same scorer.\n  `base` holds the posted lineup and the "
                  f"OBSERVED game-time weather; the opening\n  price had "
                  f"neither. What survives in `nolook` is the part of the CLV "
                  f"a\n  pre-lineup, pre-weather projection actually earned.")
            print(f"\n    {'arm':10s} {'ML CLV':>10s} {'t':>7s} "
                  f"{'TOT CLV':>10s} {'t':>7s} {'line runs':>10s} {'t':>7s}")
            ref = None
            for k, (mrows, trows) in by_arm.items():
                a1 = _clv_summary(mrows)
                a2 = _clv_summary([t for t in trows if t["priced"]])
                a3 = _clv_summary(trows, "clv_runs")
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
                s1 = _clv_summary(nl)["clv"] / ref[0] if ref[0] else float("nan")
                s2 = _clv_summary(nt)["clv"] / ref[1] if ref[1] else float("nan")
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

        print(f"\n  CLV needs no game result, so its error bar is the one "
              f"quoted — but it is NOT\n  an edge on its own: a market can "
              f"move toward a model and still be right.")
    elif cmd == "stuff":
        ap = argparse.ArgumentParser(prog="mlb_sim.py stuff")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--no-score", action="store_true",
                        help="reliability only; skip the (slow) A/B against "
                             "the incumbent's predictions")
        a = ap.parse_args(argv[1:])
        seasons = a.season or [2025, 2026]
        names = ("K", "BB", "HBP", "GB_OUT", "AIR_OUT", "1B", "2B", "3B", "HR")
        for season in seasons:
            r = measure_stuff_reliability(season)
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
            s = score_stuff_prior(season)
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
    elif cmd == "diff":
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
                rows = differential_rows(bt, season)
                print_differential(score_differential(rows),
                                   f"{arm_name} {season}")
                pooled += rows
            if len(seasons) > 1:
                print_differential(score_differential(pooled),
                                   f"{arm_name} POOLED")

    elif cmd == "ab":
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
    elif cmd == "eventodds":
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
        print(f"per-event OPENING + CLOSING odds: moneyline, totals, run line, "
              f"and the first-five-innings scopes.\n"
              f"  ~17s/event per worker; resumable by event id, so an "
              f"interrupted run costs nothing.\n"
              f"  needs a non-US egress IP — a US one returns zero outcomes "
              f"while the event page still resolves.")
        seasons = a.season or [2026]
        got = {}
        for season in seasons:
            got = fetch_event_odds(season, limit=a.limit, workers=a.workers,
                                   timeout=a.timeout)
        # what did we actually get? A count of games is not a count of markets.
        # Reported for the LAST season fetched; each season prints its own
        # cached-vs-expected line as it finishes.
        n_tot = n_f5 = n_ml = 0
        for e in got.values():
            if event_totals(e, 1):
                n_tot += 1
            if event_totals(e, 2):
                n_f5 += 1
            if any(l.get("bt") == 3 and l.get("sc", 1) == 1
                   for l in e.get("lines", [])):
                n_ml += 1
        print(f"\n  of {len(got)} cached events: {n_ml} with a moneyline, "
              f"{n_tot} with whole-game totals, {n_f5} with F5 totals")
    elif cmd == "recency":
        ap = argparse.ArgumentParser(prog="mlb_sim.py recency")
        ap.add_argument("--season", action="append", type=int, default=None)
        ap.add_argument("--side", action="append", choices=("bat", "pit"),
                        default=None)
        a = ap.parse_args(argv[1:])
        hl = (150.0, 250.0, 500.0, 1000.0)
        for side in (a.side or ["pit", "bat"]):
            for season in (a.season or [2025, 2026]):
                r = measure_recency(side, season, hl)
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
    elif cmd == "stints":
        ap = argparse.ArgumentParser(prog="mlb_sim.py stints")
        ap.add_argument("--games", type=int, default=400)
        ap.add_argument("--refresh", action="store_true",
                        help="re-scrape the play-by-play (1,850 games)")
        a = ap.parse_args(argv[1:])
        if a.refresh:
            collect_reliever_stints(refresh=True)
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
    elif cmd == "aaa":
        ap = argparse.ArgumentParser(prog="mlb_sim.py aaa")
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        if a.refresh:
            measure_milb_translation()
        aaa_translation_report()
    elif cmd == "parkbuild":
        ap = argparse.ArgumentParser(
            prog="mlb_sim.py parkbuild",
            description="Per-outcome park factors and each player's measured "
                        "park exposure. Both read savedata/pa/v2/ and the "
                        "season slate; USE_PARK_DECONTAM needs both.")
        ap.add_argument("seasons", nargs="*", type=int, default=None)
        ap.add_argument("--reliability", type=float, default=0.70)
        a = ap.parse_args(argv[1:])
        for season in (a.seasons or [2023, 2024, 2025, 2026]):
            fac = build_park_outcome_factors(season, a.reliability)
            fp = park_outcome_path(season)
            with open(fp, "w") as fh:
                json.dump(fac, fh, indent=1)
            exp = build_player_park_exposure(season)
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
    elif cmd == "milb":
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
            collect_milb(seasons, refresh=a.refresh)
        # Same players, same season, same cache file — see
        # `collect_milb_statcast`. Hawk-Eye is Triple-A and FSL only; a
        # Double-A arm is untouched by this and stays on the level ladder.
        if not a.no_statcast:
            collect_milb_statcast(seasons, refresh=a.refresh or a.statcast_only)
        milb_report(max(seasons))
    elif cmd == "framing":
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
        measure_framing(a.season, a.upto, with_pitcher=not a.no_pitcher,
                        with_umpire=not a.no_umpire, out_path=a.out)
        if a.validate:
            framing_validate_report(a.season, a.out)
        if a.repeatability:
            framing_repeatability_report(a.season, a.split)
    elif cmd == "milbpark":
        ap = argparse.ArgumentParser(prog="mlb_sim.py milbpark")
        ap.add_argument("seasons", nargs="*", type=int, default=None)
        ap.add_argument("--refresh", action="store_true")
        a = ap.parse_args(argv[1:])
        seasons = a.seasons or [2024, 2025, 2026]
        collect_milb_park(seasons, refresh=a.refresh)
        milb_park_report(max(seasons))
    elif cmd == "milbasof":
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
            collect_milb_asof(cuts, season, force=a.force)
    elif cmd == "baserunning":
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
        baserunning_report(a.season, workers=a.workers)
    elif cmd == "re24":
        ap = argparse.ArgumentParser(prog="mlb_sim.py re24")
        ap.add_argument("--season", type=int, default=BASERUN_SEASON)
        ap.add_argument("--games", type=int, default=6000)
        a = ap.parse_args(argv[1:])
        re24_report(a.games, season=a.season)
    elif cmd == "boards":
        ap = argparse.ArgumentParser(prog="mlb_sim.py boards")
        ap.add_argument("seasons", nargs="+", type=int)
        ap.add_argument("--side", action="append", choices=("bat", "pit"),
                        default=None)
        ap.add_argument("--force", action="store_true")
        a = ap.parse_args(argv[1:])
        sides = tuple(a.side) if a.side else ("bat", "pit")
        print(f"full-season boards: {sides} x {a.seasons} "
              f"(needs headless Firefox)")
        got = fetch_season_boards(a.seasons, sides, force=a.force)
        print(f"\nfetched {len(got)}; cached under {SAVE_DIR}")
    elif cmd == "slate":
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
        print(f"\n  per-inning mean   (real inning 1 is the HIGHEST — 5.4)")
        print(f"    {'inning':8s}" + "".join(f"{i:>8d}" for i in range(1, 9)))
        print(f"    {'sim':8s}" + "".join(f"{x:8.3f}" for x in sim["by_inning"]))
        print(f"    {'real':8s}" + "".join(f"{x:8.3f}" for x in real["by_inning"]))
        print(f"    {'diff':8s}" + "".join(
            f"{s - t:+8.3f}" for s, t in zip(sim["by_inning"], real["by_inning"])))
        print(f"\n  by window   (real: starter 1-5, bullpen 6-8, spanning)")
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
    elif cmd in ("", "smoke"):
        smoke_test()
    else:
        print(__doc__)
        raise SystemExit(f"mlb_sim: unknown command {cmd!r}")



# ===========================================================================
# 13. CLV HARNESS — SCORING THE MODEL AGAINST MARKET MOVEMENT
# ===========================================================================
# Closing line value, not ROI. Over any sample a bettor can realistically
# collect, ROI is dominated by variance; CLV converges far faster and is the
# thing that actually says whether the model knows something the market did
# not yet know. This is the discipline the NHL work already settled on.
#
# **READ THIS BEFORE BELIEVING A BACKTEST NUMBER.** The rate layer
# (`build_rates`) reads SEASON-TO-DATE boards. Scoring a game from three
# months ago with those rates lets the model use information that did not
# exist when the line opened — a hitter's hot August is inside the rates used
# to "predict" his May game. That is look-ahead bias and it flatters the
# result. Two modes exist for that reason:
#
#   record  — snapshot today's projections against today's prices. Honest by
#             construction: nothing later than the fixture is in the model.
#             Score it after the games close. This is the real harness.
#   replay  — score past games with current rates. Optimistically biased, and
#             labelled as such wherever it prints. Useful as a smoke test of
#             the plumbing and of whether the model's structure moves WITH the
#             market at all; useless as an edge estimate.

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
    path = SAVE_DIR / "mlb_roster_2026.json"
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
    STADIUM_DATA = _wm().STADIUM_DATA
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


def american(dec: Optional[float]) -> Optional[int]:
    if not dec or dec <= 1.0:
        return None
    return round((dec - 1.0) * 100) if dec >= 2.0 else round(-100.0 / (dec - 1.0))


# ---------------------------------------------------------------------------
# Model prices for whole-game markets
# ---------------------------------------------------------------------------

def game_totals(results: Sequence[GameResult]) -> List[float]:
    return [float(r.runs_home + r.runs_away) for r in results]


def p_total_over(results: Sequence[GameResult], line: float) -> float:
    return price_over(game_totals(results), line)


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
    vals = game_totals(results)
    ladder = [lo + 0.5 * i for i in range(int((hi - lo) / 0.5) + 1)]
    prev_l, prev_p = ladder[0], price_over(vals, ladder[0])
    for L in ladder[1:]:
        p = price_over(vals, L)
        if p <= 0.5 <= prev_p and prev_p != p:
            return prev_l + (prev_p - 0.5) / (prev_p - p) * (L - prev_l)
        prev_l, prev_p = L, p
    return prev_l


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


def _half_inning_hist(results: Sequence[GameResult], side: str) -> Dict[str, int]:
    """{runs in a half-inning: count} pooled over a set of simulated games."""
    out: Dict[str, int] = {}
    for r in results:
        for v in (r.half_runs_home if side == "home" else r.half_runs_away):
            k = str(v)
            out[k] = out.get(k, 0) + 1
    return out


def club_quality_asof(season: int, save_dir: Path = SAVE_DIR,
                      cutoffs: Optional[Sequence[str]] = None
                      ) -> Dict[tuple, float]:
    """{(date, club): shrunk run differential per game} from PRIOR games only.

    **Two ways to be wrong here, and the first one bit.**

    *Leakage.* The entry is keyed by DATE but the loop runs per GAME, so on a
    doubleheader the second game's write includes the first game's result and
    overwrites the shared key — pricing game one with its own outcome partly
    baked in. 128 team-games in 2025, 76 in 2026, ~2%. `seen` freezes the
    entry at a club's FIRST game of a date.

    *Freshness.* Strictly-prior is legal but it is not automatically a fair
    A/B: every other input is frozen at the weekly as-of board cutoff, and a
    feature that updates DAILY is being handed fresher information than the
    model it is being added to. Pass `cutoffs` and the differential is frozen
    at the same cutoff the boards are, which is the only version that isolates
    what the FEATURE is worth from what its RECENCY is worth.

    Live, `cutoffs=None` is right — you really do know yesterday's score.
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
                        frozen[fk] = shrink_team_quality(got[0], int(got[1]))
                    out[key] = frozen[fk]
                else:
                    out[key] = shrink_team_quality(got[0], int(got[1]))
            acc[club] = [got[0] + diff, got[1] + 1]
    return out


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


def p_home_covers(results: Sequence[GameResult], handicap: float) -> float:
    """P(home + handicap > away). OddsPortal signs the handicap from the HOME
    side, so `-1.5` is the home side laying a run and a half."""
    margins = [(r.runs_home + handicap) - r.runs_away for r in results]
    live = [m for m in margins if m != 0]
    if not live:
        return 0.5
    return sum(1 for m in live if m > 0) / len(live)


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
    op = devig(open_dec)
    cp = devig(close_dec)
    if not all(op) or not all(cp):
        return None
    return ([o.name for o in outs], op, cp, open_dec, close_dec)


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
        pr = _pair_probs(line)
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
        add(tot, p_total_over(results, tot.handicap))

    ml = eo.main_line("home-away")
    if ml:
        add(ml, p_home_win(results))

    rl = eo.main_line("asian-handicap")
    if rl and rl.handicap is not None:
        add(rl, p_home_covers(results, rl.handicap))

    return picks


@dataclass
class TotalsBias:
    """Model total vs the market's total, per game.

    **Compare MEDIANS, never the mean.** A book hanging 8.0 at even money is
    stating that P(over 8.0) = 0.5 — a MEDIAN. Runs per game are strongly
    right-skewed (a 15-run blowout drags the mean up but moves the median
    barely), and in this engine the gap is **+0.75 runs**: simulated mean 8.75
    against a simulated median of 8.00.

    Comparing our mean to their line therefore reads as a standing +0.6 to
    +0.75 "bias" that does not exist. It cost a full diagnostic pass here:
    the model's median is 8.00 against a Bovada median line of 8.00, and MLB's
    actual MEAN is 8.96 — every one of those is consistent, and only the
    mean-vs-median comparison looked broken.

    `model_total` is the model's median, i.e. the line at which it would price
    the game pick'em. Note the CLV picks themselves were never affected —
    they price `p_total_over(results, line)` at the market's own number, which
    is the correct comparison whatever the skew.
    """
    game: str
    model_total: float          # MEDIAN simulated total
    market_total: float
    model_mean: Optional[float] = None   # kept for reference only

    @property
    def diff(self) -> float:
        return self.model_total - self.market_total


def summarize_bias(rows: Sequence[TotalsBias]) -> dict:
    if not rows:
        return {"n": 0}
    d = [r.diff for r in rows]
    d_sorted = sorted(d)
    return {
        "n": len(d),
        "mean_model": sum(r.model_total for r in rows) / len(rows),
        "mean_market": sum(r.market_total for r in rows) / len(rows),
        "mean_diff": sum(d) / len(d),
        "median_diff": d_sorted[len(d) // 2],
        "over_share": sum(1 for x in d if x > 0) / len(d),
    }


def fade_correlation(picks: Sequence[ClvPick]) -> Optional[float]:
    """Is the model finding edges, or just fading whatever the market says?

    Correlates each pick's claimed edge against how far the market's own price
    sits from the middle. A model with game-specific insight shows ~0 here: its
    disagreements land wherever the information is. A model whose outputs are
    COMPRESSED shows a strongly negative number, because it reverts everything
    to the league mean and therefore takes the under on every high total, the
    over on every low one, and every underdog on the moneyline.

    **Check this before reading an edge board.** Measured on one real slate:
    totals -0.650, moneyline -0.887, run line +0.703 — every market dominated
    by a systematic fade, i.e. the edge list was a readout of the model's own
    narrow spread rather than of market error. An "edge" that big and that
    correlated is a defect, not a bet.
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
           "fade": fade_correlation(picks)}
    by_market = {}
    for p in picks:
        by_market.setdefault(p.market, []).append(p)
    out["by_market"] = {k: agg(v) for k, v in by_market.items()}
    return out


# ---------------------------------------------------------------------------
# Running a slate
# ---------------------------------------------------------------------------

def _op_client(proxy: Optional[str] = None):
    from OddsPortalClient import OddsPortalClient
    return OddsPortalClient(proxy=proxy)


# **`/matches/baseball/` is the wrong page for an MLB slate.** It is the
# sport-wide "today and upcoming" listing: at 02:00 local it carried seventeen
# rows from the day BEFORE (finished games), sixteen from the current day, and
# of those only ONE was major-league — the rest Czech Extraliga, NPB and
# Triple-A. Priced blind that produces a board of games whose probables do not
# exist, every side falling back to the season board's best nine.
#
# The LEAGUE page carries the real slate: `/baseball/usa/mlb/` returned all
# fifteen of the current day's games. Same parser, different path.
#
# **But it is a FIXTURES page, and a finished game leaves it.** Measured
# 2026-08-23, with all fifteen of that day's games already Final on StatsAPI:
# `/baseball/usa/mlb/` returned FOUR rows, the nearest a week out, while
# `/baseball/usa/mlb/results/` carried 50 finished games back through 08-19.
# So `--date` accepted any past date and could never resolve one — the board
# came back empty and `run_clv` reported "nothing on the board", which reads
# as "no games that day" rather than "this path cannot see finished games".
# A flag that looks like it works and silently produces nothing is the same
# family as trap 11; the fix is to read BOTH pages.
#
# Both are merged rather than switched on the date, because a slate in
# progress is genuinely mixed: the 1:05 games are on the results page while
# the 7:05 games are still fixtures, and either page alone is a partial board
# that looks complete. `date-start-timestamp` is what actually selects the
# slate, and it is on rows from both.
_OP_LEAGUE_PATH = {"baseball": "/baseball/usa/mlb/"}
_OP_RESULTS_PATH = {"baseball": "/baseball/usa/mlb/results/"}


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
    paths = [p for p in (_OP_LEAGUE_PATH.get(sport),
                         _OP_RESULTS_PATH.get(sport) if finished else None)
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


def slate_games(sport: str = "baseball", proxy: Optional[str] = None,
                date: Optional[str] = None) -> List[dict]:
    """Today's board from OddsPortal, joined to our club abbreviations.

    Rows whose clubs do not resolve are dropped with a count rather than
    silently skipped — an unresolved club is usually a name-map drift, and a
    harness that quietly scores 12 of 15 games looks identical to one that
    scored all 15.
    """
    c = _op_client(proxy)
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
    # `slate_games` with no date returns whatever OddsPortal's listing is
    # showing, which at 02:00 local was seventeen games from 2026-08-20 — games
    # already PLAYED — mixed with Triple-A and foreign leagues, plus sixteen
    # from the 21st. `fetch_probables` meanwhile defaulted to today. Three of
    # the four MLB games that resolved were therefore priced against a slate
    # whose probables did not exist, so every one of them silently fell back to
    # the board's best-nine-by-PA and no real starter — and the run reported
    # `real starters used on 2/8 sides` and carried on.
    #
    # One date, passed to both.
    date = date or datetime.date.today().isoformat()
    games = slate_games(sport, proxy, date=date)
    if limit:
        games = games[:limit]
    if not games:
        if verbose:
            print(f"[clv] nothing on the {date} board")
        return [], {}

    bat_table, _ = build_rates("bat")
    pit_table, _ = build_rates("pit")
    hz = starter_hazard()
    c = _op_client(proxy)

    probables: Dict[tuple, dict] = {}
    if live_lineups:
        try:
            # doubleheader-aware: keys carry the game number, so look up
            # through `probable_for` rather than indexing (away, home)
            probables = fetch_probables(date)
        except Exception as e:
            print(f"[clv] probables unavailable ({e}) — falling back to "
                  f"season-board starters, which biases totals high")
    subs = {"sp": 0, "lineup": 0, "games": 0}

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
            subs["lineup"] += int(uh["lineup"]) + int(ua["lineup"])
        except Exception as e:
            if verbose:
                print(f"[clv] sides failed {g['label']}: {e}")
            continue
        # **Venue and weather were NOT being passed, and that is not cosmetic.**
        # Every game on the live board was priced at a NEUTRAL park with no
        # conditions: `weather_tilt(None, None)` and `park_run_tilt(None, ...)`
        # both return 0, so Coors and Petco got the same run environment. It
        # showed up as the model sitting 1.31 runs under the market on
        # CLE @ COL — the largest disagreement on the slate, and it was the
        # model not knowing where the game was.
        venue = resolve_venue(pr.get("venue") or "") or (g.get("venue") or None)
        wx = None
        if pr.get("game_pk"):
            try:
                wx = game_weather(int(pr["game_pk"]), date)
            except Exception:                                  # noqa: BLE001
                wx = None
        # Scheduled game -> no observation exists yet; use the forecast rather
        # than pricing at a neutral park. See `forecast_game_weather`.
        if wx is None:
            wx = forecast_game_weather(venue, pr.get("start"))
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
        got = clv_picks_for_game(res, eo, g["label"])
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
        print(f"[clv] real starters used on {subs['sp']}/{2*subs['games']} sides, "
              f"posted lineups on {subs['lineup']}/{2*subs['games']}")
    return picks, {"clv": summarize_clv(picks), "bias": summarize_bias(bias),
                   "subs": subs}



# ---------------------------------------------------------------------------
# Tonight's actual probables and posted lineups
# ---------------------------------------------------------------------------
# `build_side` reads season boards and hands every club its highest-GS arm in
# every game. That is fine offline and wrong on a slate: a two-ace matchup and
# a bullpen game get the same starter, so the model cannot see the pitching
# matchup at all. On one measured slate it left totals +0.45 runs high and
# priced SD @ CLE at 8.41 against a market of 7.0 — a game the market had low
# precisely because of who was starting.

STATSAPI = "https://statsapi.mlb.com/api/v1"


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


# --- PROJECTED lineups, for the hours before the real ones are posted ------
# MLB publishes no projection: StatsAPI's `lineups` hydrate is EMPTY until a
# club files its card, which on a 7pm slate is mid-afternoon. So a model run in
# the morning has `USE_POSTED_LINEUP` on and nothing to use it with, and falls
# back to the board's best-nine-by-PA — which is POSITIVELY SELECTED (5.6a) and
# hands the club a better offence than the one that will actually play.
#
# Rotowire's beat writers post an expected nine per club all day, with batting
# order, position and bat side. `GUIMLBlineups.fetch_daily_lineups` has been
# scraping that page for the team-news widget all along and imports nothing but
# `requests` and `bs4`, so this costs no new scraper and keeps `mlb_sim`
# headless.
#
# **A projection is not a posted lineup and the difference is recorded**, never
# folded in silently: `probable_for` marks the row `lineup_source` "posted" or
# "projected", because a silent substitution is indistinguishable from having
# used the real thing (the standing rule in `_game_side`).
USE_PROJECTED_LINEUP = True


ROTOWIRE_LINEUPS_URL = "https://www.rotowire.com/baseball/daily-lineups.php"
_ROTO_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),?\s+(20\d\d)")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def rotowire_lineup_date(timeout: float = 20.0) -> Optional[str]:
    """The date the Rotowire lineups page is actually showing, ISO, or None.

    **The page is day-of and carries no date parameter**, so which slate it
    describes is a fact about when it was fetched, not about what was asked
    for. On 2026-08-21 the club pairings for the 21st and the 22nd were
    IDENTICAL — a series — so the matchup set cannot disambiguate, and a
    projection silently applied to the wrong day of a series is a whole
    lineup's worth of wrong data with nothing to notice it by. The page
    prints its own date; this reads it.
    """
    try:
        r = requests.get(ROTOWIRE_LINEUPS_URL, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        mm = _ROTO_DATE.search(r.text)
        if not mm:
            return None
        return (f"{mm.group(3)}-{_MONTHS.index(mm.group(1)) + 1:02d}"
                f"-{int(mm.group(2)):02d}")
    except Exception:                                          # noqa: BLE001
        return None


def projected_lineups(date: Optional[str] = None,
                      timeout: float = 20.0) -> Dict[str, List[tuple]]:
    """{club abbr: [(name, pos, bats), ...]} in batting order, or {}.

    `date` is CHECKED, not requested: if the page is showing a different day
    the projection is refused rather than served for the wrong slate.

    Club codes are normalised onto the FanGraphs board's spelling on WRITE,
    not on query — the seven-club disagreement (SF/SFG, TB/TBR, ...) is the
    silent-key-miss trap recorded in §7.2.
    """
    if date:
        shown = rotowire_lineup_date(timeout)
        if shown and shown != date:
            print(f"mlb_sim: Rotowire is showing {shown}, not {date} — "
                  f"no projected lineups for this slate")
            return {}
    try:
        from GUIMLBlineups import fetch_daily_lineups
        matchups = fetch_daily_lineups() or []
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


UNRESOLVED_BATTER = -1


def resolve_projected_lineup(abbr: str, players: Sequence[tuple],
                             season: int = 2026,
                             save_dir: Path = SAVE_DIR,
                             min_resolved: int = 7) -> List[int]:
    """Rotowire display names -> MLBAM ids, in batting order.

    Rotowire abbreviates most first names ('C. DeLauter'), so this matches on
    SURNAME plus first INITIAL inside that club's board rows, then narrows a
    tie by bat side, then by position, then by playing time. A name that
    survives all three ambiguous returns `UNRESOLVED_BATTER` rather than a
    guess — `_game_side` turns that into a replacement-level hitter, which is
    the honest answer for a man the board has never seen.

    Returns [] when fewer than `min_resolved` of the nine resolve, because at
    that point the projection is worse than the board's own best nine.
    """
    pool = team_roster("bat", season, save_dir).get(abbr) or []
    wide = load_board("bat", season, save_dir) or []
    idx: Dict[Tuple[str, str], List[dict]] = {}
    for row in pool:
        idx.setdefault(_name_key(row.get("PlayerName") or ""), []).append(row)

    def hits(rows, last, first):
        out = []
        for r in rows:
            l, f = _name_key(r.get("PlayerName") or "")
            if l != last:
                continue
            if f[:1] == first[:1] if len(first) <= 1 else f == first:
                out.append(r)
        return out

    ids: List[int] = []
    for name, pos, bats in players[:9]:
        last, first = _name_key(name)
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


def fetch_probables(date: Optional[str] = None, timeout: float = 20.0
                    ) -> Dict[tuple, dict]:
    """{(away_abbr, home_abbr): {...}} for one date, from MLB StatsAPI.

    Abbreviations are mapped into the FanGraphs board's spelling, since the
    two disagree on seven clubs. Free, no key, one request for the slate.
    """
    if date is None:
        date = datetime.date.today().isoformat()
    url = (f"{STATSAPI}/schedule?sportId=1&date={date}"
           f"&hydrate=probablePitcher,lineups,team")
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    def abbr(side_team: dict) -> str:
        a = (side_team.get("team") or {}).get("abbreviation") or ""
        return _FG_ALIAS.get(a, a)

    # **Keyed on (away, home) — which a DOUBLEHEADER collides on.** The dict
    # silently kept whichever game was parsed last, so the live path could never
    # price game ONE of a doubleheader; it priced game two under game one's key
    # and reported success. `game_number` is now part of the key, and a bare
    # (away, home) lookup resolves to game 1 via `probable_for()` so existing
    # callers keep working and get the EARLIER game rather than an arbitrary one.
    #
    # Note `odds_by_game` already handles this collision, by DROPPING ambiguous
    # keys — one place in this module knew about doubleheaders and the other
    # did not.
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
                "home_catcher": _lineup_catcher(lu.get("homePlayers")),
                "away_catcher": _lineup_catcher(lu.get("awayPlayers")),
            }
    # **The projection fills only what StatsAPI left EMPTY**, and only when
    # the club has not filed. A posted nine is always preferred: it is the
    # real card, the projection is a forecast of it, and overwriting one with
    # the other would be a downgrade wearing the label of a fallback.
    if USE_PROJECTED_LINEUP and any(
            len(r.get(f"{s}_lineup") or []) < 9
            for r in out.values() for s in ("home", "away")):
        proj = projected_lineups(date)
        if proj:
            season = int(date[:4])
            cache: Dict[str, List[int]] = {}
            for key, row in out.items():
                aa, ha = key[0], key[1]
                for side, abbr in (("home", ha), ("away", aa)):
                    if len(row.get(f"{side}_lineup") or []) >= 9:
                        continue
                    if abbr not in cache:
                        cache[abbr] = resolve_projected_lineup(
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
    url = (f"{STATSAPI}/schedule?sportId=1&date={date}"
           f"&hydrate=weather&gameType=R")
    data = requests.get(url, timeout=timeout).json()
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


# An OPENER is a reliever making the start, and he must not inherit the
# starter hook curve. San Diego started Wandy Peralta on 2026-08-15: 53 G / 4
# GS, `bf_per_outing` 4.83 — and the sim ran him 22.3 batters, 15.4 outs, 5.14
# IP, because `build_side_live` handed the named starter the generic
# `hook_hazard([18, 20, 21, ...])` regardless of who he is.
#
# Detected on the board rather than guessed: a starter whose GS/G is below this
# is a reliever taking the ball, and his hook comes from HIS OWN measured
# `bf_per_outing`.
OPENER_GS_SHARE = 0.5

# Shape of a short outing, mean 5.0 BF, rescaled to the arm's own mean.
# **Hand-drawn, and the real distribution is on disk.** Its mean is 5.00
# against a real 6.23 over 222 opener-length starts (<=10 BF) and its sd 1.47
# against 2.31 — the same too-short, too-tight stand-in as the starter curve
# one function over. Kept only as the fallback for a checkout with no stint
# cache; `_opener_bf_shape()` prefers the measured one.
_OPENER_BF_SHAPE = (3, 3, 4, 4, 4, 5, 5, 5, 6, 6, 7, 8)
_OPENER_SHAPE: List[Sequence[int]] = []


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

_GS_SHARE: Dict[int, Dict[int, float]] = {}
_PIT_ROWS: Dict[tuple, Dict[int, dict]] = {}


def _pit_row(pid: int, season: int, save_dir: Path) -> Optional[dict]:
    """One pitcher's board row, by id. Indexed, not scanned."""
    key = (int(season), str(save_dir))
    tab = _PIT_ROWS.get(key)
    if tab is None:
        tab = {}
        for row in load_board("pit", season, save_dir) or []:
            rid = _row_id(row)
            if rid:
                tab[rid] = row
        _PIT_ROWS[key] = tab
    return tab.get(int(pid))


def starter_gs_share(pid: Optional[int], season: int = 2026,
                     save_dir: Path = SAVE_DIR) -> Optional[float]:
    """GS / G off the pitching board. None when the arm is not on it."""
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
# 121 PURE starters on the 2026 board (G == GS, so the relief netting below is
# identically zero and the estimate is exact) span **4.10 to 6.60 IP/start**,
# median 5.46, p95 6.05. The shipped clamp ceiling was 7.00 — above anything a
# real starter does — and it CLAMPED rather than refused, so an arm whose
# netting produced 16.10 IP/start was quietly served as a 7-inning starter.
START_IP_CEILING = 6.6
START_IP_FLOOR = 0.7            # a true opener legitimately goes ~1 inning

# `ip_rel` is netted out of season IP and the remainder divided by GS, so an
# error in it is amplified by **(G - GS) / GS** — up to 59x on the 2026 board,
# where 86 of 335 arms sit above 3x. Only 34.8% of board pitchers carry a
# measured `ip_per_outing`; the rest take a 1.0 default, and at high leverage
# that guess cannot carry the estimate. Above this, refuse.
START_NET_MAX_LEVERAGE = 1.0

# Median batters faced in a START, by the pitcher's own GS share, over 3,728
# cached starts in `reliever_stints.json`.
#
#   GS share < 0.15   n=168   median  6.0 BF   <- true opener / swingman
#   0.15 - 0.30       n= 41   median 21.0
#   0.30 - 0.50       n=177   median 21.0
#   0.50 - 0.75       n=221   median 21.0
#   0.75 +            n=3120  median 23.0
#
# **The step is at ~0.15, not at `OPENER_GS_SHARE`'s 0.5.** Arms between 0.15
# and 0.50 throw ordinary 21-batter starts; only below 0.15 does the length
# collapse. That is the population split the old docstring was reaching for
# with "a starter pulled by the 2nd averages inning 2.13 while a spot starter
# goes to inning 5.23", measured here on the cached stints instead.
START_BF_BY_GS_SHARE: Tuple[Tuple[float, float], ...] = (
    (0.15, 6.0), (0.75, 21.0), (2.0, 23.0))


def population_start_bf(pid: Optional[int], season: int = 2026,
                        save_dir: Path = SAVE_DIR) -> float:
    """What an arm with THIS GS share throws in a start, measured.

    The fallback when his own line cannot resolve it. Deliberately not his
    relief `bf_per_outing`: `start_bf_estimate`'s own docstring says the
    relief workload is the wrong number for a start, and then the call site
    used it as the fallback anyway — which is how a man with four real starts
    was handed a one-inning target.
    """
    share = starter_gs_share(pid, season, save_dir)
    if share is None:
        return START_BF_BY_GS_SHARE[-1][1]
    for hi, bf in START_BF_BY_GS_SHARE:
        if share < hi:
            return bf
    return START_BF_BY_GS_SHARE[-1][1]


def start_bf_estimate(pid: Optional[int], season: int = 2026,
                      save_dir: Path = SAVE_DIR) -> Optional[float]:
    """Batters this arm faces in a START, from his own IP/GS on the board.

    **His RELIEF workload is the wrong number for this.** IP is shared with
    his relief work: Wandy Peralta has 61 IP over 53 G but only 4 GS, so a
    naive ratio calls him a 15-inning starter. The relief innings are netted
    out first, using his own measured relief length.

    **Returns None rather than a number it cannot stand behind.** The netting
    divides by GS, so an error in `ip_rel` is multiplied by (G - GS) / GS. For
    Lake Bachar — G 41, GS 4, leverage 9.2x — his true 1.59 IP per relief
    outing against the assumed 1.00 became a **5.5 IP per start** overstatement,
    and the old clamp turned 7.05 into a 7-inning start: 30.1 batters, the
    longest projected start on the 2026-08-23 slate, ahead of an arm with 646
    TBF and 25 starts. Two other arms computed NEGATIVE innings per start
    (-4.00, -1.80) and were clamped up to 0.7.

    A clamp is the wrong instrument here. It converts "this arithmetic did not
    resolve" into "this man throws a complete game", which is a plausible
    wrong answer with nothing attached to say so — `sim_state.md` trap 11. The
    caller falls back to `population_start_bf`, which is measured.
    """
    if pid is None:
        return None
    row = _pit_row(int(pid), season, save_dir)
    if row is None:
        return None
    gs, g, ip = _num(row, "GS"), _num(row, "G"), _innings(row, "IP")
    if gs < 1 or ip <= 0:
        return None
    relief = max(0.0, g - gs)
    tr = load_reliever_traits(season).get(int(pid)) or {}
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
    shape = _opener_bf_shape()
    base = statistics.mean(shape) if shape else 5.0
    scale = max(0.4, float(bf_target or 4.5)) / base
    return hook_hazard([max(1, round(b * scale)) for b in shape])


def build_side_live(abbr: str, bat_table: Dict[int, dict],
                    pit_table: Dict[int, dict], *,
                    sp_id: Optional[int] = None,
                    lineup_ids: Optional[Sequence[int]] = None,
                    catcher_id: Optional[int] = None,
                    season: int = 2026,
                    hazard: Optional[List[float]] = None,
                    save_dir: Path = SAVE_DIR,
                    use_itp_pen: bool = True):
    """A TeamSide using tonight's ACTUAL starter, posted lineup and BULLPEN.

    Falls back to `build_side`'s season-board choices for whatever is missing —
    a rookie with no board rows, or a lineup that has not posted yet — and
    reports which parts were substituted, because a silent fallback is
    indistinguishable from having used the real thing.

    **The pen comes from insidethepen, not the season board.** The board gives
    the UNION of every reliever a club used all year (24.2 arms); the real pen
    is 8, and for Oakland only 4 of its 8 current arms were even on the board
    list the sim had been using. Arms resting on real recent workload are
    dropped here, which is the point — see `build_pen_from_itp`.
    """
    side = build_side(abbr, bat_table, pit_table, season, hazard, save_dir)
    used = {"sp": False, "lineup": False, "pen": "board", "framing": "club"}

    # **Tonight's actual catcher, not the club's season average.** Only when
    # the pitch-level series is on — the Savant CSV is club-level and has no
    # per-catcher figure to reach for. Lagged by `TEAM_CONTEXT_LAG` like every
    # other team-context term; lagging a CATCHER is legitimate because his
    # skill travels with him, which a club aggregate's does not.
    if catcher_id is not None:
        side.catcher_id = int(catcher_id)
        if USE_PITCH_FRAMING:
            v = catcher_framing_per_game(int(catcher_id),
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
        traits = load_reliever_traits(season).get(int(sp_id)) or {}
        is_opener = share is not None and share < OPENER_GS_SHARE
        # His own start length where his line can resolve one, else what arms
        # with HIS GS SHARE actually throw — measured, not his relief outing.
        #
        # **The old chain fell back to `traits["bf_per_outing"]`, which is a
        # RELIEF length**, the very number `start_bf_estimate`'s docstring
        # says is wrong for a start; and then to a bare 4.5. So the two
        # outcomes were a 7-inning start (the clamp) or a one-inning one (the
        # fallback), with nothing in between and no measurement behind either.
        bf_target = (start_bf_estimate(sp_id, season, save_dir)
                     or population_start_bf(sp_id, season, save_dir))
        hz = opener_hazard(bf_target) if is_opener else (hazard or [])
        sp = make_pitcher(int(sp_id), pit_table, is_starter=True, hazard=hz)
        if sp is None:
            # **A DEBUT has no board row, so `make_pitcher` returns None and the
            # side silently keeps the club's board starter — its BEST arm.**
            # Kade Anderson's 2026-08-22 debut was priced as Logan Gilbert,
            # worth 4.1 points of win probability on that game. The minor
            # league ladder can say something about him where the board cannot,
            # so it is asked before the fallback is accepted.
            sp = milb_only_pitcher(int(sp_id), season, save_dir,
                                   is_starter=True, hazard=hz)
        if sp is not None:
            side.starter = sp
            used["sp"] = True
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
# Every number a bullpen decision needs, MEASURED per arm and written to
# `MLBAnalytics/reliever_traits_<season>.csv` alongside the other stat tables
# in that directory. The sim reads them; `validate_bullpen_usage()` then
# checks that what the sim PRODUCES matches what the file says the pitcher
# actually did. A trait that is loaded but not reproduced is not modelled.
#
# The traits are deliberately all ratios the board already contains — nothing
# here is chosen:
#   app_rate      G / team games         how often he pitches at all
#   bf_per_outing TBF / G                how long he stays
#   ip_per_outing IP / G                 one-inning arm vs long man
#   gm_li         gmLI                   the leverage he is used in
#
# Real league marks for sanity: appearance rate median 10.8%, p90 41.2%,
# **max 53.4%**. Any simulated arm above ~50% is wrong by construction.

MLBA_DIR = Path(__file__).resolve().parent.parent / "MLBAnalytics"
RELIEVER_TRAIT_COLS = ("player_name", "player_id", "team", "season",
                       "g", "team_games", "app_rate", "bf_per_outing",
                       "ip_per_outing", "gm_li", "sv", "hld", "siera")


def _team_games(season: int = 2026, save_dir: Path = SAVE_DIR) -> Dict[str, float]:
    """Games played per club, taken as the max G on the batting board."""
    out: Dict[str, float] = {}
    for r in load_board("bat", season, save_dir) or []:
        t = r.get("TeamNameAbb")
        if t and "Tms" not in str(t):
            out[t] = max(out.get(t, 0.0), _num(r, "G"))
    return out


def export_reliever_traits(season: int = 2026,
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
                    lambda i: fetch_itp_traits(i, sess), ids)):
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


_TRAITS: Dict[int, Dict[int, dict]] = {}


def load_reliever_traits(season: int = 2026) -> Dict[int, dict]:
    """{mlbam_id: traits} from the CSV, generated on first use if absent."""
    if season in _TRAITS:
        return _TRAITS[season]
    path = MLBA_DIR / f"reliever_traits_{season}.csv"
    if not path.exists():
        export_reliever_traits(season)
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



# --- InsideThePen deployment traits ----------------------------------------
# The FanGraphs board says how OFTEN and in what LEVERAGE an arm is used. It
# does not say the things a manager actually decides on, which insidethepen
# publishes per pitcher as "Advanced Pitcher Traits":
#
#   Avg Inning when called     when he enters
#   Avg Run Diff when called   the score context he is trusted in
#   back to back days          whether he can go on no rest  <- availability
#   over 30 pitches            workload capacity
#   before the 8th             role
#   versus LH / RH batters     whether he is a specialist
#
# `EffortMLB.fetch_reliever_page_sync` already fetches these live, but
# importing that module pulls in Qt, so the fetch is duplicated here in
# Qt-free form and the RESULT is written to the CSV. The CSV is the interface;
# the sim never touches the network.
#
# `MLBAnalytics/MLBstats/BPdata/` holds an earlier snapshot of the same traits
# (per pitcher, per date, 2025) scraped by `MLBAnalytics/penski.py`.

ITP_BASE = "https://insidethepen.com"
ITP_TRAIT_LABELS = (
    "Games Pitched this Season", "Games Started this Season",
    "versus LH batters", "versus RH batters",
    "Avg Inning when called", "Avg Run Diff when called",
    "over 30 pitches", "before the 8th", "back to back days",
)
ITP_TRAIT_COLS = ("itp_role", "itp_ip7", "itp_avg_inning", "itp_avg_run_diff",
                  "itp_back_to_back", "itp_over_30", "itp_before_8th",
                  "itp_vs_lh", "itp_vs_rh")


ITP_COOKIES_FILE = SAVE_DIR / "itp_cookies.json"
_ITP_SESSION = []          # at most one; a list so it survives re-import
_ITP_LOCK = __import__("threading").Lock()


def _itp_session():
    """ONE logged-in session, cookies persisted, reused for the process.

    **This used to log in on every call**, two HTTP round-trips per bullpen,
    and `fetch_itp_bullpen` had no cache — so pricing a 15-game slate twice
    was 180 requests, and a day of A/B sweeps ran to four figures. That is
    what times insidethepen out, and the timeouts then read as "bullpen
    unknown" and silently fell back to the season board.

    `EffortMLB` already had this right: one module-level session behind a
    lock, cookies persisted to `itp_cookies.json`, login only when the jar is
    empty or stale. This mirrors it rather than inventing a second scheme —
    they share the same cookie file, so a login in either warms both.
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
        _itp_login_into(s)
        _ITP_SESSION.append(s)
        return s


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
            r = s.get(f"{ITP_BASE}/login.html", timeout=20)
            tok = BeautifulSoup(r.content, "lxml").find(
                "input", {"name": "csrf_token"})
            r2 = s.post(f"{ITP_BASE}/login.html", timeout=20, data={
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


def _yn(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    t = str(v).strip().lower()
    return 1.0 if t.startswith("y") else (0.0 if t.startswith("n") else None)


def fetch_itp_traits(pid: int, session=None) -> dict:
    """One reliever's deployment traits from insidethepen. {} on any failure."""
    s = session or _itp_session()
    out: dict = {}
    try:
        r = s.get(f"{ITP_BASE}/pitcher/x-{pid}.html", timeout=20)
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
        out["itp_back_to_back"] = _yn(traits.get("back to back days"))
        out["itp_over_30"] = _yn(traits.get("over 30 pitches"))
        out["itp_before_8th"] = _yn(traits.get("before the 8th"))
        out["itp_vs_lh"] = _yn(traits.get("versus LH batters"))
        out["itp_vs_rh"] = _yn(traits.get("versus RH batters"))
    except Exception as e:
        print(f"[itp] {pid}: {e}")
    return {k: v for k, v in out.items() if v is not None}



# ---------------------------------------------------------------------------
# The REAL bullpen state — insidethepen's per-team page
# ---------------------------------------------------------------------------
# **This is the authority on pen composition and workload, and it replaces
# reconstructing either.** `/team/<ABBR>-bullpen.html` is UNGATED and carries,
# for every club:
#
#   * the CURRENT bullpen — 7-8 arms, which is what a club actually carries.
#     Deriving it from the FanGraphs season board instead gives the UNION of
#     every pen a club used all year (24.2 arms), which is not a bullpen and
#     puts a July call-up in an April game;
#   * a SEVEN-DAY per-day workload grid — innings, batters faced and PITCH
#     COUNTS per arm per date. That is the availability state directly: an arm
#     who threw 28 pitches yesterday is not going today, and no season-average
#     appearance frequency can express that.
#
# `/bullpen-availability-today.html` has it pre-digested with a Status column
# ("Available" / "Likely Rest") and a fatigue score, but **28 of 30 teams are
# premium-gated** there, so the team pages are the route that actually works.
#
# Note the scope: this is TODAY's state, so it serves live projections. A
# BACKTEST over past dates still needs the `appearance_dates` reconstruction,
# which is why both paths exist.
ITP_TEAM_URL = ITP_BASE + "/team/{abbr}-bullpen.html"
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


def itp_bullpen_cache_path(abbr: str, date: Optional[str] = None,
                           save_dir: Path = SAVE_DIR) -> Path:
    d = date or datetime.date.today().isoformat()
    return Path(save_dir) / "itp" / d / f"{normalize_club(abbr)}.json"


def load_itp_bullpen(abbr: str, date: Optional[str] = None,
                     save_dir: Path = SAVE_DIR,
                     session=None, timeout: float = 25.0,
                     refresh: bool = False) -> dict:
    """One club's bullpen, fetched at most ONCE PER DAY.

    A bullpen page changes when the club plays, so the natural cache key is
    the DATE — anything finer is re-fetching the same page. Without this every
    `build_side_live(use_itp_pen=True)` hit the site live, so pricing a slate
    twice was 60 fetches and a day of sweeps ran to four figures, which is
    what produced the read timeouts.

    **A timeout is written to the cache as a MISS, not as an empty pen.**
    `fetch_itp_bullpen` returns {} on failure and its docstring is explicit
    that callers must read that as "unknown", never "everyone is available" —
    so a failed fetch must not be cached as though it were an answer.
    """
    path = itp_bullpen_cache_path(abbr, date, save_dir)
    if path.exists() and not refresh:
        try:
            got = json.loads(path.read_text())
            if got.get("pen"):
                return got
        except Exception:
            pass
    got = fetch_itp_bullpen(abbr, session=session, timeout=timeout)
    if got.get("pen"):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(got))
        tmp.replace(path)              # atomic: a killed write cannot leave a
                                       # half-parsed bullpen behind
    return got


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
        r = s.get(ITP_TEAM_URL.format(abbr=ab), timeout=timeout)
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
                # not ("Edwin Díaz"), so a raw-string lookup silently missed
                # every tagged arm — which is to say every CLOSER, the most
                # important pitcher in the pen. Díaz threw 26 pitches on one
                # day and 24 the next and still read "available".
                workload[_norm_name(_itp_clean_name(c[0]))] = per
    return {"pen": pen, "workload": workload, "days": days} if pen else {}


# Pitch-count thresholds for next-day availability. `back to back days` on an
# arm's own insidethepen page says whether he is USED that way at all; these
# gate on what he actually threw.
ITP_HEAVY_PITCHES = 25          # a heavy outing yesterday -> very likely rest
ITP_BACK_TO_BACK_PITCHES = 15   # a light one -> he can go again


def itp_availability(state: dict, skip_today: bool = True) -> Dict[str, str]:
    """{pitcher name: 'available' | 'likely_rest'} from real recent workload.

    Reads the seven-day grid rather than a season-average frequency:
      * threw on each of the last two days -> rest (three straight is not a
        thing — measured, not one Oakland reliever did it all season);
      * threw a heavy outing yesterday -> rest;
      * threw a light one yesterday -> available.

    **The grid's FIRST column is TODAY, not yesterday** — the page is built for
    the current date, so that column is empty until the games are played.
    Reading it as "yesterday" made every arm look rested. `skip_today=False`
    is for a grid that has already been trimmed to completed days.
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
        if y and d2:
            out[nm] = "likely_rest"
        elif y and (y.get("pitches") or 0) >= ITP_HEAVY_PITCHES:
            out[nm] = "likely_rest"
        else:
            out[nm] = "available"
    return out


# How much worse than league average an arm with no board row is. A pitcher
# who has not accumulated a FanGraphs line is a fresh call-up or a September
# add, not a league-average reliever — the same trap as defaulting `app_rate`
# to 0.35. Sized off the gap between a league-average reliever and the bottom
# of a real pen; deliberately coarse, because the alternative is dropping him
# from the roster entirely, which is worse.
REPLACEMENT_TILT = -0.06


def replacement_pitcher_rates() -> List[float]:
    """League baseline tilted to replacement level, from the PITCHER's side."""
    return offence_tilt(list(LEAGUE_BASELINE), -REPLACEMENT_TILT)


def replacement_batter(season: int = 2026,
                       save_dir: Path = SAVE_DIR) -> "Batter":
    """A hitter the rate layer has never seen — a callup with no board row.

    A player nobody has a line for is not league average — he is who a club
    reaches for once it has run out of the ones it preferred. The running game
    stays at the league marks, since there is nothing else to go on.

    **The tilt sign is OPPOSITE to `replacement_pitcher_rates`.**
    `offence_tilt` raises offence on a negative argument, so replacement is
    `-REPLACEMENT_TILT` for an arm (he allows MORE) and `+REPLACEMENT_TILT`
    for a hitter (he produces LESS). Copying the pitcher's sign made the
    unknown callup a 0.330 on-base hitter against a league 0.317 — an upgrade
    on the average regular, which is the opposite of the bug being fixed.
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
        return [copy_pitcher(p) for p in pen], dict(rep)
    # `_ITP_PEN_CACHE` above is in-PROCESS and dies with the interpreter, so a
    # day of separate A/B scripts re-fetched every club every time. The disk
    # cache is keyed on the DATE, which is when the page actually changes.
    state = load_itp_bullpen(abbr, session=session)
    if not state:
        return [], {"ok": False, "reason": "itp fetch failed"}
    by_name = {}
    for pid, rec in pit_table.items():
        by_name.setdefault(_norm_name(rec.get("name", "")), (pid, rec))
    avail = itp_availability(state)

    pen, missing, rested = [], [], []
    traits = load_reliever_traits(2026)
    for row in state["pen"]:
        raw = row["name"]
        nm = _itp_clean_name(raw)
        # **A resting arm stays on the ROSTER; he is just not available.**
        # Deleting him shortened the pen — Oakland went to FOUR arms on
        # 2026-08-23 — and this function's own comment a few lines down says a
        # short pen "hands his innings to better arms, which is the same error
        # as truncating the pen at 8". The rest filter was doing exactly that,
        # unguarded.
        #
        # The consequence was not a slightly thin pen but a broken one: with
        # four arms the sim burned the whole staff in EVERY simulated game and
        # hit an empty pen on 50.8% of change decisions, against under 4% for
        # the other 29 clubs. `_choose_reliever` returns None there and all
        # three call sites read `if nxt is not None`, so the man on the mound
        # simply stayed — `RELIEF_PULL_DAMAGE` inert and a shelled reliever
        # unremovable for the rest of the game.
        #
        # `_choose_reliever` already has the right shape: it picks from the
        # READY set and falls back to "everyone rested or burned: go anyway".
        # Rest belongs in that first tier, not in the roster. Real managers do
        # the same — out of arms, a tired one pitches the 12th.
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
    return [copy_pitcher(p) for p in pen], dict(rep)


def copy_pitcher(p: "Pitcher") -> "Pitcher":
    """A fresh Pitcher with the same inputs — the sim mutates per-game state."""
    import copy as _copy
    return _copy.copy(p)


def validate_bullpen_usage(teams: Sequence[str] = (), n: int = 3000,
                           season: int = 2026, seed: int = 11) -> List[dict]:
    """Does the sim REPRODUCE each reliever's measured usage?

    A trait that is loaded but not reproduced is not modelled. For every arm
    this compares what the traits file says he does against what the simulated
    games actually do:

        app_rate       how often he pitches at all
        avg_inning     the inning he enters
        bf_per_outing  how long he stays

    The league marks worth checking against: appearance rate median 10.8%,
    p90 41.2%, **max 53.4%** — any simulated arm above ~50% is wrong by
    construction, which is exactly what happens with no availability model.
    """
    bat_table, _ = build_rates("bat")
    pit_table, _ = build_rates("pit")
    traits = load_reliever_traits(season)
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



# ===========================================================================
# 15. OBSERVED RELIEVER ENTRIES — ground truth for deployment
# ===========================================================================
# The deployment scales in section 13 were nudged against a single pen. This
# extracts what managers ACTUALLY did: every pitching change in a sample of
# real games, with the state at the moment of the change and the handedness of
# both the arm coming in and the batter he faced.
#
# That gives two things nothing else does:
#   * the real distribution of entry inning / margin / leverage per role. It
#     is consumed DIRECTLY by `build_deployment` as per-arm histograms rather
#     than fitted against a pair of penalty scales — the scales that sentence
#     named were removed 2026-08-23, having reached nothing for some time;
#   * the real size of the handedness effect, which is otherwise an assertion.

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


def fetch_pbp_entries(game_pk: int, timeout: float = 20.0) -> List[dict]:
    """Every pitching change in one game, with the state at the change.

    A change is detected by the pitcher id differing from the previous plate
    appearance. The STARTER's first appearance is skipped — he did not enter,
    he began.
    """
    out: List[dict] = []
    try:
        r = requests.get(
            f"{STATSAPI}/game/{game_pk}/playByPlay", timeout=timeout)
        r.raise_for_status()
        plays = r.json().get("allPlays") or []
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


# ---------------------------------------------------------------------------
# Who is actually available tonight — rest, from real recent usage
# ---------------------------------------------------------------------------
# The per-game availability draw was a season-average FREQUENCY with no memory:
# an arm's chance of being ready tonight did not depend on whether he threw
# yesterday. Real usage is nothing like that.
#
# **Measured over 2026** (`savedata/reliever_entries.json`, dated through the
# schedule):
#
#   * back-to-back is 16.8% of appearances against a base rate near 34% of
#     days, so an arm who pitched yesterday is roughly HALF as likely to
#     pitch today;
#   * **three days in a row essentially never happens.** Across Oakland's
#     entire season not one reliever did it — max streak 2, for every arm.
#
# So availability is not a coin flip per game, it is a STATE carried from the
# previous days, and for a real slate it is knowable rather than modelled.
MAX_CONSECUTIVE_DAYS = 2
P_PITCH_ON_ZERO_REST = 0.50      # relative to his normal chance, measured 16.8/34

# Keyed on SEASON. Both were bare globals, so the first season loaded was
# served for every later request — the same silent no-op that would have made
# TEAM_CONTEXT_LAG report success while changing nothing.
_GAME_DATES: Dict[int, Dict[int, str]] = {}
_APPEARANCES: Dict[int, Dict[int, List[str]]] = {}


def game_dates(season: int = 2026, save_dir: Path = SAVE_DIR,
               refresh: bool = False) -> Dict[int, str]:
    """{game_pk: 'YYYY-MM-DD'} for the season, cached."""
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
    url = (f"{STATSAPI}/schedule?sportId=1&gameType=R"
           f"&startDate={season}-03-01&endDate={season}-11-15")
    try:
        data = requests.get(url, timeout=90).json()
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


def appearance_dates(season: int = 2026) -> Dict[int, List[str]]:
    """{pitcher id: sorted ISO dates he appeared in relief}."""
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


def _days_before(iso: str, n: int) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return (datetime.date(y, m, d)
            - datetime.timedelta(days=n)).isoformat()


def rest_days(pid: Optional[int], on_date: str,
              season: int = 2026) -> Optional[int]:
    """Days since this arm last pitched, or None if he has no history."""
    if pid is None or not on_date:
        return None
    ds = appearance_dates(season).get(int(pid))
    if not ds:
        return None
    y, m, d = (int(x) for x in on_date.split("-"))
    today = datetime.date(y, m, d)
    prev = [x for x in ds if x < on_date]
    if not prev:
        return None
    yy, mm, dd = (int(x) for x in prev[-1].split("-"))
    return (today - datetime.date(yy, mm, dd)).days


# A club uses 24.2 relievers across a season but carries only ~8 at a time —
# the season list is a UNION of many different pens, not one pen. Carrying all
# of them into every game lets a July call-up pitch in April and flattens the
# usage distribution across arms who were never on the roster together. An arm
# counts as rostered for a date if he appeared within this many days of it.
PEN_ROSTER_WINDOW_DAYS = 14


def available_bullpen(bullpen: Sequence["Pitcher"], on_date: Optional[str],
                      rng: random.Random, season: int = 2026,
                      window: int = PEN_ROSTER_WINDOW_DAYS,
                      future: bool = False) -> List["Pitcher"]:
    """Filter a pen to the arms that could realistically pitch on `on_date`.

    Uses REAL recent usage, not a season-average frequency:

      * not on the roster around this date -> not in the pen at all;
      * pitched each of the last `MAX_CONSECUTIVE_DAYS` days -> unavailable,
        because a third straight day essentially never happens;
      * pitched yesterday -> available at `P_PITCH_ON_ZERO_REST` of normal;
      * otherwise available.

    `future=True` looks only BACKWARD for the roster test, which is what a live
    projection must do; the default also looks forward, which is correct for a
    backtest and wrong for a forecast.

    With no date, or for an arm with no history, this is a no-op — the honest
    default, since the alternative is to invent a rest state.
    """
    if not on_date:
        return list(bullpen)
    app = appearance_dates(season)
    prior = [_days_before(on_date, k)
             for k in range(1, MAX_CONSECUTIVE_DAYS + 1)]
    lo = _days_before(on_date, window)
    hi = _days_before(on_date, -window) if not future else on_date
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


def season_game_pks(season: int = 2026, save_dir: Path = SAVE_DIR) -> List[int]:
    """Every completed game id for a season.

    Prefers the PBP accumulator when it exists (2026 only), and otherwise falls
    back to the SLATE, which is cached for any season the backtest can reach.
    Without the fallback there was no way to collect 2025 entries at all.
    """
    path = Path(save_dir) / "pbp" / f"season_{season}_v2.json"
    if path.exists():
        try:
            with open(path) as fh:
                return json.load(fh)["games"]
        except (OSError, ValueError, KeyError):
            pass
    return [r["pk"] for r in season_slate(season, save_dir=save_dir) if r.get("pk")]


def collect_reliever_entries(n_games: int = 0, workers: int = 12,
                             refresh: bool = False, season: int = 2026,
                             save_dir: Path = SAVE_DIR) -> List[dict]:
    """Pitching changes across a season, cached to disk PER SEASON."""
    path = entry_cache_path(season, save_dir)
    if path.exists() and not refresh:
        try:
            with open(path) as fh:
                cached = json.load(fh)
            if len(cached) > 0:
                return cached
        except (OSError, ValueError):
            pass
    pks = season_game_pks(season, save_dir)
    pks = pks[-n_games:] if n_games else pks
    print(f"[entries] {season}: fetching play-by-play for {len(pks)} games...")
    out: List[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(fetch_pbp_entries, pks):
            out.extend(rows)
    dest = Path(save_dir) / ENTRY_CACHE_FMT.format(season=season)
    with open(dest, "w") as fh:
        json.dump(out, fh)
    print(f"[entries] {season}: {len(out)} changes from {len(pks)} games")
    return out


# ---------------------------------------------------------------------------
# How LONG a relief appearance is — ground truth for sim_state.md 5.6
# ---------------------------------------------------------------------------
# The board gives the mean directly (2026 pure relievers: 4.481 TBF and 1.033
# IP per outing) but not the SHAPE, and the shape is what section 5.6 is
# about: an arm who touches two innings is a different usage pattern from one
# who faces six men in one. Both average the same.
#
# Note what is NOT measurable from the entries cache: it records where a
# reliever came IN, never where he went out.
STINT_CACHE = SAVE_DIR / "reliever_stints.json"


def fetch_pbp_stints(game_pk: int, timeout: float = 20.0) -> List[dict]:
    """Every pitcher's STINT in one game — batters faced, outs, innings.

    A stint is a maximal run of consecutive plate appearances by the same
    pitcher for one side. `mid_entry` is True when his first batter was not
    the first batter of a half-inning, which is the inherited-runner rescue.
    """
    try:
        r = requests.get(
            f"{STATSAPI}/game/{game_pk}/playByPlay", timeout=timeout)
        r.raise_for_status()
        plays = r.json().get("allPlays") or []
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
        # **Pitches per STINT, from the same response.** A manager hooks on the
        # pitch count and this engine hooks on BATTERS FACED, which cannot tell
        # 75 pitches through six from 105 through four. Whether that is what
        # under-disperses simulated starter length (BF sd 4.63 against a real
        # 5.13) is measurable only with this column. Counted off `playEvents`
        # rather than the boxscore because the boxscore is per PITCHER and a
        # pitcher can have two stints.
        cur["pitches"] += sum(1 for e in (p.get("playEvents") or [])
                              if e.get("isPitch"))

    out: List[dict] = []
    for side in ("top", "bot"):
        for s in stints[side]:
            s["innings"] = len(s["innings"])
            out.append(s)
    return out


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
    with open(SAVE_DIR / "pbp" / "season_2026_v2.json") as fh:
        pks = json.load(fh)["games"]
    pks = pks[-n_games:] if n_games else pks
    print(f"[stints] fetching play-by-play for {len(pks)} games...")
    out: List[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(fetch_pbp_stints, pks):
            out.extend(rows)
    with open(STINT_CACHE, "w") as fh:
        json.dump(out, fh)
    print(f"[stints] {len(out)} stints from {len(pks)} games")
    return out


def stint_profile(stints: Optional[Sequence[dict]] = None,
                  team_games: Optional[int] = None) -> dict:
    """Relief-appearance shape: BF, outs and innings touched.

    Takes stints from either source — `collect_reliever_stints` (real) or
    `sim_stints` — so the two are scored by the SAME code and cannot drift
    apart on a definition.
    """
    rows = [s for s in (stints if stints is not None
                        else collect_reliever_stints()) if not s["starter"]]
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


def validate_stint_shape(n_games: int = 400, season: int = 2026,
                         seed: int = 7, save_dir: Path = SAVE_DIR) -> dict:
    """Simulated relief-appearance shape against the real one — 5.6.

    The board gives only the mean. This is the distribution, and the
    distribution is where the defect is: matching BF per outing while touching
    too many innings means the sim is letting arms roll over inning
    boundaries instead of rescuing mid-inning.
    """
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
# Section 5.6c of sim_state.md catalogued the constants that were still
# hand-set stand-ins, after two hook curves turned out to be hand-drawn
# sequences sitting next to a file that had held the real distribution for
# weeks. The pattern it named is *a plausible stand-in, written when the real
# data did not exist, surviving after it did* — and it never fails a test,
# because the MEAN is usually right and only the SHAPE is wrong.
#
# These are the rest of that list. Every one of them is an OUTCOME question —
# take the base-out state before the play and ask what happened — so all of
# them fall out of a single traversal:
#
#   P_SAC_FLY       air out, runner on 3rd, <2 out       -> did he score
#   P_GIDP          ground out, runner on 1st, <2 out    -> were there two
#   P_GB_ADVANCE    ground out, runner on 2nd, 3rd empty -> did he take third
#   P_GB_SCORES     ground out, runner on 3rd, <2 out    -> did he score
#   P_STEAL_SUCCESS steal of second                      -> safe or out
#   FRAMING_K_SHARE the count table (below)
#
# **Section 2's claim that the first four "cannot be measured" is true only of
# the MOVEMENT-RECORD method.** A runner who holds generates no record, so he
# cannot be counted that way — which is exactly why the three advancement
# rates above them were measured and these four were not. Counted as outcomes
# the holders are simply the denominator minus the numerator, and nothing has
# to be inferred from an absence.
#
# The traversal has three seams worth stating, each of which silently
# corrupts a rate if it is got wrong:
#
#   * **The base state at CONTACT is not the state the PA started with.** A
#     runner can steal, or be picked off, or move on a wild pitch, during the
#     plate appearance and before the ball is put in play. Counting a man who
#     stole second as still standing on first turns a routine ground out into
#     a double-play opportunity that never existed. The running game is
#     therefore resolved FIRST, from the runner records, and only then is the
#     batted ball classified.
#   * **`count.outs` is the count AFTER the play**, and it includes outs made
#     on that running game. Differencing it against the previous play charges
#     a caught stealing to the batted ball, which reads as a double play.
#   * **`matchup.postOnFirst/Second/Third` is authoritative for the state
#     after the play** and is used instead of applying the movements
#     ourselves, so a missed movement cannot accumulate down an inning.
#
# Ground/air comes from `hitData.trajectory`, which is the same split the rate
# model uses (GB_OUT against AIR_OUT), so popups and line-drive outs are in
# the sac-fly denominator exactly as they are in the engine's AIR_OUT. That
# makes the measured P_SAC_FLY lower than a fly-ball-only reading, and the
# lower number is the one the engine needs.
#
# The pass also carries the COUNT TABLE, because it is free once the pitches
# are already in hand and it is what settles FRAMING_K_SHARE — see
# `framing_k_share` below.

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


def fetch_pbp_baserunning(game_pk: int, timeout: float = 30.0) -> Dict[str, int]:
    """Every base-running outcome and every count in one game, as counters.

    Returns a flat {name: count} dict so the season merge is a `Counter`
    update and the cache is plain JSON. Empty on any failure — a missing game
    is a smaller error than a half-parsed one.
    """
    c: Dict[str, int] = collections.Counter()
    try:
        r = requests.get(
            f"{STATSAPI}/game/{game_pk}/playByPlay", timeout=timeout)
        r.raise_for_status()
        plays = r.json().get("allPlays") or []
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
        # from the FIRST play of the half and so scores that play at zero. A
        # leadoff home run is silently free, and because the tail is cumulative
        # the loss lands on the (empty, 0 out) cell, biasing exactly the cell
        # every run-expectancy calibration keys off.
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


def collect_baserunning(season: int = BASERUN_SEASON, workers: int = 12,
                        refresh: bool = False, n_games: int = 0,
                        save_dir: Path = SAVE_DIR) -> Dict[str, int]:
    """Season-wide base-running and count counters, cached to disk.

    Keyed on SEASON from the start, because every cache in this file that was
    not has cost a silent wrong-season run at least once.
    """
    path = Path(save_dir) / BASERUN_CACHE_FMT.format(season=season)
    if path.exists() and not refresh:
        try:
            with open(path) as fh:
                got = json.load(fh)
            if got.get("counts"):
                return got["counts"]
        except (OSError, ValueError):
            pass
    pks = season_game_pks(season, save_dir)
    pks = pks[-n_games:] if n_games else pks
    print(f"[baserunning] {season}: play-by-play for {len(pks)} games...")
    tot: collections.Counter = collections.Counter()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(fetch_pbp_baserunning, pks):
            tot.update(got)
            done += 1
            if done % 200 == 0:
                print(f"[baserunning]   {done}/{len(pks)}", flush=True)
    counts = {k: int(v) for k, v in sorted(tot.items())}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"season": season, "counts": counts,
                   "rates": baserunning_rates(counts)}, fh, indent=1)
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
    wrong data to settle it.** Savant's catcher-framing leaderboard publishes
    `rv_11`..`rv_19`, which the doc read as run value BY COUNT; they are run
    value BY ZONE — Statcast's out-of-zone quadrants, which is why 15 is
    missing from the sequence. That board cannot answer this question at all.

    What answers it is the count table. A borderline take is called a strike
    or a ball, so the plate appearance continues from (b, s+1) or (b+1, s),
    and the difference between those two counts' eventual outcomes IS the
    effect of the call:

        dK  = P(K | b, s+1) - P(K | b+1, s)
        dBB = P(BB | b, s+1) - P(BB | b+1, s)

    weighted across counts by where framing chances actually occur. The
    constant is used as a share of a MULTIPLIER, so the split is between the
    two RELATIVE moves, not the absolute ones — and that is the whole reason
    0.5 is wrong. Walks are a quarter as common as strikeouts, so an equal
    absolute effect on each is a four times larger relative move on walks.

    Sanity mark: the measured absolute effects price out at about 0.13 runs
    per extra strike against a published framing run value near 0.125.
    """
    def p_k(b: int, s: int) -> float:
        if s >= 3:
            return 1.0
        if b >= 4:
            return 0.0
        n = c.get(f"c{b}{s}_reach", 0)
        return c.get(f"c{b}{s}_k", 0) / n if n else 0.0

    def p_bb(b: int, s: int) -> float:
        if b >= 4:
            return 1.0
        if s >= 3:
            return 0.0
        n = c.get(f"c{b}{s}_reach", 0)
        return c.get(f"c{b}{s}_bb", 0) / n if n else 0.0

    dk = dbb = w = 0.0
    for b in range(4):
        for s in range(3):
            wt = float(c.get(f"c{b}{s}_take", 0))
            if not wt:
                continue
            dk += wt * (p_k(b, s + 1) - p_k(b + 1, s))
            dbb += wt * (p_bb(b, s + 1) - p_bb(b + 1, s))
            w += wt
    if w < 5000 or dk <= 0 or dbb >= 0:
        return None
    dk, dbb = dk / w, dbb / w
    rk = dk / LEAGUE_BASELINE[K]
    rb = -dbb / LEAGUE_BASELINE[BB]
    return rk / (rk + rb)


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


def baserunning_report(season: int = BASERUN_SEASON, refresh: bool = False,
                       workers: int = 12) -> dict:
    """Measured against shipped, for every constant in sim_state.md 5.6c."""
    counts = collect_baserunning(season, workers=workers, refresh=refresh)
    rates = baserunning_rates(counts)
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




# --- RUN EXPECTANCY, as the instrument for a changed advancement model -----
# Section 2 says the free-advancement constants are "calibrated against our own
# measured RE24 ... re-fit them before trusting a changed advancement model."
# That instrument existed only as a table on disk and a throwaway script, so
# there was no way to ask the question it was written for. This is it.
#
# **The table it used to be scored against is biased, and only in one cell.**
# `walk_half_innings` in EffortMLB.py, which built `savedata/pbp/
# season_2026_v2.json`, tracks runs by DIFFERENCING the running score and
# seeds that difference from the first play of the half — so runs scored ON
# that first play count as zero. Every later row's tail is cumulative, so the
# loss lands entirely on the leadoff state: bases empty, nobody out. It reads
# 0.4665 there against a measured 0.4977, and a leadoff home run at 0.0305 per
# PA is almost exactly the gap. Anything fitted to make the sim match that cell
# was being asked to under-produce runs from an empty inning by 6%.
#
# `collect_baserunning` counts runs off the SCORING MOVEMENTS instead, which
# cannot drift, and `re24_report` scores against that.

_BASE_LABEL = ("___", "1__", "_2_", "12_", "__3", "1_3", "_23", "123")


def real_re24(season: int = BASERUN_SEASON, save_dir: Path = SAVE_DIR
              ) -> Dict[Tuple[int, int], Tuple[float, int]]:
    """{(bases bitmask, outs): (runs to end of inning, opportunities)}."""
    counts = collect_baserunning(season, save_dir=save_dir)
    out: Dict[Tuple[int, int], Tuple[float, int]] = {}
    for base in range(8):
        for outs in range(3):
            n = int(counts.get(f"re_{base}_{outs}_n", 0))
            if n:
                out[(base, outs)] = (
                    float(counts.get(f"re_{base}_{outs}_runs", 0)), n)
    return out


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


def re24_report(n: int = 6000, seed: int = 5,
                season: int = BASERUN_SEASON) -> dict:
    """Simulated run expectancy against the measured table, cell by cell.

    League-average clones on both sides, so nothing here is about a roster —
    it is the base-out transition model on its own, which is what the
    advancement constants are.
    """
    side = league_side

    home, away = side("H"), side("A")
    logs: List[List[dict]] = []
    for i in range(n):
        log: List[dict] = []
        simulate_game(home, away, random.Random(seed * 1_000_003 + i), log=log)
        logs.append(log)
    sim, real = sim_re24(logs), real_re24(season)

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


# ===========================================================================
# 16. EMPIRICAL DEPLOYMENT — per pitcher, from what he actually did
# ===========================================================================
# Sections 13/15 scored arms with a formula whose scales were tuned by hand.
# That was the wrong shape of solution: **we have every entry he made.** Mason
# Miller's real distribution is 8th 14% / 9th 84% / 10th 2% — he has never
# entered a 6th or a 7th — and no exponential penalty reproduces that as
# cleanly as simply reading it off.
#
# Three distributions, each shrunk toward the next-coarsest level by its own
# sample size (median depth is 10 entries per pitcher, so individuals are thin
# and the shrinkage is doing real work):
#
#   P(inning | pitcher)      his own histogram  <- role histogram
#   P(margin | pitcher)      his own histogram  <- role histogram
#   home/away tie factor     measured: closers enter 9th-inning ties 67 times
#                            at home against 44 on the road (1.52x), which is
#                            managers saving the closer for the 10th on the
#                            road exactly as expected
#
# Everything is per pitcher and therefore per TEAM by construction — no
# league-aggregate role model in the selection path at all.

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


def _role_of(pid: int, traits: dict,
             pbp_avg_inning: Optional[float] = None) -> str:
    """Bullpen role, from insidethepen when it is available and from the
    PLAY-BY-PLAY when it is not.

    **ITP only serves the CURRENT season**, so every past-season run had
    `itp_avg_inning = None` for every arm and classified all of them "other" —
    2025 produced 713 relievers and zero closers, which silently disabled the
    entire role layer for any backtest before this year.

    The play-by-play carries the same quantity: ITP's "average inning when
    called" IS the mean entry inning, and we have 16,017 of those for 2025.
    Validated on 2026, where both exist: **corr +0.90**, MAE 0.34 innings, and
    it reproduces ITP's own role label 83% of the time. Rounded, because ITP
    reports the figure on an integer-ish scale (its mean is 7.03 against a
    continuous 7.24) and the thresholds below were set against that.
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


def build_deployment(season: Optional[int] = None) -> dict:
    """Per-pitcher entry distributions, shrunk toward role. Cached per season."""
    season = DEPLOY_SEASON if season is None else season
    if season in _DEPLOY:
        return _DEPLOY[season]
    entries = collect_reliever_entries(season=season)
    traits = load_reliever_traits(season)

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
    mar_key = lambda r: margin_bucket(int(r["margin"] or 0))

    role_rows: Dict[str, List[dict]] = {}
    for pid, rows in by_p.items():
        role_rows.setdefault(_role_of(pid, traits, pbp_inn.get(pid)),
                             []).extend(rows)
    role_inn = {r: hist(v, inn_key, INNING_BUCKETS) for r, v in role_rows.items()}
    role_mar = {r: hist(v, mar_key, MARGIN_BUCKETS) for r, v in role_rows.items()}
    # P(margin | inning, role) — the JOINT, which the product of marginals
    # cannot express. A closer's 9th-inning probability is so dominant that
    # multiplying it by a low blowout probability still beat every other arm's
    # 7th-inning-shaped distribution, so he took 30% of his entries in
    # blowouts against a real 14%. Conditioning on the inning fixes that: in a
    # 9th-inning blowout the closer's own history says he is not the man.
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


def deployment_score(pid: Optional[int], inning: int, margin: int,
                     is_home: bool) -> float:
    """How likely THIS pitcher is to be the one entering in THIS state."""
    dep = build_deployment()
    rec = dep["pitchers"].get(pid or -1)
    if rec is None:
        ri = dep["role_inning"].get("other") or {}
        rm = dep["role_margin"].get("other") or {}
        p = ri.get(min(inning, 10), 0.1) * rm.get(margin_bucket(margin), 0.2)
        return max(p, 1e-6)
    inn = min(inning, 10)
    mb = margin_bucket(margin)
    # The role's JOINT P(margin | inning), RAKED by this arm's own deviation
    # from his role's margin marginal.
    #
    # **The joint alone cannot separate two arms in the same role, and that was
    # the whole defect** (section 4i). It is a four-way role label, and in the
    # bucket that matters the roles agree: P(trail4 | inning 8) reads 0.167
    # closer / 0.184 middle / 0.111 other / 0.161 setup. Selection is a RATIO
    # of scores across available arms, so a term that is ~equal for every arm
    # cancels — the engine's closer was about as likely to enter down six in
    # the 8th as its mop-up man, and 14 of 30 pens conceded BACKWARDS.
    #
    # `rec["margin"]` already measures the thing per arm and correlates +0.455
    # with pitcher run value (worst-quartile P(trail4) 0.215 against a best-
    # quartile 0.103), but `role_joint` covered 82% of (pitcher, inning) cells
    # and overrode it on every one of them. The signal was measured, correct,
    # and outranked by a fallback.
    #
    # The raking keeps BOTH pieces and adds no constant: the joint keeps the
    # inning conditioning — which fixed a real bug, the closer taking 30% of
    # his entries in blowouts against a real 14% — while the ratio restores
    # each arm's own deviation. The regulariser is the shrinkage already inside
    # `build_deployment` (`w = n/(n+stabilize)`), so a thin arm's histogram
    # collapses toward his role and the ratio goes to 1 on its own. Nothing is
    # clamped, because a clamp would be the fitted parameter this deliberately
    # avoids.
    #
    # **NOT behind a flag, deliberately.** A flag is for a modelling choice the
    # A/B decides; this is a SIGN ERROR — 14 of 30 pens conceded backwards —
    # and a defect repair does not get a toggle. Same precedent as trap 17's
    # `make_pitcher` fallback fix, which went in as code with no switch. The
    # cost is that this change can never be priced in isolation, because no
    # `noarmratio` reference arm was ever cached; that is accepted.
    #
    # It does NOT reintroduce the bug the override was added for. The closer's
    # blowout-entry share goes 0.298 -> 0.258 (real ~0.14) — the raking IMPROVES
    # the very metric `role_joint` was protecting, which the override was in
    # fact failing to protect: 0.298 is the same ~30% the override was
    # introduced to fix.
    #
    # Measured over all 30 pens, innings 6-8, expected mound run value:
    # concession gap trail4-lead4 goes -0.0003 -> +0.2278 runs/9 against a real
    # +0.6801 (0% -> 33%), backwards clubs 14/30 -> 1/30, and the real inverted
    # U at lead-4+ appears where the old path had no notion of it.
    #
    # It does NOT close the gap, and the remaining ceiling is `MARGIN_BUCKETS`:
    # five cells cannot express a gradient that is monotone in every unit of
    # margin (`trail13` spans +0.139 at -1 to +0.429 at -3). That is a separate
    # change and wants measuring separately.
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



# ===========================================================================
# 17. SIM vs REALITY — validate against baseball, not against the market
# ===========================================================================
# A market line is a proxy with its own noise and its own vig; agreement with
# it is neither necessary nor sufficient for the simulation being right. These
# compare the engine against what actually happened, which is the thing it is
# supposed to reproduce.
#
# Reference marks, 1,837 completed 2026 games / 32,713 half-innings, measured
# off StatsAPI linescores (`REAL_MARKS_SOURCE` below):
#
#   team-game runs   mean 4.479  sd 3.225
#   game total       mean 8.958  median 8.0   sd 4.536
#   home win rate    0.5269
#   runs per half-inning  mean 0.5036  sd 1.0356, 72.60% scoreless
#   innings batted per team-game  8.894      extra-inning games  8.60%
#
# **The half-inning marks here were previously wrong** — 0.520 / 1.078 / 0.724
# — and the sd was 4.1% high, which mattered far more than it looks. It made
# sqrt(9) x half-inning sd (1.078 x 3 = 3.234) land on the real game sd
# (3.224) and founded the conclusion that REAL INNINGS ARE INDEPENDENT. They
# are not. With the measured 1.0356 the identity fails (3 x 1.0356 = 3.107 vs
# 3.225), and a direct decomposition over innings 1-8 — the only ones every
# team bats unconditionally — puts **11.1% of team-game run variance in
# between-inning covariance**:
#
#   innings 1-8      Var 9.660 = indep-sum 8.584 + covariance +1.076
#
# Innings 9+ must be excluded from that decomposition or they swamp it. All
# three of the score-selected effects there push covariance NEGATIVE: the home
# half of the 9th is not batted when the home side leads, extras happen only
# in tied (so low-scoring) games, and the auto-runner then inflates them.
#
# **It is still not momentum, and no rally term is warranted.** The covariance
# is FLAT in lag (+0.009 to +0.032 across lags 1-7, with lag 1 the LOWEST —
# the opposite of what momentum predicts; consecutive innings share a lineup
# turnover that pushes them apart). Flat in lag is the signature of a SHARED
# PER-GAME FACTOR, and leave-one-game-out attribution with a shuffled-label
# control says what it is: the opposing STARTER's identity carries ~47% of it,
# venue ~14%, opposing team ~11%, and the batting team essentially none.
#
# Which is why `validate_vs_reality` below cannot show it: it puts
# league-average clones on both sides, so matchup heterogeneity is zero BY
# CONSTRUCTION and the covariance term measures -0.7%. Run
# `validate_slate_vs_reality()` for the comparison that is actually fair.

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
# league moved ~0.6 runs a game across the 2019-2023 span alone — so a frozen
# mark silently turns into a wrong target, and the engine gets "validated"
# against a season that is no longer being played. `measure_real_marks()`
# recomputes every number here from the linescores and caches the result;
# `real_marks()` is what the harness should call.
MARKS_CACHE = SAVE_DIR / "real_marks_{season}.json"


def measure_real_marks(season: int = 2026, start: Optional[str] = None,
                       end: Optional[str] = None, refresh: bool = False,
                       timeout: float = 90.0) -> dict:
    """Recompute the reference marks from StatsAPI linescores, and cache them.

    Every quantity `validate_vs_reality` scores against, measured off the same
    pull so the denominators cannot drift apart — which is exactly how the
    half-inning marks went wrong before: 4.477 runs / 0.520 per half-inning
    implies 8.61 innings batted per team-game, and a team bats 8.894.
    """
    path = Path(str(MARKS_CACHE).format(season=season))
    if path.exists() and not refresh:
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass

    start = start or f"{season}-03-01"
    end = end or f"{season}-11-01"
    url = (f"{STATSAPI}/schedule?sportId=1&gameType=R"
           f"&startDate={start}&endDate={end}&hydrate=linescore")
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


def real_marks(season: int = 2026, measured: bool = True) -> dict:
    """Reference marks: measured when they can be, frozen when they cannot."""
    if measured:
        try:
            return measure_real_marks(season)
        except Exception:
            pass
    return dict(REAL_MARKS)


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
    marks = real_marks()
    return {k: {"sim": v, "real": marks[k], "diff": v - marks[k]}
            for k, v in got.items()}


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
            "starter_1_5": _window_cov(v, lambda i, j: j <= 4),
            "bullpen_6_8": _window_cov(v, lambda i, j: i >= 5),
            "spanning": _window_cov(v, lambda i, j: i <= 4 <= 5 <= j),
        },
    }


def _window_cov(v: Sequence[Sequence[int]], sel) -> Optional[float]:
    k = len(v[0])
    pts = [(x[i], x[j]) for x in v
           for i in range(k) for j in range(i + 1, k) if sel(i, j)]
    if not pts:
        return None
    mx = statistics.mean(a for a, _ in pts)
    my = statistics.mean(b for _, b in pts)
    return sum((a - mx) * (b - my) for a, b in pts) / len(pts)


def _form_probe(sd: float, shift: float, n: int, seed: int) -> dict:
    """Run league-average clones at a given form draw and report the effect."""
    global GAME_FORM_SD, GAME_FORM_MEAN_SHIFT
    old = (GAME_FORM_SD, GAME_FORM_MEAN_SHIFT)
    GAME_FORM_SD, GAME_FORM_MEAN_SHIFT = sd, shift
    try:
        side = league_side
        rng = random.Random(seed)
        home, away = side("H"), side("A")
        logs = []
        for _ in range(n):
            log: List[dict] = []
            simulate_game(home, away, rng, log=log)
            logs.append(log)
        rep = dispersion_report(inning_vectors(logs))
    finally:
        GAME_FORM_SD, GAME_FORM_MEAN_SHIFT = old
    return rep


def calibrate_form(target_extra_cov: float = 0.0159, n: int = 8000,
                   seed: int = 23, verbose: bool = True) -> dict:
    """Fit `GAME_FORM_SD` and `GAME_FORM_MEAN_SHIFT` against measured data.

    Two quantities, fitted in order because they are nearly independent:

    1. `GAME_FORM_SD` — solved so the added per-inning covariance matches
       `target_extra_cov`. Covariance is quadratic in the tilt, so one probe
       fixes the scale: sd = probe_sd * sqrt(target / probe_cov).
    2. `GAME_FORM_MEAN_SHIFT` — runs are CONVEX in offensive rate, so a
       symmetric tilt RAISES the mean. Measured, then subtracted. Skipping
       this would ship a variance fix that quietly moves every total.

    Returns the fitted values; it does NOT write them. Paste them into the
    constants once you have looked at the report.
    """
    base = _form_probe(0.0, 0.0, n, seed)
    # **Fit a GRID, do not iterate.** The covariance estimate carries ~20%
    # sampling error at this n, so a secant step chases that noise instead of
    # the signal — successive iterations bounced 0.0111 / 0.0115 / 0.0197 for
    # monotonically increasing sd. Covariance is very nearly quadratic in the
    # tilt (the clipping bends it only in the far tail), so probe a spread of
    # sd values, fit `cov = k * sd^2` by least squares through the origin, and
    # solve once. That averages the noise instead of following it.
    grid = [0.06, 0.09, 0.12, 0.15, 0.18]
    pts = []
    for g in grid:
        cov = _form_probe(g, 0.0, n, seed)["pair_cov"] - base["pair_cov"]
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
    at_sd = _form_probe(sd, 0.0, n, seed)
    # mean shift per team-game, converted back into tilt units by the same
    # local slope the probe measured
    d_mean = at_sd["mean"] - base["mean"]
    shift = 0.0
    if d_mean > 0:
        lo = _form_probe(sd, 0.004, n, seed)
        per_unit = (lo["mean"] - at_sd["mean"]) / 0.004
        if per_unit < 0:
            shift = max(0.0, d_mean / -per_unit)
    final = _form_probe(sd, shift, n, seed)

    out = {"GAME_FORM_SD": sd, "GAME_FORM_MEAN_SHIFT": shift,
           "base": base, "final": final,
           "target_extra_cov": target_extra_cov}
    if verbose:
        marks = real_marks()
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


def validate_dispersion(n: int = 6000, seed: int = 11,
                        season: int = 2026) -> dict:
    """Score the sim's run DISPERSION, not just its level, against reality.

    The level marks pass while the shape does not, and the shape is what
    prices totals and run lines. Note this runs league-average CLONES, which
    have no matchup heterogeneity at all: expect the covariance term near
    zero here, and read it against `inn18_cov` as the size of what real
    matchups plus an explicit game-level term have to supply.
    """
    side = league_side

    rng = random.Random(seed)
    home, away = side("H"), side("A")
    logs = []
    for _ in range(n):
        log: List[dict] = []
        simulate_game(home, away, rng, log=log)
        logs.append(log)
    rep = dispersion_report(inning_vectors(logs))
    marks = real_marks(season)
    rep["real"] = {"var": marks.get("inn18_var"),
                   "indep": marks.get("inn18_indep"),
                   "cov": marks.get("inn18_cov"),
                   "pair_cov": marks.get("inn18_pair_cov")}
    return rep


# ===========================================================================
# 17b. THE REAL SLATE — the comparison league-average clones cannot make
# ===========================================================================
# `validate_vs_reality` and `validate_dispersion` both put league-average
# CLONES on both sides. That is the right harness for asking whether the base/
# out machinery is sound, and the WRONG one for anything involving matchup
# spread: starter, lineup, park and weather heterogeneity are all zero by
# construction there, so between-inning covariance, the first-inning lift and
# the team-offence spread every read as engine defects when they are harness
# artifacts. Every headline number in sections 5.1a and 5b of sim_state.md was
# produced by running the REAL slate instead; this is that harness, which had
# been living in throwaway scripts.
#
# **The real comparison is computed from the same games that were simulated**,
# not from a season-wide marks file, and through the same `dispersion_report`
# the sim goes through. Same game set, same estimator, same innings window —
# so a difference cannot be a difference in what was measured.

SLATE_CACHE = SAVE_DIR / "season_slate_{season}.json"

# StatsAPI serves the whole hydrate in one request per window; 30 days keeps
# each response near 3 MB.
SLATE_WINDOW_DAYS = 30


def _dedupe_slate(rows: List[dict]) -> List[dict]:
    """One row per gamePk, the LAST entry winning.

    A game that is rescheduled or resumed comes back under TWO schedule
    entries sharing the same `pk` and differing only in `start`/`day_night` —
    five of them across 2025-26, including the Speedway Classic at Bristol
    Motor Speedway on 2025-08-02, which was rain-delayed. Every slate consumer
    iterates these rows, so a duplicate counts that game's runs TWICE: in
    `build_park_run_factors`' home/road means, in the forecast fetch, and in
    any backtest. 0.2% of games, with no error attached to it.

    **NOT the doubleheader case.** Those legitimately share a date and both
    clubs while carrying DISTINCT `pk`s — 32 of them in 2025 — and must
    survive; `probable_for` already had to grow a game number for exactly that
    reason.
    """
    seen: Dict[int, dict] = {}
    for r in rows:
        try:
            seen[int(r["pk"])] = r
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(seen.values(), key=lambda r: (r["date"], r["pk"]))


def season_slate(season: int = 2026, start: Optional[str] = None,
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
        url = (f"{STATSAPI}/schedule?sportId=1&gameType=R"
               f"&startDate={cur.isoformat()}&endDate={hi.isoformat()}"
               f"&hydrate=linescore,weather,team,probablePitcher,lineups")
        data = requests.get(url, timeout=timeout).json()
        for day in data.get("dates", []):
            for g in day.get("games", []):
                row = _slate_row(g)
                if row is not None:
                    out.append(row)
        cur = hi + datetime.timedelta(days=1)

    out.sort(key=lambda r: (r["date"], r["pk"]))
    # **One row per gamePk.** A game that is rescheduled or resumed comes back
    # under TWO schedule entries sharing the same `pk` and differing only in
    # `start` / `day_night` — five of them in 2025-26, including the Speedway
    # Classic at Bristol Motor Speedway on 2025-08-02, which was rain-delayed.
    # Every slate consumer iterates these rows, so a duplicate counts that
    # game's runs TWICE: in `build_park_run_factors`' home/road means, in the
    # forecast fetch, and in any backtest. 0.2% of games and no error attached
    # to it. The LAST entry wins, which is the rescheduled one.
    #
    # Note this is NOT the doubleheader case: those legitimately share a date
    # and both clubs while carrying DISTINCT `pk`s, and 32 of them in 2025
    # must survive — `probable_for` already had to grow a game number for
    # exactly that reason (trap 13's neighbour).
    out = _dedupe_slate(out)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(out, fh)
    except OSError:
        pass
    return out


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
        # FIRST PITCH, UTC ISO. The slate carried only the DATE, so the engine
        # could not ask any question involving when a game starts — day/night,
        # body clock, shadows. §7 records circadian as UNDERPOWERED rather than
        # absent, and that test had to be run against a different database
        # entirely because this one has no clock. Cheap to carry, and a
        # prerequisite for ever revisiting it here.
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
        "home_catcher": _lineup_catcher(lu.get("homePlayers")),
        "away_catcher": _lineup_catcher(lu.get("awayPlayers")),
        "condition": wx.get("condition"),
        "temp_f": float(temp) if temp not in (None, "") else None,
        "wind_mph": float(m.group(1)) if m else None,
        "wind_label": (m.group(2).strip() if m else ""),
        "home_innings": home,
        "away_innings": away,
    }


# ---------------------------------------------------------------------------
# PERIOD-CORRECT weather — the forecast the market actually had
# ---------------------------------------------------------------------------
# **The slate's weather is StatsAPI's GAME-TIME OBSERVATION, and that is a
# look-ahead.** The opening price is hung a median ~1.1 days before first pitch
# off a FORECAST, so a model holding the observed temperature and wind knows
# something no market participant could have known, and 3d.12's CLV is partly
# measuring that rather than the model.
#
# Ablating weather (the `nowx` arm) bounds the problem but answers the wrong
# question — it asks what the model is worth with NO weather, when what we want
# is what it is worth with the SAME weather the market had. Open-Meteo archives
# its own past forecast runs, so that is directly available:
# `temperature_2m_previous_day1` is the forecast for a given hour as it stood
# one day earlier.
#
# **The correction is not cosmetic.** Measured at Wrigley over the whole 2025
# season, the day-1 forecast misses by 2.06 F on temperature, 2.17 mph on wind
# speed, and **32.2 degrees on wind DIRECTION — more than 45 degrees on 21.8%
# of hours.** 5b.2 established that raw wind speed does nothing and only the
# component blowing OUT TO CENTRE carries signal, so direction is the whole
# term, and a 32-degree error is a large share of it.
#
# One request per park covers a whole season (4,680 hourly rows, ~5 s), so both
# seasons cost ~60 requests.
OPEN_METEO_PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
FORECAST_WX_PATH_FMT = "weather_forecast_{season}_d{lag}.json"

# Which weather the rate/context layer sees. "observed" is StatsAPI's game-time
# reading and ships, because a LIVE projection legitimately has tonight's
# forecast and this is the closest thing to it. "forecast_d1" is the
# period-correct version and is what any comparison against a MARKET PRICE
# should use. Uppercase, so `_slate_overrides` ships it to a pool worker.
WEATHER_SOURCE = "observed"
WEATHER_FORECAST_LAG_DAYS = 1

_FCST_WX: Dict[tuple, Dict[int, dict]] = {}


def forecast_weather_path(season: int, lag: int = 1,
                          save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / FORECAST_WX_PATH_FMT.format(season=season, lag=lag)


def weather_source_lag(source: Optional[str] = None) -> Optional[int]:
    """`"forecast_d1"` -> 1. None when the source is the observation.

    **Day 0 is not the same claim as day 1 and both are needed.** Day 0 is
    Open-Meteo's own analysis — still a look-ahead, exactly like the shipped
    observation — but it reaches the engine through the SAME continuous-bearing
    path as the forecast. Without it, an arm that swaps the observation for a
    forecast changes the information set AND the representation at once
    (StatsAPI's `wind_label` is a coarse eight-way bucket; a bearing is
    continuous), and the two cannot be told apart. Day 0 is the matched control
    and the day0 -> day1 difference is the pure information effect.
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
    path = forecast_weather_path(season, lag, save_dir)
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


def fetch_forecast_weather(season: int = 2026,
                           lag_days: Optional[int] = None,
                           save_dir: Path = SAVE_DIR,
                           verbose: bool = True) -> Dict[int, dict]:
    """The forecast as it stood `lag_days` before each game, per game_pk.

    One request per PARK covering the whole season, indexed onto each game by
    its first-pitch UTC hour.

    Two things are deliberate:

    * **the wind comes back as a COMPASS bearing** and is tagged
      `wind_frame="compass"`, so `weather_tilt` rotates it into the park frame
      by `park_azimuth`. StatsAPI's own label is already field-relative and
      needs no rotation — mixing the two up is the error CLAUDE.md records as
      making every park behave as though centre field pointed due north;
    * **`condition` is carried over from the observation** purely so the
      ROOF-CLOSED test still fires. Whether a roof is shut is close to
      knowable in advance and is not the leak being closed here; temperature
      and wind are.
    """
    lag = WEATHER_FORECAST_LAG_DAYS if lag_days is None else lag_days
    wm = _wm()
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
    _progress(f"forecastwx {season}: {len(by_park)} parks to fetch")

    for i, (park, rows) in enumerate(sorted(by_park.items()), 1):
        meta = wm.STADIUM_DATA[park]
        days = sorted({r["date"] for r in rows if r.get("date")})
        if not days:
            continue
        params = {
            "latitude": meta["lat"], "longitude": meta["lon"],
            # a day either side, because first pitch in UTC can land on the
            # neighbouring calendar day for a night game
            "start_date": _days_before(days[0], 1),
            "end_date": _days_before(days[-1], -1),
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
            r = requests.get(OPEN_METEO_PREVIOUS_RUNS, params=params, timeout=90)
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
        _progress(f"forecastwx {season} {i}/{len(by_park)} {park} {hit}")

    path = forecast_weather_path(season, lag, save_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump({str(k): v for k, v in out.items()}, fh)
    _FCST_WX.pop((int(season), int(lag)), None)
    if verbose:
        print(f"\n[forecastwx] {season}: {len(out)}/{len(slate)} games -> {path}")
    _progress(f"forecastwx {season} FINISHED {len(out)}/{len(slate)}")
    return out


# A nine-inning game spans about three hours, and the weather does not hold
# still for them. At Sutter Health Park on 2026-08-24 the forecast runs
# 85.0F/9.8mph at first pitch and 75.0F/7.4mph three hours later — the
# temperature falls 14.6F and the wind 35%. Priced off first pitch alone that
# game reads +1.88 runs of weather on the total; across the window it is
# +1.06, and the game-window MEAN temperature (79.6F) is within 0.2F of the
# park's own reference, so the entire heat term was an artifact of the hour we
# happened to sample.
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
    cannot serve a future game.** `fetch_forecast_weather` iterates
    `season_slate`, whose docstring is "every COMPLETED regular-season game",
    and it queries Open-Meteo's PREVIOUS-RUNS archive — it answers "what did
    the forecast say N days before a game that has already been played", which
    is a look-ahead control for the backtest, not a forward projection. So a
    live projection of tomorrow's slate got `weather_tilt = 0.0` on every game:
    `game_weather` reads StatsAPI's OBSERVATION, and a scheduled game has none.
    Silent, and weather is worth 0.0317 runs/degF and 0.0618 runs/mph wind-out.

    `weatherman.WeatherService.at()` already picks forecast-vs-archive by date
    and returns the hour covering first pitch, so this maps its row rather than
    opening a second Open-Meteo client.

    The frame TAGS are carried through deliberately and not re-derived:
    Open-Meteo's wind is a COMPASS bearing and must be rotated by the park
    azimuth, and its `surface_pressure` is already at the park's elevation.
    Mislabelling either is the error CLAUDE.md records — every park behaving as
    though centre field pointed due north, and Coors' 840 hPa "corrected" to
    664 for a 21% density error.
    """
    if not venue or not start_iso:
        return None
    try:
        wm = _wm()
        # **RESOLVE the name first.** StatsAPI's spelling is not our key —
        # it serves "Rate Field" where `STADIUM_DATA` holds "Guaranteed Rate
        # Field", and "loanDepot park" against "LoanDepot Park". An exact-match
        # test on the raw name silently returned None, so an OPEN-roof park
        # lost its forecast and priced neutral with nothing to say so. Every
        # other park consumer resolves internally (`park_run_factor`,
        # `weather_tilt`); this one did not, which is trap 2 — a silent key
        # miss returns a default, not an error.
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
    # `weather_tilt` reads `condition` and tests it against
    # `ROOF_CLOSED_CONDITIONS`; the backtest forecast path carries the OBSERVED
    # condition through for exactly that reason, and a scheduled game has no
    # observation to carry. Open-Meteo's `condition` is the WMO SKY code
    # ("Clear"), so passing it straight through silently disables the roof test
    # — Chase Field on a 103.6F day took a full +0.65-run heat bonus for a game
    # that will be played under a shut roof at ~72F.
    #
    # A fixed roof is KNOWN and is stamped closed. A RETRACTABLE one is a
    # decision made on the day, and guessing it is exactly the "correction
    # added for a plausible reason with no support" this project keeps
    # recording — so those games get NO forecast and keep the neutral
    # behaviour they already had. Weather is wired for the parks where it
    # cannot be confounded, and nowhere else.
    roof = str(((_wm().STADIUM_DATA.get(venue) or {}).get("roof") or "")).lower()
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
                pit_table: Dict[int, dict], season: int = 2026,
                hazard: Optional[List[float]] = None,
                save_dir: Path = SAVE_DIR) -> Dict[str, "TeamSide"]:
    """One board-built TeamSide per club appearing on the slate.

    Built once and shared: `simulate_game` never mutates a side, and rebuilding
    30 clubs per game costs more than every simulation put together.
    """
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
# one thing that could manufacture the CLV in 3d.12. Lineups go up a few hours
# before first pitch — after the opener is hung and before the close — and they
# are one of the things that MOVE a baseball line, so a model holding tonight's
# real nine knows something the opening price did not.
#
# Off, `_game_side` keeps `base.lineup`, the board's best-nine-by-PA, which is
# what a genuine pre-lineup projection has. Note it is POSITIVELY SELECTED
# (5.6a) — so the ablated arm carries a slightly better offence than reality,
# which biases toward finding LESS difference, not more.
#
# The STARTER is deliberately NOT gated with it: probables are announced days
# ahead and are normally known when the opener is hung, so they are not a
# look-ahead in the same sense. Late scratches are the exception and are not
# separable here.
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
    catcher_framing = (catcher_framing_per_game(
        catcher_id, season - TEAM_CONTEXT_LAG, save_dir)
        if (USE_PITCH_FRAMING and catcher_id is not None) else None)

    if sp_id:
        share = starter_gs_share(int(sp_id), season, save_dir)
        traits = load_reliever_traits(season).get(int(sp_id)) or {}
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


# **Calibration state must travel to the pool as DATA, and the list of what
# travels must NOT be maintained by hand.**
#
# Under Python 3.14 the default start method on Linux is `forkserver`, so a
# worker RE-IMPORTS this module and gets the shipped constants back. Anything a
# probe rebinds in the parent silently reverts inside the pool — the parent
# then reports the shipped model's numbers as the variant's, which is a wrong
# answer with no error attached.
#
# This bit three times in one session. A fixed 4-tuple missed `STABILIZE_PA_*`;
# replacing it with a hand-maintained NAME LIST then missed
# `FRAMING_TILT_SCALE` the very next time a constant was added. Both times the
# tell was two byte-identical result blocks — an A/B that had compared a
# variant against itself.
#
# So the capture is automatic: every module-level UPPERCASE binding holding a
# simple immutable value, plus the few private ones that matter. A new constant
# is covered the moment it exists.
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

    `multiprocessing` pickles the callable by qualified name — the same
    constraint `_slate_worker` and `_bank_worker` carry.

    Returns the raw 8-inning vectors rather than a finished report, because
    the covariance decomposition has to be taken over the POOLED set: summing
    per-chunk covariances would drop every cross-chunk pair and centre each
    chunk on its own mean. They are small (8 ints per team-game) and the
    module has exactly one `dispersion_report`, which is the point.
    """
    (rows, season, reps, seed, use_weather, use_venue, use_real_sp,
     use_real_lineups, save_dir, overrides, variant) = job
    # **Every calibration in this file works by rebinding a module global, and
    # on Python 3.14 that no longer survives the pool.** The default start
    # method on Linux is now `forkserver`, so a worker re-IMPORTS this module
    # and gets the shipped constants back — it would run the wrong model and
    # report it as the fitted one, with no error anywhere. State travels as
    # DATA in the job, never as inherited memory.
    #
    # It is a NAME->VALUE dict rather than a fixed tuple on purpose. The tuple
    # version enumerated four specific constants, and the first calibration
    # that touched a fifth (`STABILIZE_PA_PIT`) silently compared a variant
    # against itself and produced two byte-identical result blocks. A dict
    # cannot fail that way as long as `_slate_overrides` lists what it sets.
    globals().update(overrides)
    # Anything derived from an overridden constant has to be recomputed, or the
    # worker uses a cache built from the shipped values.
    _BOARDS.clear(); _ASOF_BOARDS.clear()
    _PRIOR_CURVE.clear(); _PRIOR_LEAGUE.clear()
    _SLATE_TABLES.clear()
    _PIT_ROWS.clear(); _BAT_ROWS.clear()
    if variant is not None:
        # Same reason, one level worse: a monkeypatched FUNCTION cannot be
        # pickled into a fresh interpreter at all, so the fatigue probe sends
        # its parameters and the worker rebuilds the closure here.
        globals()["fatigue_multipliers"] = _fatigue_variant(*variant)
    bat_table, pit_table, hz = _slate_context(season, Path(save_dir))
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
            vec += [v[:8] for v in inning_vectors([log]) if len(v) >= 8]
            tots.append(res.runs_home + res.runs_away)
        tot.append(statistics.mean(tots))
        mc.append(statistics.variance(tots) if reps > 1 else 0.0)
        real_vec += [row["away_innings"][:8], row["home_innings"][:8]]
        real_tot.append(sum(row["away_innings"]) + sum(row["home_innings"]))

    return {"vec": vec, "tot": tot, "mc": mc, "real_vec": real_vec,
            "real_tot": real_tot, "used": used}


def validate_slate_vs_reality(season: int = 2026, reps: int = 15,
                              seed: int = 17, limit: Optional[int] = None,
                              use_weather: bool = True,
                              use_venue: bool = True,
                              use_real_sp: bool = True,
                              use_real_lineups: bool = True,
                              workers: Optional[int] = None,
                              save_dir: Path = SAVE_DIR) -> dict:
    """Simulate the season's real matchups and score them against themselves.

    Every game is played `reps` times with its own starters, lineups, park and
    game-time weather, and the result is compared with the linescores of the
    very same games. The comparison therefore controls for schedule, park mix
    and opponent mix for free — none of which the clone harness can do.

    Games are independent pure-Python CPU work, so `workers` is processes; the
    GIL makes threads worthless here. **The answer does not depend on the
    worker count** — every game carries its own seed.
    """
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

    sim = dispersion_report(sim_vec)
    real = dispersion_report(real_vec)
    return {
        "season": season, "reps": reps, "workers": workers, "used": used,
        "sim": sim, "real": real,
        "game_total": _slate_total_report(sim_tot, sim_mc, real_tot, reps),
    }


def _slate_total_report(sim_tot: Sequence[float], sim_mc: Sequence[float],
                        real_tot: Sequence[int], reps: int) -> dict:
    """Game-total agreement, with the harness's OWN noise floor removed.

    **`reps` is not a free knob and a low one silently fakes both numbers.**
    A per-game mean over `reps` sims is the model's expectation plus Monte
    Carlo noise of variance `mc/reps`, and at reps=10 that noise is ~1.4 runs
    against a real model spread near 1.0 — so most of the reported `model_sd`
    is the harness, and the correlation is attenuated by roughly the same
    factor. Two fatigue variants once read 0.194 and 0.152 purely on that,
    which is a conclusion drawn from rep count.

    So the MC component is measured per game (the within-game variance of the
    reps, which is free) and reported alongside: `model_sd_adj` is the spread
    with it removed, and `corr_adj` the correlation disattenuated for it.
    Neither can be trusted when `mc_share` is large — raise `reps` instead.
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


# ---------------------------------------------------------------------------
# Fatigue, scored on the real slate — sim_state.md 5.4
# ---------------------------------------------------------------------------

def multiplier_run_value(n: int = 4000, seed: int = 3,
                         probe: float = 0.04) -> dict:
    """Runs per PA per unit of the fatigue/HFA multiplier bundle. MEASURED.

    The bridge between this engine's units and the RV/PA the play-by-play
    measurements and the literature are quoted in. Without it the two cannot
    be compared, and section 5.4 is exactly a comparison of the two: the
    shipped 0.004/batter had to be turned into RV/batter before anyone could
    see it was 4.2 standard errors off a measured slope of zero.

    Run on league-average clones, applying a CONSTANT bundle to every PA
    through `simulate_game`'s own `context`, so the number comes out of the
    same code path the term itself uses rather than a linear-weights estimate.
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


# The fatigue variant currently under test, as PARAMETERS rather than a
# patched function, so it can be shipped to a worker process. None outside a
# calibration run.
_fatigue_variant_args: Optional[tuple] = None


def _fatigue_variant(decline: float, opening: float, opening_bf: int):
    """Build a `fatigue_multipliers` for one candidate curve. Probe only."""
    def patched(bf, decline_per_bf=None, ref_bf=FATIGUE_REF_BF):
        d = 1.0 + decline * (bf - ref_bf)
        if bf < opening_bf:
            d *= opening
        d = max(d, 0.5)
        return {HR: d, S1B: d, S2B: d, BB: d,
                K: 1.0 / d, GB_OUT: 1.0 / d, AIR_OUT: 1.0 / d}
    return patched


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
    globals()["fatigue_multipliers"] = _fatigue_variant(*_fatigue_variant_args)
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


def calibrate_fatigue(season: int = 2026, reps: int = 12, seed: int = 17,
                      declines: Sequence[float] = (0.0, 0.002, 0.004),
                      openings: Sequence[float] = (1.0, 1.04, 1.078),
                      workers: Optional[int] = None,
                      verbose: bool = True) -> dict:
    """Score fatigue variants on the real slate's per-inning MEAN profile.

    Two things get re-derived here, and the second is the one that is easy to
    get wrong.

    **The gradient.** Sweeping `declines` against inning 1 shows directly what
    the play-by-play measurement said: flat fits, 0.004 does not.

    **The opening penalty, which must NOT be applied.** The same measurement
    found starters are worse for the first two batters of the game — +0.0265
    RV/PA against the rest of their own start, t 3.10 — and at 0.3378 runs per
    PA per unit that is a multiplier of 1.078. Applying it overshoots inning 1
    by nearly 3x, because **it is mostly the top of the batting order, not the
    pitcher**: bf 1-2 is always slots 1 and 2, while bf 3-24 averages the whole
    lineup, and the measurement controlled for pitcher but not for batter. A
    PA simulator bats the real order, so it already has that lift structurally
    and adding the measured penalty on top counts lineup quality twice.

    That is the FOURTH instance in this engine of the same trap — after
    uncentred fatigue, the uncentred park term and the uncentred platoon gap.
    The sweep is kept so the conclusion is re-derivable rather than asserted:
    if it ever stops overshooting, the term is worth revisiting.
    """
    rv = multiplier_run_value()
    rows = []
    for d in declines:
        rows.append(_fatigue_probe(d, 1.0, 2, season, reps, seed, workers))
    for o in openings:
        if o == 1.0:
            continue
        rows.append(_fatigue_probe(0.0, o, 2, season, reps, seed, workers))

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
        # **Do NOT rank these on `profile_rmse`.** It is taken over all eight
        # innings and is dominated by the ~0.04 level deficit in innings 4-8,
        # which no fatigue curve touches — so it separates the variants by
        # almost nothing (0.0334 vs 0.0335 between "no opening penalty" and a
        # x1.04 one) and will happily nominate a term that is wrong. The
        # quantity fatigue actually controls is the inning-1 LIFT, and the
        # decision rests on the structural argument in this function's
        # docstring, not on a summary statistic.
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


# ---------------------------------------------------------------------------
# The form draw, fitted on the REAL slate rather than on clones
# ---------------------------------------------------------------------------

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


def calibrate_form_on_slate(season: int = 2026, reps: int = 20,
                            seed: int = 17,
                            grid: Sequence[float] = (0.08, 0.12, 0.16),
                            workers: Optional[int] = None,
                            verbose: bool = True) -> dict:
    """Fit `GAME_FORM_SD` against the covariance the REAL SLATE leaves over.

    `calibrate_form` fits on league-average clones, which have no matchup
    spread at all, so it has to be given a target that already has an
    ASSUMPTION subtracted from it — "the real total is 0.0192 and matchup
    spread supplies ~0.0045, so add 0.0147". This fits the same quadratic on
    the real slate instead, where the matchup contribution is whatever it
    actually is and the target is simply the real number.

    Same two quantities, same order, and the second is still the trap:

    1. `GAME_FORM_SD` — covariance is quadratic in the tilt, so probe a grid,
       fit `cov = base + k*sd^2` and solve once. Probing rather than iterating
       is deliberate: the covariance estimate carries real sampling error and
       a secant step chases it (see `calibrate_form`).

       **Know this harness's noise floor before reading the verification run.**
       At reps=20 the grid's own residuals against the fitted quadratic are
       -7%, -3.5% and +1.5%, and two verification runs of the same fitted sd
       came back 0.0198 and 0.0176 against a target of 0.0189 — they straddle
       it. Anything inside ~5% of target at that rep count is the estimator,
       not the fit, and chasing it produces a different constant every time.
       Raise `reps` rather than re-fitting.
    2. `GAME_FORM_MEAN_SHIFT` — runs are CONVEX in offensive rate, so a
       symmetric tilt RAISES the mean, and it scales with sd^2. It is measured
       against the sim's OWN form-off mean, never against the real mean: the
       sim is ~0.17 runs light per team-game for reasons that have nothing to
       do with this draw (sections 5.4, 5.5), and calibrating the shift
       against reality would quietly launder that deficit into the noise term.

    **The two are NOT independent, and fitting them in sequence undershoots.**
    The grid is probed at shift 0, but the shipped configuration runs with the
    shift, which lowers the run level ~1.6% — and the covariance a shared
    MULTIPLICATIVE factor produces scales with the level squared. Predicted
    drop -3.2%, observed -3.9%, and the verification run duly came back 0.0182
    against a 0.0189 target every time it was run. So the grid is probed a
    second time WITH each candidate's own matched shift, which is the shape
    that actually ships, and the quadratic is refitted there.

    The second pass also MEASURES the tilt slope rather than importing
    `RUNS_PER_TILT`, which was fitted on league-average CLONES: the probe pair
    gives it on the real slate for free. **It does not reliably transfer** —
    it read 6.430 against the constant's 6.524 before the pitcher-rate fixes
    of section 5.9 and 7.494 against 6.513 after, a 15% gap, because the slope
    depends on the run level and on how much pitcher spread there is. Measure
    it; do not import it.
    """
    base = _slate_form_probe(0.0, 0.0, season, reps, seed, workers)
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
        r = _slate_form_probe(g, 0.0, season, reps, seed, workers)
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
        r = _slate_form_probe(g, s_g, season, reps, seed, workers)
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
        final = _slate_form_probe(sd, shift, season, reps, seed, workers)
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


# ---------------------------------------------------------------------------
# BACKTEST — the slate replayed on AS-OF rates
# ---------------------------------------------------------------------------
# The forward record (§0) is honest by construction; this is the other half,
# and its whole correctness rests on ONE rule:
#
#   **every input used to price a game must predate that game.**
#
# `asof_cutoff_for` enforces it by taking the latest cached cutoff STRICTLY
# before the game date — never on it. A cutoff equal to the game date would
# include the game itself, which is the exact failure this exists to prevent
# and is invisible in the output: the model simply looks good.
#
# What is still season-final, and is REPORTED rather than hidden, because a
# backtest that implies a frozen pipeline when it has one is worse than one
# that admits the gap: Savant OAA and catcher framing (their leaderboards
# ignore date parameters), the insidethepen pen, and the fitted constants
# (`GAME_FORM_SD`, `FRAMING_TILT_SCALE`, `PARK_RUN_RELIABILITY`, the
# playing-time prior's shape). See §3c.

# --- historic prices -------------------------------------------------------
# The right surface is the SEASON RESULTS ARCHIVE, not participant search:
#
#   https://www.oddsportal.com/baseball/usa/mlb-2025/results/
#
# One feed returns a page of finished games WITH their moneylines, so a season
# costs ~90 pages instead of 2,400 per-game resolutions. The page embeds the
# feed path it uses as `"ajaxUrl"` in its HTML — extract it rather than
# constructing it, because it carries a season token and a bookmaker bitmask
# that are not derivable:
#
#   /ajax-sport-country-tournament-archive_/6/YP4DOZ9N/X0X0...X32/1/0/?_=<ts>
#     6          sport id (baseball)
#     YP4DOZ9N   season token (MLB 2025; tournamentId 95993, season id 81037)
#     X0X0...    bookmaker selection bitmask, 41 words
#     1 / 0      page / offset
#
# **From a US IP this returns 200 with ZERO rows** — 332 bytes carrying only
# `nullResultText` ("no odds available from your selected bookmakers"). Tested
# with the bitmask set to all-ones, all-zeros and a single word: the mask is not
# the gate. That matches the standing finding that OddsPortal pulled the US odds
# display. The same page renders fully in a browser from another geo, so this
# needs `ODDSPORTAL_PROXIES` — the multi-geo setup `live_scores_widget.py` uses.
#
# Routes that are dead and should not be retried: `participant_matches` and
# `search_matches` both return 0 (those surfaces died in the 2026 Next.js
# rewrite), and a bare H2H url yields 0 lines for every market because its
# server HTML carries no event rows at all.
ODDS_CACHE = CLV_DIR / "historic_odds_{season}.json"

# **A long scrape must be watchable WITHOUT asking whoever started it.** This
# is a fixed, predictable path — not a session temp file — rewritten after
# every page, so `tail -f` on it shows live progress and an ETA:
#
#     tail -f OddsAPI/savedata/MLBclv/progress.log
#
# Any long-running job in this module should write here.
PROGRESS_LOG = CLV_DIR / "progress.log"


def _progress(msg: str, path: Path = PROGRESS_LOG) -> None:
    """Append one timestamped line to the watchable progress log."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(f"{datetime.datetime.now():%H:%M:%S}  {msg}\n")
    except OSError:
        pass

_AJAX_URL_RE = re.compile(r'"ajaxUrl"\s*:\s*"([^"]+)"')


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
            sys.path.insert(0, str(Path(__file__).resolve().parent))
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
            c = _op_client(px)
            page = season_archive_url(season)
            html = c._get(page).text
            mm = _AJAX_URL_RE.search(html.replace('\\"', '"').replace("\\/", "/"))
            if not mm:
                if verbose:
                    print(f"[odds] geo {label}: no ajaxUrl ({len(html)}B)")
                continue
            r = c._get(_archive_feed_path(mm.group(1), 1), referer=page)
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


def _archive_feed_path(ajax: str, page: int) -> str:
    """The localized ajax path, page-substituted and PROXY-PREFIXED.

    **Both halves matter.** The path off a localized page looks like
    `/pl/ajax-sport-country-tournament-archive_/...`, and `OddsPortalClient`'s
    proxy-prefix rule only matches paths that START with `/ajax-`, so the
    locale segment hides it and the request comes back as a 187-byte `URL:...`
    echo — which decodes as "Incorrect padding" and reads like the AES key
    rotated. It has not; the prefix is simply missing.

    **Pagination is the QUERY parameter `?page=N`, not the path.** The path
    carries a `/1/0/` pair that looks exactly like page/offset and is silently
    ignored — every value returns page 1 with `activePage: 1`, so a scrape that
    trusts it re-downloads the first 50 games fifty times and looks like it
    worked. The feed reports `total` and `pagination.pageCount`; use them.
    """
    base = ajax.split("?")[0].rstrip("/")
    return "/proxy/" + base.lstrip("/") + f"/?page={page}"


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
            mm = _AJAX_URL_RE.search(
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

    c, ajax, page_url, label = _archive_session(season, proxy, verbose)
    if c is None:
        if verbose:
            print(f"[odds] {season}: no geo served the archive. The US geo "
                  f"returns an empty feed; set WEBSHARE_API_KEY or pass proxy=.")
        return cache

    found = 0
    expected = None
    t0 = time.time()
    _progress(f"{season}  starting (geo {label})")
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
                r = c._get(_archive_feed_path(ajax, page), referer=page_url)
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
                c2, ajax2, _, lbl2 = _archive_session(season, None,
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
            # An empty page is ambiguous: genuinely past the end, or a geo/mask
            # mismatch. Believe it only if we have most of what the feed said
            # it had.
            #
            # **An empty PAGE 1 is never "past the end".** It means this geo
            # cannot serve this season, and because `total` is only read from
            # page 1 the run then has no expectation to compare against and
            # reports success on zero rows. That is how a backfill silently did
            # nothing while printing "FINISHED ... complete".
            if page == 1 and geo_swaps < 4:
                geo_swaps += 1
                if verbose:
                    print(f"[odds] page 1 EMPTY on geo {label} — this geo "
                          f"cannot serve {season}; re-selecting")
                _progress(f"{season}  page 1 empty on geo {label}, re-selecting")
                c2, ajax2, _, lbl2 = _archive_session(season, None,
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
        _progress(f"{season}  page {page}/{pag.get('pageCount') or '?'}  "
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
    _progress(f"{season}  FINISHED {len(cache)}/{expected or '?'} games"
              + ("  ** SHORT — re-run to backfill **" if short else "  complete"))
    return cache


# ---------------------------------------------------------------------------
# PER-EVENT odds — opening prices, TOTALS, run lines, and the period markets
# ---------------------------------------------------------------------------
# The season archive feed (`fetch_historic_odds`) carries the aggregate CLOSING
# moneyline and nothing else: `avgOdds`, `maxOdds`, `cntActive`. That was a
# deliberate trade — one feed page is ~20 games, so a season costs ~90 requests
# instead of ~1,900. What it cannot give is the two things a totals study needs:
# the OPENING price, and any market other than the moneyline.
#
# Both live on the per-EVENT odds endpoint, which `OddsPortalClient` already
# parses in full (`OutcomeBook` carries `opening_avg` and per-book `opened_at` /
# `changed_at` — the time axis, without which "opening" and "closing" are just
# two numbers with no clock). One request per event returns ~64 priced lines:
# moneyline, 14-18 whole-game totals, ~11 run lines, and the same again at
# scopes 2/3 (first five innings).
#
# **Three things are non-obvious and each of them silently returns nothing:**
#
#   1. **The `#encodedId` fragment is mandatory.** A bare H2H url serves the
#      LATEST meeting between the two clubs, not the game you asked for — and it
#      answers with a full, plausible page. The tell is that the home and away
#      teams come back SWAPPED relative to the archive row, because it is a
#      different game. `fetch_historic_odds` stores the fragment; keep it.
#   2. **A US egress IP returns zero outcomes**, same as the archive feed. The
#      event PAGE resolves (teams, start time), so this looks like a parse
#      failure rather than a geo block. `pl` works; `jp` is the fallback.
#   3. **The `/pl/` locale prefix must be stripped**, because the client's
#      `/proxy/` rule only matches paths starting with `/ajax-` — a localized
#      path slips past it and comes back as a 187-byte `URL:` echo that decodes
#      as "Incorrect padding", reading exactly like a rotated AES key.
#
# Measured at ~17 s/event single-threaded, so a season is ~9 hours serial and
# ~1 hour at 8 workers. Resumable by event id: re-running only fetches what is
# missing, which matters because the free Webshare proxies drop constantly.
EVENT_ODDS_PATH_FMT = "MLBclv/event_odds_{season}.json"
EVENT_ODDS_MARKETS = (3, 2, 5)      # moneyline, totals, run line
EVENT_ODDS_GEOS = ("pl", "jp")      # measured: `direct`/`gb`/`es` give nothing


def event_odds_path(season: int, save_dir: Path = SAVE_DIR) -> Path:
    return Path(save_dir) / EVENT_ODDS_PATH_FMT.format(season=season)


def load_event_odds(season: int, save_dir: Path = SAVE_DIR) -> Dict[str, dict]:
    """Cached per-event odds, keyed by the archive's event id. {} when absent."""
    path = event_odds_path(season, save_dir)
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


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


def _pack_event(eo) -> dict:
    lines = getattr(eo, "markets", None)
    lines = lines() if callable(lines) else lines
    out = {"home": eo.home, "away": eo.away,
           "start_ts": getattr(eo, "start_ts", None), "lines": []}
    for m in (lines or []):
        outs = [_pack_outcome(o) for o in (m.outcomes or [])]
        if not outs:
            continue
        out["lines"].append({"bt": m.betting_type_id,
                             "sc": getattr(m, "scope_id", 1),
                             "h": m.handicap, "o": outs})
    return out


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
            packed = _pack_event(eo)
            if packed["lines"]:
                return ev_id, packed
        except Exception:
            continue
    return ev_id, None


def fetch_event_odds(season: int = 2026, limit: Optional[int] = None,
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
    archive = load_historic_odds(season, save_dir)
    if not archive:
        raise FileNotFoundError(
            f"mlb_sim: no historic_odds_{season}.json — run "
            f"`fetch_historic_odds({season})` first; this reads its event ids "
            f"and #encodedId fragments.")
    geos = _oddsportal_geos()
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
    _progress(f"eventodds {season}: {len(todo)} to fetch, {len(cache)} cached")
    if not todo:
        return cache

    path = event_odds_path(season, save_dir)
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
                _progress(f"eventodds {msg}")
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
    _progress(f"eventodds {season} FINISHED {have}/{want}")
    return cache


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


def market_total(packed: dict, scope: int = 1,
                 min_books: int = 3, price: str = "close",
                 both_ends: bool = False) -> Optional[float]:
    """The market's own expected total: the line whose over and under sit
    closest to even money.

    That line is the MEDIAN of the market's predictive distribution, not its
    mean — measured here at 0.40 (2025) and 0.47 (2026) BELOW the actual mean
    total, which is the +0.75 skew in game runs. Comparing it against a model
    MEAN manufactures a half-run market bias that does not exist, and that is
    the trap section 8 records twice.

    `min_books` matters: the extreme lines are quoted by 2-4 books and their
    prices are wide, so an unfiltered "closest to even" can land on a thin
    outlier rather than the real number.

    `price` selects which end of the market is read — "close" (`a`, the
    default and the shipped behaviour) or "open" (`o`). `both_ends` additionally
    requires the line to be priced at BOTH ends, which is what a CLV comparison
    needs: an opening line that no book still quoted at the close, or a closing
    line nobody hung at the open, cannot be differenced. **Both default to the
    old behaviour on purpose** — `score_totals_vs_market` and every number in
    3d.11 were produced by the closing-only form, and quietly changing which
    line it selects would move published results.
    """
    key = "o" if price == "open" else "a"
    best = None
    for line, side in event_totals(packed, scope).items():
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

    **This is the sharper instrument and the reason `eventodds` exists.**
    Against realised totals the ceiling is ~4-5% R2 (the closing total itself
    manages 3.99% on 2025 and 4.66% on 2026), so a real defect takes thousands
    of games and 3.5 se to see. The market's line has ~2x the correlation with
    the outcome that our model does, which makes it a far better reference for
    the same sample.

    Reported for BOTH references, deliberately:
      * `vs_actual` — the model against baseball. Ceiling ~5% R2.
      * `vs_market` — the model against the close. Calibration slope here is
        the number to watch: 1.0 means the model's deviations from the league
        mean are the size the market's are.
    """
    season = int(season or bt.get("season") or 2026)
    ev = load_event_odds(season, save_dir)
    arch = load_historic_odds(season, save_dir)
    if not ev or not arch:
        return {"n": 0, "season": season,
                "why": "no event_odds / historic_odds cached"}
    # **Reuse `odds_by_game`'s key rather than re-deriving it.** It maps the
    # archive's full club names through `_team_index` to board abbreviations and
    # applies `_ARCHIVE_LOCAL_SHIFT` to the timestamp; hand-rolling either gives
    # a join that matches nothing and looks like missing data. It also DROPS
    # doubleheaders, which the archive cannot disambiguate.
    rows = odds_by_game(season, save_dir)
    ev_by_url = {}
    for k, a in arch.items():
        if k in ev:
            ev_by_url[a.get("url")] = ev[k]
    book: Dict[tuple, float] = {}
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
        key = (g["date"], g["home"], g["away"])
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


def available_asof_cutoffs(season: int = 2026,
                           save_dir: Path = SAVE_DIR) -> List[str]:
    """Cutoff dates with BOTH boards cached. Sorted."""
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

    **This silently produced a no-op arm.** `USE_AIR_DENSITY` rides the
    forecast weather path, and `air_density` returns None without a pressure —
    so `weather_tilt` correctly falls back to the temperature term and the arm
    comes back BYTE-IDENTICAL to its control. That reads as "air density is
    worth nothing", which is a conclusion, not a missing file.

    Graceful degradation is right for ONE game with no reading and wrong for a
    SOURCE that carries none, because then the fallback is the whole arm. So
    per-game absence still degrades; a source-wide absence raises here.
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
    _cq = (club_quality_asof(season, save_dir,
                             cutoffs=available_asof_cutoffs(season, save_dir))
           if TEAM_QUALITY_GAIN else {})
    for idx, row in enumerate(rows):
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
        # the cutoffs happen to be distributed across processes.
        #
        # **The seed is identical across ARMS as well**, which is what makes
        # `RATE_MODEL` a clean A/B: the baseline arm and an ML arm play the
        # same 2,000 games with the same form draws, the same hooks and the
        # same bullpen, and the only thing that differs is the nine numbers
        # each plate appearance is drawn from. That is section 9 of the ML
        # experiment plan, and it costs nothing because it was already true.
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
            # The FULL joint (home,away) run histogram, "h,a" -> count.
            #
            # The margin distribution is what prices a heavy favourite, and
            # storing only `p_home` throws it away: a win probability cannot
            # distinguish a compressed mean differential from an inflated
            # spread, and those want opposite fixes. The joint table answers
            # both, plus every run-line rung and the total, with no
            # re-simulation. ~150 non-empty cells a game at 2,000 reps.
            #
            # Keyed as a string because JSON has no tuple keys; read it back
            # through `joint_margins`, which RAISES on an arm built before
            # this existed rather than silently reporting an empty
            # distribution (trap 9).
            "joint": _joint_runs(res),
            # Starter vs relief attribution. The engine has always tracked
            # `PitcherLine.r`; the backtest simply discarded it, the same way
            # it discarded the margin distribution before `joint`. Read the
            # attribution caveat in `_staff_split` before using it.
            **_staff_split(res, home, away),
            # Runs-per-half-inning histogram, "runs" -> count, pooled over all
            # sims. Compact, and the only thing needed to test whether the
            # engine under-clusters within an inning.
            "inn_h": _half_inning_hist(res, "home"),
            "inn_a": _half_inning_hist(res, "away"),
            "actual_total": sum(row["home_innings"]) + sum(row["away_innings"]),
            "actual_home": sum(row["home_innings"]),
            "actual_away": sum(row["away_innings"]),
            "home_won": sum(row["home_innings"]) > sum(row["away_innings"]),
        })
    return out


def backtest(season: int = 2026, reps: int = 60, seed: int = 17,
             limit: Optional[int] = None,
             odds: Optional[Dict[int, dict]] = None,
             workers: Optional[int] = None,
             save_dir: Path = SAVE_DIR, verbose: bool = True) -> dict:
    """Replay the season on as-of rates. Returns per-game projections.

    `odds` is an optional {game_pk: {...}} of historic prices; without it this
    produces projections and scores them against the ACTUAL results, which is
    still a real out-of-sample test of the model — just not a CLV number.
    """
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
# **Section 7 trap 14 says the moneyline cannot resolve a rate-layer change,
# and section 5 answers "read the closing TOTAL". The total is the WRONG
# instrument for anything that moves the two clubs in opposite directions.**
#
# A game is two numbers, H and A. Every question about who wins is a question
# about D = H - A; the total is H + A. An error that makes the favourite too
# weak and the underdog too strong by the same amount moves D by twice that
# and leaves the total EXACTLY unchanged. The totals harness is not merely
# insensitive to it, it is blind to it by construction — which is why a defect
# worth 7.7 points of win probability on heavy favourites survived a library of
# arms all scored on the total.
#
# The moneyline is the right QUANTITY and the wrong RESOLUTION: one bit a game,
# and only ~9% of games are lopsided enough to carry the signal.
#
# `bt=5` in `event_odds_<season>.json` is the Asian handicap, and it is quoted
# as a LADDER — 17k-27k lines a season, handicaps from -8.5 to +8.5. That is
# not a run line, it is the market's implied CDF of D, priced on every game.
# It was on disk unread since the archive was first pulled. Against it the same
# defect reads t +3.65 where the moneyline reads +2.79 and the total reads
# nothing at all.
#
# Two properties make the ladder trustworthy, both CHECKED rather than assumed
# (`ladder_report`): the rungs are monotone in the handicap, and the moneyline
# is bracketed by the rungs either side of it. Both hold on ~99.97% of games.
#
# **Do not read a compression factor off a per-game ladder fit without fixing
# the RUNG SET.** Games quoted with a wider ladder are more lopsided games, so
# a fit that uses whatever rungs each game happens to carry measures the
# selection and reports it as a model defect. Fitting 2025 on all available
# rungs gives a "12.7% compression, t +11"; the same games restricted to the
# rungs every game carries give +0.998. The first number is an artifact and
# cost a full analysis pass before the control caught it.

# Asian handicaps refund the push, so an INTEGER rung prices P(D > k | D != k)
# and only the half-integer rungs are clean points of the CDF. Baseball's
# ladder starts at +-1.0 (there is no +-0.5), so the moneyline supplies the
# m = 1 rung and the half-integers supply m >= 2 and m <= -1.
LADDER_MIN_BOOKS = 3


def handicap_ladder(season: int, price: str = "a",
                    min_books: int = LADDER_MIN_BOOKS,
                    save_dir: Path = SAVE_DIR) -> Dict[tuple, dict]:
    """{(date, home, away): {"rungs": {m: P(D>=m)}, "ml": p_home, ...}}.

    `price` is "a" (close) or "o" (open), matching `line_open_close`.

    The join runs through the ARCHIVE row rather than the packed event, so the
    date, the club abbreviations and the final score all come from the surface
    that `odds_by_game` already validates — and the score is kept on the row so
    a caller can verify the join instead of trusting it.
    """
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


def _slope(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float]:
    """OLS slope of y on x with its standard error."""
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - xm) ** 2 for x in xs)
    b = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / sxx
    res = [y - (ym + b * (x - xm)) for x, y in zip(xs, ys)]
    return b, math.sqrt(sum(r * r for r in res) / (len(xs) - 2) / sxx)


def score_differential(rows: Sequence[dict]) -> dict:
    """Score an arm on the run differential — the instrument the total cannot see.

    Everything here is against ACTUAL results; the market only ever enters as
    a bucketing variable, never as ground truth.
    """
    got = {"n": len(rows)}
    got["model_mean_d"] = statistics.mean(r["model_d"] for r in rows)
    got["actual_mean_d"] = statistics.mean(r["actual_d"] for r in rows)
    got["model_sd_d"] = statistics.stdev([r["model_d"] for r in rows])
    b, se = _slope([r["model_d"] for r in rows], [r["actual_d"] for r in rows])
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
    line.** Runs per game are right-skewed by about +0.58 in this engine, so
    the total where P(over) = 0.5 sits that far below the mean — comparing it
    with an actual MEAN manufactures a level bias of exactly the skew. It
    reported -0.58 on season-final rates while the mean was -0.004, and the
    per-cutoff profile still looked like a real defect because the skew is
    roughly constant.

    Both are kept because they answer different questions: `total_bias` is the
    model against baseball, `line_bias` is the model against a BOOK, whose
    total is itself a median. That distinction cost two diagnostic passes once
    already (sim_state.md 8) and this is the second time it has been made in
    the same file.
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
# The backtest above scores against RESULTS, which proves the model is not
# biased but says nothing about edge. This scores it against the market, at the
# hardest available bar: the CLOSING price, de-vigged, on games the model never
# saw. Beating a close is the standard because the close is the sharpest number
# a market produces — an edge that survives it is an edge.
#
# **Read the model's BIAS before its edge.** A model half a run high takes the
# over in three games of four and reports the bias as edge. The moneyline
# equivalent is a standing home/away tilt, which is why `score_backtest`'s
# `ml_bias` is printed alongside and is currently +0.0010.

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


# `start_ts` is UTC and StatsAPI's `officialDate` is the date in the BALLPARK's
# local time, so they disagree for every night game west of nothing in
# particular. Shifting back 8 hours before taking the date recovers the local
# day for every MLB start time without needing a timezone per park: the
# earliest first pitch is ~11:00 local (07:00-10:00 after the shift, same day)
# and the latest ~22:00 local (18:00-21:00, still the same day).
#
# **Do NOT index both candidate dates instead.** Clubs play three- and
# four-game SERIES, so the neighbouring day is usually the same two teams — a
# two-date index silently attaches Wednesday's closing price to Tuesday's game.
# It showed up as consecutive dates carrying identical odds, which is the only
# reason it was caught; every downstream number would have looked normal.
_ARCHIVE_LOCAL_SHIFT = datetime.timedelta(hours=8)


def odds_by_game(season: int, save_dir: Path = SAVE_DIR
                 ) -> Dict[tuple, dict]:
    """{(date, home_abbr, away_abbr): odds row} from the results archive.

    Ambiguous keys are DROPPED rather than resolved. A repeated key is a
    doubleheader, and the archive gives no way to tell game one from game two;
    guessing would attach the wrong price to both. `odds_join_report` counts
    what this costs.
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
    return {k: v[0] for k, v in groups.items() if len(v) == 1}


def odds_join_report(season: int, save_dir: Path = SAVE_DIR) -> dict:
    """How much of the slate the closing-odds join actually covers.

    A join is a place where a silent 40% loss looks exactly like a small
    sample, so this is printed rather than inferred.
    """
    raw = load_historic_odds(season, save_dir)
    book = odds_by_game(season, save_dir)
    slate = season_slate(season, save_dir=save_dir)
    seen = collections.Counter((r["date"], r["home"], r["away"])
                               for r in slate)
    matched = sum(1 for k, c in seen.items() if c == 1 and k in book)
    return {"archive_rows": len(raw), "unique_keys": len(book),
            "slate": len(slate),
            "slate_doubleheaders": sum(c for c in seen.values() if c > 1),
            "matched": matched}


def clv_vs_closing(bt: dict, season: int = 2026, edge: float = 0.03,
                   price: str = "avg", save_dir: Path = SAVE_DIR) -> dict:
    """Score a backtest's moneylines against the de-vigged CLOSING price.

    `edge` is the probability difference that counts as a signal — 0.03 means
    the model has to disagree with the close by three points before the game is
    a pick. `price` is `avg` (the book average, a fair-value test) or `max`
    (best available, what could actually be bet).

    Returns per-pick rows plus the summary. **Both the FILTERED and the ALL
    buckets are reported**, because a filter that selects nothing is the null
    result and it looks identical to a filter that selects badly unless the
    unfiltered number is next to it.
    """
    book = odds_by_game(season, save_dir)
    rows: List[dict] = []
    matched = 0
    mismatched = 0
    for g in bt["games"]:
        row = book.get((g["date"], g["home"], g["away"]))
        if row is None:
            continue
        decs = row.get(f"{price}_odds") or []
        if len(decs) != 2 or not all(decs):
            continue
        p = devig(decs)
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
    # **The edge filter is applied to a Monte Carlo ESTIMATE of p_home, so the
    # rep count decides what it selects.** At 40 sims the standard error on a
    # near-even probability is 0.079 — more than twice the 0.03 threshold — so
    # the "edge > 3%" bucket would be mostly games where the SIMULATOR got
    # lucky, not games where the model disagrees. Selection on a noisy score
    # then regresses: the picked set's true edge is far smaller than its
    # measured one, which dilutes ROI toward zero and flattens the bucket
    # profile. In other words the failure is quiet and it points the wrong way.
    reps = max(int(bt.get("reps") or 1), 1)
    mc_se = (0.25 / reps) ** 0.5
    return {"season": season, "edge": edge, "price": price,
            "matched": matched, "mismatched": mismatched,
            "reps": reps, "mc_se": mc_se,
            "n_games": len(bt["games"]),
            "picks": rows,
            "filtered": summarize_clv_bucket([r for r in rows
                                              if r["edge"] > edge]),
            "all": summarize_clv_bucket(rows),
            "buckets": clv_edge_buckets(rows)}


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
# Beating the CLOSE is the bar an edge has to clear. CLV asks a different and
# far more SENSITIVE question: priced against the market before it had finished
# forming its opinion, did the price move TOWARD the model? It needs no game
# result, so it converges in a season rather than a decade — which is why the
# NHL model in `NHLvacuum/model_test.py` found CLV to be the signal where ROI
# was not.
#
# **Four ways this measurement can fake a positive, each handled explicitly:**
#
#   1. **The overround shrinks as a game approaches.** A raw implied
#      probability is `fair x overround`, so as the book tightens BOTH sides
#      drift in the same direction at once — down, here, since the overround
#      falls. Differencing raw numbers therefore adds a constant to every pick
#      whichever side was taken. Measured on the real card the overround goes
#      1.049 -> 1.043, which biases raw CLV DOWNWARD: the uncorrected version
#      of this test understates the model rather than flattering it, which is
#      the opposite of what was assumed when writing it. Both ends are
#      de-vigged, and `vig_report` prints what the number would have been
#      without that, which is the only way to show the correction does work.
#
#      Note what is NOT a check here, because it looks like one: CLV on a
#      RANDOMLY chosen side. After de-vigging, both ends sum to 1, so
#      `clv_away == -clv_home` identically and a random side averages to zero
#      whatever is wrong upstream. That null is a TAUTOLOGY, not evidence, and
#      it was written and deleted here rather than shipped as reassurance.
#   2. **The join must be verified by SCORE.** Clubs play series, so an
#      off-by-one-day join matches on teams and a date both (3d.1). This reuses
#      `odds_by_game`, which drops doubleheaders rather than guessing, and then
#      re-checks the archive's own final score.
#   3. **The opener's AGE decides what is being beaten.** If the line is hung
#      AFTER our board cutoff we are not beating a market, we are beating a
#      stale number while holding newer information. The packed outcomes carry
#      `t0`, so the lag from cutoff to open is measured and printed.
#   4. **A line quoted at only one end cannot be differenced.** `both_ends`
#      forces the totals line to be priced at open AND close, otherwise the
#      selected line silently changes between the two.
#
# The model's side is all this needs — not its probability — so it runs off the
# CACHED backtest arms with no re-simulation. That matters for totals: the
# cache carries `model_total` (the implied MEDIAN line, the right quantity to
# compare against a book's line) but no distribution, so `p_over` at an
# arbitrary line is unavailable and is not needed.

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
                    min_books: int = MIN_BOOKS_FOR_CLV_OPEN) -> Optional[dict]:
    """De-vigged OPEN and CLOSE probabilities for a two-way line.

    Both ends de-vigged, which is the whole point — see this section's header.
    Returns None unless both ends are fully priced by `min_books` books.
    """
    if not ln:
        return None
    outs = ln["o"]
    if min(o.get("b", 0) for o in outs) < min_books:
        return None
    od = [o.get("o") for o in outs]
    cd = [o.get("a") for o in outs]
    if not all(od) or not all(cd):
        return None
    op, cp = devig(od), devig(cd)
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


def clv_vs_opening(bt: dict, season: int = 2026,
                   min_books: int = MIN_BOOKS_FOR_CLV_OPEN,
                   save_dir: Path = SAVE_DIR) -> dict:
    """Score a backtest against the market's OPEN -> CLOSE movement.

    For every game the model is compared to the de-vigged OPENING price; the
    side it prefers is backed, and CLV is how far the de-vigged CLOSING price
    moved toward that side. Moneyline is in probability, totals in both
    probability (at the opening main line) and RUNS (the line's own move).

    Positive CLV means the market ended up agreeing with the model more than it
    did at the open, which is evidence the model carries information the
    opening price did not — the sharpest instrument available here, and it does
    not need a single game result.
    """
    events = load_event_odds(season, save_dir)
    book = odds_by_game(season, save_dir)
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
        row = book.get((g["date"], g["home"], g["away"]))
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
        "open_after_cutoff": _open_after_cutoff(ml_rows),
        "moneyline": ml_rows, "totals": tot_rows,
    }


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
    out["raw"] = _clv_summary(got, "clv_raw")
    out["devigged"] = _clv_summary(got, "clv")
    return out


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
        s = _clv_summary(got)
        s["lo"], s["hi"] = lo, hi
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# The A/B HARNESS — one rate-layer change against the closing line
# ---------------------------------------------------------------------------
# **This lived in throwaway scripts and produced every headline number in
# section 3d**, which is the same defect 3b records about
# `validate_slate_vs_reality`: a harness cited by results and not present in
# the code. It is `python mlb_sim.py ab` now.
#
# Three properties are what make its output mean anything, and each is pinned
# by a test:
#   * the arms must actually DIFFER — two byte-identical result blocks are a
#     variant compared against itself, not a null;
#   * the comparison is PAIRED on the games present in every arm, with
#     identical per-game seeds;
#   * every arm runs the leak-free configuration, so a difference is the
#     change and not a difference in what leaked.

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
    # The 3-season park window. One season of park factor is mostly noise (§8),
    # so averaging is an arithmetic improvement rather than a fitted one: the
    # noise falls as sqrt(n) while the true park effect survives. Slope of the
    # target season's factor on the window mean — how much park signal SURVIVES:
    #
    #   window            1        2        3        4
    #   -> 2025 slope   +0.265   +0.406   +0.611   +0.667
    #   -> 2026 slope   +0.342   +0.463   +0.597   +0.553
    #
    # w=3 nearly DOUBLES it over w=1 and is where the two targets agree; w=4
    # splits between them. It is also what Savant publishes. Needs park factors
    # back to `season - lag - 2`, which is why 2021-2023 were built.
    # The arsenal stuff prior, ISOLATED at its shipped configuration. §3d.8's
    # +1.14 was measured by `ab_ars.py`, which set STUFF_RELIABILITY at RUNTIME
    # while `stuff_stabilize` captured it as a frozen default — so that run used
    # arsenal FEATURES against five-column RELIABILITIES. A hybrid, not what
    # ships. This arm turns the prior off against the current shipped model.
    "nostuff": {"USE_STUFF_PRIOR": False},
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
    # `batprior` closes only ~15% of the gap in the subset it was built for
    # (thin-underdog lineups: +1.271 -> +1.080 runs, t +3.65 -> +3.11), and
    # the suspect is that the two priors are fighting over the SAME players.
    # `MILB_MLB_PA_GATE = 150` fires the Triple-A prior on exactly the callups
    # the playing-time prior is marking down, the code has it DISPLACE that
    # prior outright on a big Triple-A line, and section 5 item 4 already
    # records the shipped credits as 1.3-2.7x too high because they were
    # fitted "AAA vs league" and are applied with the player's own MLB record
    # competing. So the playing-time prior marks a callup to replacement and
    # the Triple-A prior hands most of it back.
    #
    # `-aaafit` is the refitted credit; `-noaaa` is the BRACKET — the most
    # that removing the interaction could possibly be worth. Reading only the
    # refit would leave "is 15% simply all there is?" unanswered.
    "batprior-aaafit": {"USE_BAT_PRIOR": True, "BAT_PRIOR_CENTRED": True,
                        "MILB_CREDIT_SPEC": "applied"},
    "batprior-noaaa": {"USE_BAT_PRIOR": True, "BAT_PRIOR_CENTRED": True,
                       "USE_MILB_PRIOR": False},
    # --- the OTHER half of the heavy-favourite gap (4e) -------------------
    # The gap decomposes into the underdog's OFFENCE being over-projected
    # (-0.435 runs, which `batprior` addresses) and the favourite's offence
    # being UNDER-projected (+0.836, t +3.30) — i.e. the underdog's RUN
    # PREVENTION is over-rated. Regressing (actual - model) opponent runs on
    # the defender's OAA and bullpen SIERA jointly over 8,050 team-games, the
    # two are nearly uncorrelated (-0.119) and both survive:
    #
    #   OAA         -0.004676 +- 0.001654  (t -2.83)  -> 0.505 runs/game
    #                                                    best-to-worst MISSING
    #   pen SIERA   +0.296818 +- 0.115138  (t +2.58)
    #
    # `OAA_TO_BIP_SHIFT` was "sized from the OAA definition, not fitted", and
    # was lowered from 0.00022 to 0.00015 on the reasoning that 0.75 runs was
    # "50% hot". The data says the true swing is ~1.0 runs and BOTH values were
    # too low. 0.00030 is the fitted magnitude.
    #
    # **The same-season OAA this was fitted on is partly endogenous** — it
    # contains the very games being scored — and the LAGGED slope is a null
    # (+0.000288, t +0.17). So a `TEAM_CONTEXT_LAG = 1` backtest cannot
    # validate it and the fitted size is an upper bound. It is still the right
    # term for ORIGINATION, where `TEAM_CONTEXT_LAG = 0` and Savant publishes
    # OAA in-season.
    "oaa2x": {"OAA_TO_BIP_SHIFT": 0.00030},
    # Club quality — the residual loading the bottom-up build does not carry.
    # `teamq` is the fitted 0.089; the others bracket it, because 0.089 came
    # off a t +1.24 all-games regression and the signal lives in the tail.
    "noteamq": {"TEAM_QUALITY_GAIN": 0.0},
    # --- pricing the 4i bullpen RAKING, after the fact ---------------------
    # The raking shipped UNFLAGGED (it is a sign-error repair, not a modelling
    # choice), so there is no switch to A/B it with. The reference arm was
    # recovered instead of rebuilt: **cached `teamq` IS the incumbent.** It was
    # run with TEAM_QUALITY_GAIN = 0.089 — today's shipped value — a few hours
    # BEFORE the raking existed, so it is exactly "shipped config minus the
    # raking", and it carries the `joint` histogram so it scores on `diff`.
    #
    # `armrake` overrides NOTHING: it is the current shipped configuration
    # under its own filename. A fresh `base` would have been the same run, but
    # would OVERWRITE `bt*_base_2000.json`, which predates `teamq` and is the
    # only surviving "before" for that change. Never --fresh an arm that is
    # itself somebody else's reference.
    #
    #   python mlb_sim.py ab --arm armrake --fresh
    #   python mlb_sim.py diff --arm teamq --arm armrake
    "armrake": {},
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
    # measured against the open is partly measuring them rather than the
    # model. Both are ablated to the state a genuine pre-lineup, pre-weather
    # projection would be in, and each separately so the two can be attributed:
    #
    #   * weather — `_slate_weather` is StatsAPI's game-time OBSERVATION. The
    #     opener is hung a median ~1.1 days earlier off a forecast, and the
    #     CLOSE does not have it either: nobody knows the first-pitch
    #     temperature and wind until first pitch. Zeroing both coefficients
    #     puts every game at its own park's REFERENCE conditions, which is
    #     "we have no weather information" rather than "the weather was
    #     average";
    #   * lineup — the posted nine, up a few hours before the game.
    #
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
    # **The multi-season rate blend, per side, scored for the first time.**
    # It has shipped on since the module was written and nobody has ever asked
    # what it is worth. Section 5b recorded the hitter side as effectively off
    # for want of boards; the boards arrived 2026-08-16/18, so the hitter arm
    # is now a real comparison rather than a description of the disk.
    #
    # Per SIDE and not one combined arm, because the two are not the same
    # question: a pitcher's season is ~180 TBF of relief or ~600 of starting
    # and stabilises 2-6x slower than a hitter's, so he has far more to gain
    # from another year. If both moved together a combined arm could not say
    # which.
    # The Triple-A prior (9c/5.11). It ships OFF — the invariant test fails
    # with it on — so this arm is the "after" and `base` is the incumbent.
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
    # A residual correction on the nine-outcome vector, trained on 630,420
    # real plate appearances against the vector THIS ENGINE would have
    # produced. It beat the incumbent at the PA level on two test seasons it
    # never saw (+0.00207 on 2025, +0.00302 on 2026, against a rate layer
    # worth 0.0326 in total) — which is the reason these arms exist and NOT a
    # reason to ship anything. Section 8 of the experiment plan is explicit: a
    # PA-level win that does not survive aggregation into a moneyline is not
    # an argument for adding a model.
    #
    # `ML_MODEL_FOLD` is walk-forward. The 2026 backtest must be priced by the
    # model trained on 2023-24 and validated on 2025 ("f26"); pricing it with
    # a model that saw 2026 would be a leak of exactly the kind this whole
    # harness exists to prevent. `ab_run_arm` sets it per season.
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
    # served six nodes whose training sets span 8,028 to 325,841 rows.
    # `min_data_in_leaf = 500` is 6.2% of the 3B node's entire training set,
    # capping it near 16 leaves however high `num_leaves` is; XBH/f26
    # early-stopped at 16 rounds. A hand probe moved every one of the six
    # nodes, all toward SMALLER trees and a LOWER learning rate — which is
    # what a residual on a strong prior should want.
    #
    # The configuration was selected on fold f25's VALIDATION season (2024)
    # and nothing else, so it leaks into neither test season. This is the ONLY
    # difference from `hier25`: same nodes, same alpha, same everything
    # downstream. Anything it moves is the fit, not the architecture.
    "hier25tuned": {"RATE_MODEL": "blend", "ML_HIER_NODES": "all",
                    "ML_BLEND_ALPHA": 0.25, "ML_NODE_PARAMS": "tuned"},
    # --- the GAME-STATE residual (4b.5) -----------------------------------
    # `hier25v2` is the CONTROL: the hierarchy retrained on the current
    # baseline, no state. The cached `hier25` cannot serve as one — it predates
    # the joint histogram, the bullpen raking AND the park-decontam baseline,
    # so three things differ at once.
    #
    # `hier25state` is the same model with BASE-OUT served. Measured at PA
    # level on both TEST seasons it is +33% on the residual's whole
    # contribution, and every f26 node improved (BB nearly TRIPLED, 0.000435 ->
    # 0.001190 — walk rate is strongly base-out dependent, which is exactly
    # what a base-out-blind rate layer cannot express).
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


def ml_fold_span(fold: str) -> str:
    """Human description of what a fold saw, so a live run says it out loud."""
    import mlb_ml
    tr, va, _ = mlb_ml.FOLDS[fold]
    return f"{'+'.join(str(s) for s in tr)}, validated {va}"

# **HISTORICAL arms: scored, never re-run.** Some changes are CODE rather than
# a constant — the posted-lineup fallback fix lives inside `_game_side` and no
# flag can toggle it — so the only "before" that exists is a run made while the
# old code was present. Those runs are kept and compared against, which prices
# a code change for free instead of re-deriving it behind a new flag.
#
# The danger is obvious and is why these are a separate dict: regenerating one
# with today's code would produce TODAY's model under a name that claims to be
# the old one, and the result would look like a clean null. `--fresh` must not
# touch them and `ab_run_arm` refuses to build them.
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
# shipped configuration, not a variant: it exists so a fresh run of today's
# code does not overwrite `bt*_base_2000.json`, which predates
# `TEAM_QUALITY_GAIN` and is the only surviving reference for that change
# (never `--fresh` an arm that is somebody else's reference). A snapshot is
# listed here rather than silently exempted from
# `test_ab_base_arm_IS_the_shipped_model`, which is otherwise right that an
# empty arm is base under another name.
AB_SNAPSHOT_ARMS: frozenset = frozenset({"armrake"})

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
    season; framing is ABLATED rather than lagged because Savant's board
    returns the current season for every `year`, so a "lagged" framing file is
    a leak wearing the label of the fix for it (§3d.2).

    The stuff-model cache is cleared per arm: it is keyed on season, and its
    feature WIDTH changes with `STUFF_USE_ARSENAL`, so a model carried across
    arms would mis-index or raise depending on which ran first.

    **Every constant ANY arm touches is restored to its shipped value first.**
    Applying an arm's overrides without undoing the previous arm's makes the
    result order-dependent: `base` sets `USE_STUFF_PRIOR = False`, then
    `shipped` overrides nothing, inherits that False and runs the same model
    twice. That is precisely the failure this harness exists to detect — two
    byte-identical result blocks — and it shipped inside the detector itself.
    Caught by a smoke run whose arms agreed to the last digit; the standing
    rule is to treat exact agreement as a bug report, never as a null.
    """
    global TEAM_CONTEXT_LAG, FRAMING_TILT_SCALE, PARK_RUN_SEASON
    TEAM_CONTEXT_LAG = 1
    PARK_RUN_SEASON = season
    for k, v in _ab_shipped_defaults().items():
        globals()[k] = v
    for k, v in overrides.items():
        globals()[k] = v
    # **Framing is ablated only because SAVANT'S board cannot be lagged**, and
    # that reason expires the moment a lagged series exists. The pitch-level
    # model is date-aware, so `TEAM_CONTEXT_LAG` reaches it like any other
    # term and framing can finally be MEASURED rather than switched off.
    #
    # Decided AFTER the overrides, and deliberately not expressed as an arm
    # override of `FRAMING_TILT_SCALE`. `_ab_shipped_defaults` snapshots every
    # constant ANY arm touches and restores it for EVERY arm — so one arm
    # naming `FRAMING_TILT_SCALE` would hand `base` its shipped 0.6394 and
    # silently turn framing on for the baseline, which is the order-dependence
    # this function's own docstring warns about.
    FRAMING_TILT_SCALE = FRAMING_TILT_SHIPPED if USE_PITCH_FRAMING else 0.0
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
            print(f"  {season} {name:8s} cached  ({path.name})"
                  + ("  ** STALE: built under different constants; "
                     "re-run with --fresh before reading it against a "
                     "freshly built arm **" if stale else ""), flush=True)
        return got
    _progress(f"ab: {season} {name} starting, {reps} sims/game")
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
    _progress(f"ab: {line}")
    bt["_constants"] = fp
    with open(path, "w") as fh:
        json.dump(bt, fh)
    return bt


def _ab_fingerprint() -> str:
    """A digest of every constant an arm's model is made of.

    **A cached arm is only comparable to one built from the same code.** The
    `--fresh` flag exists because of that and the help text says so, but it is
    a thing a person has to remember, and forgetting it does not fail — it
    produces two clean-looking result blocks whose difference is partly the
    change under test and partly whatever else moved in between. Measuring the
    base-running constants moved five of them at once, which would have
    silently re-priced every arm on disk against a new `base`.

    Stamped into the arm file and checked on every cache hit. It is captured
    AFTER `ab_configure`, so an arm's own overrides are part of its identity
    and two different arms are expected to differ.
    """
    o = _slate_overrides()
    blob = json.dumps({k: o[k] for k in sorted(o)},
                      default=str, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _ab_ll(p: float, y: float) -> float:
    return -(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9)))


def _ab_paired(d: Sequence[float]) -> Tuple[float, float, float]:
    n = len(d)
    if n < 2:
        return 0.0, float("nan"), float("nan")
    mu = statistics.mean(d)
    se = statistics.pstdev(d) / n ** 0.5
    return mu, se, (mu / se if se else float("nan"))


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
        picks = {a: clv_vs_closing(bt, season, edge=0.03, price="avg",
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
        lm = [_ab_ll(p, o) for p, o in zip(mkt, y)]
        print(f"    {'arm':9s} {'log-loss':>10s} {'vs mkt t':>9s} "
              f"{'vs base':>9s} {'ROI':>8s}")
        print(f"    {'market':9s} {statistics.mean(lm):10.5f}")
        lb = None
        for a in arms:
            xp = [rows[a][k]["model_home"] for k in shared]
            lx = [_ab_ll(p, o) for p, o in zip(xp, y)]
            t_mkt = _ab_paired([u - v for u, v in zip(lm, lx)])[2]
            t_base = (float("nan") if lb is None else
                      _ab_paired([u - v for u, v in zip(lb, lx)])[2])
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
    lm = [_ab_ll(p, o) for p, o in zip(pool["base"]["m"], pool["base"]["y"])]
    print(f"    {'arm':9s} {'log-loss':>10s} {'vs mkt t':>9s} "
          f"{'vs base':>9s} {'ROI':>8s}")
    print(f"    {'market':9s} {statistics.mean(lm):10.5f}")
    lb = None
    for a in arms:
        d = pool[a]
        lx = [_ab_ll(p, o) for p, o in zip(d["x"], d["y"])]
        t_mkt = _ab_paired([u - v for u, v in zip(lm, lx)])[2]
        t_base = (float("nan") if lb is None else
                  _ab_paired([u - v for u, v in zip(lb, lx)])[2])
        print(f"    {a:9s} {statistics.mean(lx):10.5f} {t_mkt:+9.2f} "
              f"{t_base:+9.2f} {statistics.mean(d['ret']):+8.4f}")
        if lb is None:
            lb = lx
    print("\n  A pooled t inside ~2 is not an edge, and an arm that helps one "
          "season while\n  hurting the other is noise however good the pooled "
          "number looks (3d.1, 3d.3).\n  Same sign in BOTH seasons is the bar.")


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
            s = summarize_clv_bucket(sel)
            s["lo"], s["hi"] = lo, hi
            out.append(s)
    return out


def _corr(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    if len(a) < 3 or len(a) != len(b):
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return num / (da * db) if da and db else None


# ===========================================================================
# 17c. BMIELKE — a CONTACT-QUALITY prior for thin-sample hitters
# ===========================================================================
# The rate layer regresses every hitter toward the LEAGUE, because
# `PRIOR_SIDES` is pitchers only (§5.9: a hitter arrives through the posted
# lineup, which is already a strong selection, so a playing-time prior on top
# counts it twice). That reasoning is still right — but "no playing-time
# prior" was silently taken to mean "no prior at all", and league average is a
# poor description of a hitter we have 40 plate appearances of.
#
# It matters more than the rookie count suggests. Only 4.8% of lineup slots
# carry under 50 effective PA, but the MEDIAN is 423 — and against the
# measured stabilisation (§3d.5) a 423-PA hitter is still 85% league on
# doubles and 63% on home runs. The prior is doing most of the work for most
# of the lineup, most nights.
#
# BMIELKE is the user's own metric and it is built for exactly this: bat
# speed, attack angle, intercept depth and whiff rate are measured on SWINGS
# rather than on outcomes, so they stabilise far faster, and it already
# carries a two-stage shrinkage that puts the player's own PRIOR SEASON
# underneath this season's balls in play. See `bmielke_core`.
#
# **As-of costs one fetch per player-season, not one per cutoff.** The Savant
# detail CSV carries `game_date` on every row, so a single ~2 MB pull covers
# every cutoff by filtering in memory. Trimmed to the eight fields the metric
# reads, a season of ~640 hitters is ~40 MB rather than ~1.3 GB.

BMIELKE_DIR = SAVE_DIR / "bmielke"
SAVANT_DETAIL_URL = (
    "https://baseballsavant.mlb.com/statcast_search/csv"
    "?hfPT=&hfAB=&hfGT=R%7C&hfPR=&hfZ=&hfStadium=&hfBBL=&hfNewZones=&hfPull="
    "&hfC=&hfSea={season}%7C&hfSit=&player_type=batter"
    "&hfOuts=&hfOpponent=&pitcher_throws=&batter_stands=&hfSA=&min_pitches=0"
    "&min_results=0&group_by=name&sort_col=pitches"
    "&player_event_sort=api_p_release_speed&sort_order=desc&min_abs=0"
    "&type=details&player_id={pid}"
)
# What `bmielke()` reads, PLUS the launch angle and the realised event, which
# the contact->outcome mapping needs (§3d.7). Everything else in that CSV is
# ~90% of its bytes and none of its information here.
#
# **The cache directory is VERSIONED.** Adding a field to a cache that already
# has thousands of files is the classic silent corruption: the old files parse
# fine, the new field reads None everywhere, and the model quietly runs on a
# constant. A new version means a new directory and no ambiguity.
_BM_FIELDS = ("date", "desc", "bat_speed", "attack_angle", "icept_y",
              "ev", "la", "hc_x", "hc_y", "xwoba", "event")
BM_CACHE_VERSION = "v2"


def bmielke_detail_path(pid: int, season: int,
                        save_dir: Path = SAVE_DIR) -> Path:
    return (Path(save_dir) / "bmielke" / BM_CACHE_VERSION / f"{season}"
            / f"{pid}.json.gz")


_BM_DETAIL: Dict[tuple, List[dict]] = {}


def fetch_bmielke_detail(pid: int, season: int, save_dir: Path = SAVE_DIR,
                         timeout: float = 60.0,
                         allow_fetch: bool = False) -> List[dict]:
    """One hitter's season of pitch detail, trimmed and cached gzipped.

    Memoised in process as well as on disk: a backtest asks for the same
    hitter once per CUTOFF, and re-reading plus re-inflating a 60 KB gzip
    twenty times per player is most of the cost of the whole prior.
    """
    key = (int(pid), int(season))
    got = _BM_DETAIL.get(key)
    if got is not None:
        return got
    path = bmielke_detail_path(pid, season, save_dir)
    if path.exists():
        try:
            with gzip.open(path, "rt") as fh:
                data = json.load(fh)
            _BM_DETAIL[key] = data
            return data
        except (OSError, ValueError):
            pass

    def fnum(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # **A cache MISS must not become a network call here.** `bmielke_asof` runs
    # inside `build_rates`, which runs inside a pool worker: a player who was
    # never pre-cached would trigger a live 8-second Savant fetch in the middle
    # of a backtest. Measured before this guard: building ONE cutoff's rate
    # table took 420s against 0.3s, because ~235 board players are not lineup
    # regulars and had no file. Pre-fetching is an explicit step
    # (`fetch_bmielke_season`); everything else reads what is on disk.
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
                    "bat_speed": fnum(row.get("bat_speed")),
                    "attack_angle": fnum(row.get("attack_angle")),
                    "icept_y": fnum(row.get(
                        "intercept_ball_minus_batter_pos_y_inches")),
                    "ev": fnum(row.get("launch_speed")),
                    "la": fnum(row.get("launch_angle")),
                    "event": row.get("events") or "",
                    "hc_x": fnum(row.get("hc_x")),
                    "hc_y": fnum(row.get("hc_y")),
                    "xwoba": fnum(row.get("estimated_woba_using_speedangle")),
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


def fetch_bmielke_season(pids: Sequence[int], season: int, workers: int = 10,
                         save_dir: Path = SAVE_DIR,
                         verbose: bool = True) -> int:
    """Cache the detail for a list of hitters. Idempotent — skips what exists."""
    todo = [p for p in pids
            if not bmielke_detail_path(p, season, save_dir).exists()]
    if verbose:
        print(f"[bmielke] {season}: {len(todo)} of {len(pids)} to fetch",
              flush=True)
    got = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, rows in enumerate(
                ex.map(lambda p: fetch_bmielke_detail(p, season, save_dir,
                                                  allow_fetch=True),
                       todo), 1):
            got += bool(rows)
            if verbose and i % 25 == 0:
                _progress(f"bmielke {season}  {i}/{len(todo)}  ({got} with rows)")
    if verbose:
        print(f"[bmielke] {season}: {got}/{len(todo)} returned rows")
    return got


_BM_CACHE: Dict[tuple, Optional[dict]] = {}


def bmielke_asof(pid: int, season: int, as_of: Optional[str] = None,
                 save_dir: Path = SAVE_DIR) -> Optional[dict]:
    """BMIELKE for one hitter using only pitches STRICTLY BEFORE `as_of`.

    The prior comes from the season before, whole — it finished before any
    game being priced, so it leaks nothing.
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
        xw = [r["xwoba"] for r in prev
              if r.get("ev") is not None and r.get("hc_x") is not None
              and r.get("xwoba") is not None]
        if xw:
            prior_w, prior_n = sum(xw) / len(xw), len(xw)
    out = bmielke_core.bmielke(rows, prior_w, prior_n)
    _BM_CACHE[key] = out
    return out


# --- turning a contact-quality estimate into a PRIOR VECTOR ----------------
# wOBA weights, linear-weights scale. Only the ratios matter here, because the
# vector is renormalised.
WOBA_W = {"1B": 0.883, "2B": 1.244, "3B": 1.569, "HR": 2.004}


def league_wobacon(league: Sequence[float]) -> float:
    """League expected wOBA per ball in play, from a baseline vector."""
    bip = (league[S1B] + league[S2B] + league[S3B] + league[HR]
           + league[GB_OUT] + league[AIR_OUT])
    if bip <= 0:
        return BMIELKE_LG_WOBACON_FALLBACK
    return (WOBA_W["1B"] * league[S1B] + WOBA_W["2B"] * league[S2B]
            + WOBA_W["3B"] * league[S3B] + WOBA_W["HR"] * league[HR]) / bip


BMIELKE_LG_WOBACON_FALLBACK = 0.3807
# How far a BMIELKE reading is allowed to move the contact prior. 1.0 uses it
# at face value; the metric is already shrunk twice internally, so this is a
# safety rail rather than a fitted parameter and it ships at 1.0.
BMIELKE_PRIOR_SCALE = 1.0
# Off until the A/B says otherwise. Uppercase, so `_slate_overrides` ships it
# to the pool. Named for what it is: the per-hitter CONTACT profile of §3d.7,
# not §3d.6's single BMIELKE multiplier, which is retired.
USE_CONTACT_PRIOR = False


def bmielke_relative(pids: Sequence[int], season: int,
                     as_of: Optional[str] = None,
                     save_dir: Path = SAVE_DIR) -> Dict[int, float]:
    """{pid: relative contact quality, 1.0 = this population's average}.

    **Three things had to be right here and the first draft got two of them
    wrong**, both in the direction that quietly moves the run level:

    1. **Use `raw`, not `wobacon`.** `bmielke()` returns both: `wobacon` is the
       hitter's RAW observed xwOBAcon and `raw` is the model's PREDICTION. The
       signal test scored `raw` at corr +0.70 against `wobacon`'s +0.56 — so
       wiring `wobacon` shipped the weaker of the two after validating the
       stronger. Validate one thing and ship another and the measurement means
       nothing.
    2. **Divide by `_bmielke_ref(n)`, not the league constant.** `raw` lives on
       the model's own scale, whose reference mean is ~0.3729 and which the
       metric deliberately varies with sample size; `BMIELKE_LG_WOBACON` is
       0.3807. Dividing by the wrong one put the population at 0.955 — every
       hitter 4.5% below league — and cost 0.146 runs a game.
    3. **CENTRE on the population it is applied to.** Even with the right
       reference the lineup population reads 0.975 rather than 1.000, because
       the reference is anchored on 2025 regulars and this is a different set
       of hitters in a different year. Uncentred, that is a league-wide tilt
       wearing the clothes of a player adjustment — the fifth instance of the
       trap §8 records after fatigue, the park term, the platoon gap and the
       fatigue opening penalty.

    Centring is over the PLAYERS, unweighted, because the prior is applied per
    player rather than per plate appearance and the quantity being neutralised
    is the average tilt handed to a hitter.
    """
    rel: Dict[int, float] = {}
    for pid in pids:
        bm = bmielke_asof(int(pid), season, as_of, save_dir)
        if not bm:
            continue
        ref_mean, _ = bmielke_core._bmielke_ref(bm["bbe"])
        if ref_mean > 0:
            rel[int(pid)] = bm["raw"] / ref_mean
    if not rel:
        return {}
    centre = statistics.mean(rel.values())
    if centre <= 0:
        return {}
    return {pid: v / centre for pid, v in rel.items()}


def bmielke_prior(league: Sequence[float], rel: float) -> List[float]:
    """`league`, retuned to a hitter's RELATIVE contact quality.

    `rel` is centred so that 1.0 is the population average — see
    `bmielke_relative`. It must be a RATIO and never an absolute xwOBAcon:
    Savant's scale (league 0.3807) and this baseline's wOBA-on-contact (0.3575)
    are different quantities, and passing one as the other reads every hitter
    as 6.5% better than league and lifts the whole run environment.

    Scales the four HIT outcomes by one factor and absorbs the difference in
    the batted-ball OUTS, so the contact mass is conserved and the strikeout,
    walk and hit-by-pitch rates are untouched — those already stabilise
    correctly (measured 55/125/250 against a shipped 60/120/240), so there is
    nothing for a contact model to add there and everything to break.

    Scaling the hit types PROPORTIONALLY is a deliberate first cut and is
    known to be imperfect: better contact skews toward extra bases more than
    toward singles, so this under-rates the power end. The honest version reads
    the player's own launch-condition distribution; this one is testable today.
    """
    if rel <= 0 or league_wobacon(league) <= 0:
        return list(league)
    f = 1.0 + (rel - 1.0) * BMIELKE_PRIOR_SCALE
    hits = league[S1B] + league[S2B] + league[S3B] + league[HR]
    outs = league[GB_OUT] + league[AIR_OUT]
    if outs <= 0 or hits <= 0:
        return list(league)
    # conserve the contact mass: what the hits gain, the outs give up
    g = (hits + outs - f * hits) / outs
    if g <= 0:
        return list(league)
    out = list(league)
    for i in (S1B, S2B, S3B, HR):
        out[i] = league[i] * f
    for i in (GB_OUT, AIR_OUT):
        out[i] = league[i] * g
    return _normalize(out)


# ===========================================================================
# 17d. CONTACT -> OUTCOME — the league mapping (sim_state.md 3d.7)
# ===========================================================================
# §3d.6 put a hitter's contact quality into the prior as ONE multiplier over
# 1B/2B/3B/HR, and it made the moneyline worse while making totals better.
# The reason is that one multiplier says a hitter whose extra quality is
# singles and one whose extra quality is home runs are the same hitter, and
# they are worth very different runs — so the DIFFERENCE between two teams
# picks up noise even as the SUM improves.
#
# The fix is to stop guessing the split and read it: for each batted ball,
# what does a ball hit that hard, at that angle, in that direction actually
# BECOME, league-wide? Average over a hitter's own batted balls and his
# expected outcome vector falls out with no multiplier anywhere. That is
# BallparkPal's "C-Only" contact model in substance.
#
# **Definitions are matched to `outcome_counts`, deliberately and exactly**,
# because a prior on one definition blended with observations on another is
# silently wrong:
#   * the board's outs are `PA - SO - BB - HBP - H`, which INCLUDES reached-on
#     -error, sacrifice flies, sacrifice bunts and fielder's choice. The engine
#     has no error outcome (`P_REACH_ON_ERROR` turns a fraction of ground outs
#     into reaches later), so every one of those is an OUT here too;
#   * outs split ground/air by Savant's `bb_type`, which is the same GB/LD/FB
#     taxonomy FanGraphs' columns use — NOT by a launch-angle threshold of our
#     own, which would be a third convention.
#
# **Nothing park-dependent may enter.** `hit_distance_sc` encodes the park and
# the weather, both of which the engine applies separately, so it is not a
# feature. The mapping is a league-average park by construction.

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
    # outside [0, 90] threw away 8.3% of batted balls and did it NON-RANDOMLY:
    # 17.8% of the discards were doubles against 5.3% of those kept, because a
    # ball down the line is both the most likely to compute slightly foul and
    # the most likely to go for extra bases. It dragged the league doubles rate
    # from 6.2% to 5.3% — a bias built straight into the mapping.
    #
    # A ball at polar -8 is a left-field-line ball whose coordinates are a
    # degree or two off; one at -45 is behind the plate and is bad data. So
    # tolerate a margin and clamp, reject beyond it.
    if polar < -CONTACT_POLAR_TOL or polar > 90.0 + CONTACT_POLAR_TOL:
        return None
    polar = min(max(polar, 0.0), 90.0 - 1e-9)
    sbin = min(int(polar / (90.0 / CONTACT_SPRAY_BINS)),
               CONTACT_SPRAY_BINS - 1)
    return e, a, sbin


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
    root = Path(bbe_dir) if bbe_dir else Path(__file__).resolve().parent
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
                b = _contact_bins(_fnum(row.get("launch_speed")),
                                  _fnum(row.get("launch_angle")),
                                  _fnum(row.get("hc_x")),
                                  _fnum(row.get("hc_y")))
                if b is None:
                    continue
                cls = _contact_class(row.get("events"), row.get("bb_type"))
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


def contact_lookup(cmap: dict, ev, la, hc_x, hc_y) -> Optional[List[float]]:
    """The outcome distribution for one batted ball, most specific first."""
    b = _contact_bins(ev, la, hc_x, hc_y)
    if b is None:
        return None
    v = cmap["cell"].get(b)
    if v is not None:
        return v
    v = cmap["pair"].get((b[0], b[1]))
    if v is not None:
        return v
    return cmap["la"].get(b[1], cmap["global"])


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
CONTACT_SHRINK_BBE = 120.0


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
        v = contact_lookup(cmap, r.get("ev"), r.get("la"),
                           r.get("hc_x"), r.get("hc_y"))
        if v is None:
            continue
        n += 1
        for i in CONTACT_CLASSES:
            acc[i] += v[i]
    if n < CONTACT_MIN_BBE:
        return None
    return [a / n for a in acc], n


def contact_prior(league: Sequence[float], profile: Sequence[float],
                  n: int, lg_profile: Sequence[float]) -> List[float]:
    """`league`, with its BALL-IN-PLAY mass redistributed by a hitter's profile.

    K, BB and HBP are untouched — a contact model has nothing to say about
    them and they already stabilise correctly. The in-play mass is held exactly
    constant and only its SHAPE moves, so this cannot shift a hitter's contact
    RATE, only what his contact turns into. That is the whole difference from
    §3d.6's single multiplier, which moved the shape and the rate together and
    could not tell a singles hitter from a slugger.

    `lg_profile` is the population's own average profile, so the ratio is
    relative and a year in which batted balls simply carry further cannot leak
    in as everyone being better — which out of sample is worth +2.65% of
    wOBAcon (§3d.7).
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


_CMAP_CACHE: Dict[tuple, dict] = {}


def contact_map_for(season: int, save_dir: Path = SAVE_DIR) -> Optional[dict]:
    """The league map a run scoring `season` is allowed to use.

    Strictly earlier seasons only — the map is league knowledge that could
    have been had before the season started, and fitting it on the season
    being scored would be the same leak as a season-final board.
    """
    key = (int(season), str(save_dir))
    if key in _CMAP_CACHE:
        return _CMAP_CACHE[key]
    root = Path(__file__).resolve().parent
    have = sorted(int(p.stem.rsplit("_", 1)[1]) for p in
                  root.glob("savant_bbe_*.csv"))
    use = [y for y in have if y < season]
    out = None
    if use:
        try:
            out = build_contact_map(use, save_dir)
        except RuntimeError:
            out = None
    _CMAP_CACHE[key] = out
    return out


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
    cmap = contact_map_for(season, save_dir)
    if cmap is None:
        return {}, []
    out: Dict[int, Tuple[List[float], int]] = {}
    for pid in pids:
        rows = fetch_bmielke_detail(int(pid), season, save_dir)
        if not rows:
            continue
        got = hitter_contact_profile(rows, cmap, as_of)
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


# ===========================================================================
# 18. RUNNER ADVANCEMENT — per runner, from three sources
# ===========================================================================
# Taking the extra base is not a league constant. It is a mix of the runner's
# own history, his speed, and his measured extra-base value, and all three are
# already on disk:
#
#   PBP history   `savedata/runner_advance.json` — every first-to-third,
#                 second-scores-on-a-single and first-scores-on-a-double in
#                 the season, per runner id (590 runners, 6,722 chances)
#   XBR           FanGraphs extra-bases-taken runs. corr **+0.517** with the
#                 observed rate — the single best predictor
#   Spd           Bill James speed score. corr +0.462
#
# **The raw per-runner rate is almost pure noise and must not be used
# directly.** Median depth is 10 opportunities; at a league rate of 0.357 the
# binomial sd alone is 0.15, against an observed spread of 0.147. Essentially
# all of the apparent spread between runners is sampling. Regressing it
# properly is the whole job:
#
#   prior  = league rate tilted by XBR/Spd (measured: Spd<3.5 -> 0.299,
#            Spd>5.5 -> 0.452 against a league 0.357)
#   rate   = shrink(observed, prior, n)   with the shrinkage constant set from
#            the variance decomposition, not chosen

RUNNER_ADV_PATH = SAVE_DIR / "runner_advance.json"
# Opportunities at which a runner's own history is half-believed. From
# observed var 0.0216 = true var + binomial var(0.0115 at n~20) => true sd
# ~0.10, so k = p(1-p)/true_var = 0.36*0.64/0.0101 ~ 23.
STABILIZE_ADVANCE = 23.0
# Slope of advance rate on the standardised speed/extra-base composite,
# measured from the fast/slow split: (0.452-0.299) over ~2 sd = 0.077 per sd.
ADV_PER_SD = 0.077

_ADV: Optional[dict] = None


def load_runner_advance() -> dict:
    global _ADV
    if _ADV is None:
        try:
            with open(RUNNER_ADV_PATH) as fh:
                _ADV = json.load(fh)
        except (OSError, ValueError):
            _ADV = {}
    return _ADV


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
    rec = load_runner_advance().get(str(pid or ""), {})
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
# Two things the engine had NO representation of at all: the fielders behind
# the pitcher, and the arms that stop a runner taking the extra base. The
# out-vs-hit split came entirely from the batter's and pitcher's own rates, so
# a fly ball to a Gold Glove centre fielder and one to a statue were the same
# event.
#
# Both come off Savant leaderboards and are written to MLBAnalytics as CSVs
# alongside the reliever traits:
#
#   outs_above_average  -> team OAA, shifting balls in play toward outs
#   arm-strength        -> team outfield arm, suppressing the extra base
#
# Sizing note: EffortMLB's own study put whole-team defence at about **0.2
# runs per start** between the extremes (corr -0.039 with actual-minus-expected
# wOBA on contact over 2,652 starts). It is a real effect and a SMALL one —
# anything here that moves scoring by a run is wrong.

SAVANT_OAA_URL = "https://baseballsavant.mlb.com/leaderboard/outs_above_average"
SAVANT_ARM_URL = "https://baseballsavant.mlb.com/leaderboard/arm-strength"


def _savant_csv(url: str, params: dict) -> List[dict]:
    r = requests.get(url, params=params, timeout=40)
    r.raise_for_status()
    text = r.text.lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def _savant_club(name: str, by_name: Dict[str, str]) -> Optional[str]:
    """Savant's `display_team_name` -> a board abbreviation, unambiguously.

    **This was a substring test and it corrupted every season it touched.**
    Savant sends SHORT names ("Reds", "Athletics") while the club index is
    keyed on full ones ("cincinnatireds"), so the old rule was
    `_norm_club(nm) in norm`, first match wins over a dict. Two collisions:

      * **`"---"`** is Savant's placeholder for a player who changed clubs.
        It normalises to the EMPTY STRING, and `"" in norm` is true for all 30
        clubs — so every multi-team player's outs-above-average landed in
        whichever club happened to lead the dict. That is the whole of
        Oakland's -152 in 2024 and -92 in 2023.
      * **`"Reds"`** is a substring of `"bostonredsox"` as well as
        `"cincinnatireds"`. Boston won the race, so **Cincinnati's entire OAA
        was added to Boston and CIN disappeared from the file.**

    Neither failed loudly: the output was a plausible-looking table of clubs
    with plausible-looking numbers, which is the signature this file records
    over and over. The rule is now an exact match, else a UNIQUE suffix match
    ("reds" ends `cincinnatireds` but not `bostonredsox`, and "redsox" the
    reverse), with the empty string and any ambiguity rejected outright.
    """
    key = _norm_club(name)
    if not key:
        return None
    if key in by_name:
        return by_name[key]
    hits = {a for norm, a in by_name.items() if norm.endswith(key)}
    return hits.pop() if len(hits) == 1 else None


def export_defense(season: int = 2026) -> Path:
    """Team OAA and outfield arm strength -> MLBAnalytics CSV."""
    idx = _team_index()
    by_name = {}
    for norm, rec in idx.items():
        by_name[norm] = rec["abbr"]

    oaa = _savant_csv(SAVANT_OAA_URL, {"type": "Fielder", "year": str(season),
                                       "csv": "true", "min": "10"})
    arm = _savant_csv(SAVANT_ARM_URL, {"type": "player", "year": str(season),
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


SAVANT_FRAMING_URL = "https://baseballsavant.mlb.com/leaderboard/catcher-framing"

_FRAMING: Dict[int, Dict[str, float]] = {}     # keyed on SEASON
# Whether the 'no framing file for a lagged season' notice has been said.
# A dedicated flag, NOT `not _FRAMING`: the cache is cleared and
# repopulated by other code, so tying the notice to it made the notice
# fire again every time something else touched the cache.
_FRAMING_WARNED = False


def export_framing(season: int = 2026) -> Path:
    """Per-club catcher framing runs -> MLBAnalytics/team_framing_<season>.csv.

    **One request per club, because the league-wide CSV is unusable**: its `id`
    and `name` columns come back EMPTY and it carries no team column, so there
    is nothing to join on. The `team=` filter does work, and `type=Team` is
    silently ignored (it returns the identical 60 catcher rows), so per-club
    fetching is the only route.

    Note `pitches` in that feed is FRAMING CHANCES — shadow-zone takes, ~66 per
    team-game — not total pitches. Comparing it against a season's pitch count
    makes coverage look like 44% when it is complete.

    **This endpoint IGNORES `year` — verified, not assumed.** Toronto returns
    rv_tot 15.80 over 9,723 chances for 2023, 2024, 2025 AND 2026, byte for
    byte. So there is no such thing as a prior-season framing file from here,
    and writing one would put the CURRENT season on disk under last year's
    name — a leak wearing the label of the fix for that leak. It raises
    instead. The thorough route is to rebuild framing from `statcast_search`,
    which does honour dates (§3c).
    """
    if season != datetime.date.today().year:
        raise ValueError(
            f"mlb_sim: Savant's framing leaderboard ignores `year` — asking it "
            f"for {season} returns the current season. Writing "
            f"team_framing_{season}.csv would mislabel it. Rebuild from "
            f"statcast_search if a dated version is needed.")
    path = MLBA_DIR / f"team_framing_{season}.csv"
    with open(SAVE_DIR / f"mlb_roster_{season}.json") as fh:
        teams = json.load(fh)["teams"]
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    rows = []
    for t in teams:
        abbr = normalize_club(t.get("abbreviation") or "")
        r = sess.get(SAVANT_FRAMING_URL,
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


def load_team_framing(season: int = 2026) -> Dict[str, float]:
    """{club: framing runs saved over the season}. Empty when unavailable."""
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
            # `if not _FRAMING`, i.e. coupled to a cache that other code is
            # free to populate or clear; and every `backtest()` call starts a
            # FRESH pool, so ~22 workers each said it once per arm — over a
            # hundred identical lines in one A/B. A dedicated flag fixes the
            # first. The second is fixed by not warning at all when framing is
            # switched off on purpose: `FRAMING_TILT_SCALE == 0.0` means the
            # caller asked for no framing, and missing data you asked not to
            # use is not a problem worth a line of output.
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
# `export_framing` above refuses to write a past season because Savant's
# leaderboard IGNORES `year`. That is why `ab_configure` ABLATES framing
# instead of lagging it, and why a term worth **+0.0263 of win probability**
# best-to-worst catcher (~10-15 cents of moneyline) ships live at 0.6394
# having never been through an A/B.
#
# `statcast_search` DOES honour dates and every pitch carries `fielder_2`, so
# the series is rebuildable from pitch level. Collected by `scrape_framing.py`
# (kept separate: it is a long network job, not model code) into
# `savedata/framing_pitches/v2/<season>/<pitcher>.json.gz`.
#
# **Two mistakes this code exists to not make, both of which look fine:**
#
#   * `zone` is NOT the attack zone. 1-9 is the 3x3 grid INSIDE the strike
#     zone, 11-14 are the quadrants ENTIRELY OUTSIDE it, so "shadow = 11-14"
#     scores a 4.3% called-strike rate and means nothing.
#   * A GEOMETRIC shadow band is no better: inside +/- one baseball the call
#     runs 0.995 -> 0.279 from the inner edge to the outer, a 3.6x swing, so
#     any bin averages over pitches with nothing in common and the metric
#     partly measures which pitches a catcher happened to receive.
#
# So the surface is modelled CONTINUOUSLY and the credit is `actual -
# expected` per pitch, with no zone definition in it anywhere.
#
# Validated against Savant's own published per-club numbers (r +0.95, slope
# +1.03, RMSE 1.73 runs like-for-like) — see `framing_validate_report`.

FRAMING_RUNS_PER_STRIKE = 0.125          # Statcast's published conversion
FRAMING_PITCH_VERSION = "v2"
# **Tuned by HELD-OUT log-loss, not by eye.** Smoothing counts and strikes
# separately then dividing is Nadaraya-Watson, and at a wide bandwidth it is
# badly biased here: strike mass from the dense, high-rate zone interior
# bleeds into sparse low-rate cells. At (0.10, 0.15, sigma 1.5) `expected`
# over-predicted by 0.0038 of strike rate — -130 runs across a league whose
# real spread is +/-15. Finer bins and a tighter kernel win on log-loss AND
# calibration at once, which is bias, not a bias/variance trade.
FR_X_LO, FR_X_HI, FR_X_STEP = -2.0, 2.0, 0.05
FR_Z_LO, FR_Z_HI, FR_Z_STEP = -3.0, 3.0, 0.075
FR_SIGMA = 1.0


def framing_pitch_dir(season: int, save_dir: Path = SAVE_DIR) -> Path:
    return (Path(save_dir) / "framing_pitches" / FRAMING_PITCH_VERSION
            / str(season))


def _fr_nx() -> int:
    return int(round((FR_X_HI - FR_X_LO) / FR_X_STEP))


def _fr_nz() -> int:
    return int(round((FR_Z_HI - FR_Z_LO) / FR_Z_STEP))


def load_framing_takes(season: int = 2026, upto: Optional[str] = None,
                       save_dir: Path = SAVE_DIR) -> List[dict]:
    """Every taken pitch, normalised for the model.

    `plate_x` needs no normalisation — the plate is 17 inches for everyone.
    `plate_z` does, because `sz_top`/`sz_bot` are per BATTER, so it is carried
    as `(z - mid) / half`: -1..+1 inside the zone whatever the hitter's
    height. The published reference implementation skips this and says so; the
    fields are here at 100%, so there is no reason to.

    `blocked_ball` is excluded: blocking is a different skill from receiving.
    """
    import gzip
    rows: List[dict] = []
    d = framing_pitch_dir(season, save_dir)
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
                    # keys on — SF/SFG, TB/TBR, WSH/WSN, KC/KCR, SD/SDP,
                    # AZ/ARI, CWS/CHW — so a model stored under Savant's
                    # codes hands `build_side` a miss on a QUARTER of the
                    # league. It does not raise: the lookup returns 0.0 and
                    # those clubs simply get no framing, which is why an A/B
                    # of it read as "no effect". `normalize_club` maps
                    # Savant's spelling ONTO the board's, so it has to be
                    # applied to the stored key, not to the query.
                    "club": normalize_club(
                        (x.get("home_team") if x.get("inning_topbot") == "Top"
                         else x.get("away_team")) or "")})
    return rows


def _fr_logit(p, eps: float = 1e-6):
    """ARRAY logit. Deliberately not `_logit`, which is the scalar `math`
    version used by the Triple-A translation and would silently accept an
    array and return nonsense."""
    import numpy as np
    return np.log(np.clip(p, eps, 1 - eps) / (1 - np.clip(p, eps, 1 - eps)))


def _fr_expit(x):
    import numpy as np
    return 1.0 / (1.0 + np.exp(-np.clip(x, -35, 35)))


def _fr_smooth2d(a, sigma_bins: float = FR_SIGMA, radius: int = 4):
    """Separable Gaussian blur. numpy only — scipy is not a dependency."""
    import numpy as np
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma_bins) ** 2)
    k /= k.sum()
    out = np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 0, a)
    return np.apply_along_axis(lambda m: np.convolve(m, k, mode="same"), 1, out)


def build_framing_surface(rows: Sequence[dict], sigma: float = FR_SIGMA):
    """{stance: (rate_grid, count_grid)} — smoothed empirical P(called strike).

    Fitted per batter STANCE, which moves the zone edges ~2 percentage points.
    """
    import numpy as np
    nx, nz = _fr_nx(), _fr_nz()
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
        ns, ss = _fr_smooth2d(n, sigma), _fr_smooth2d(st_, sigma)
        with np.errstate(invalid="ignore", divide="ignore"):
            rate = np.where(ns > 0, ss / np.maximum(ns, 1e-9), np.nan)
        out[stand] = (rate, ns)
    return out


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
    if not (0 <= i < _fr_nx() and 0 <= j < _fr_nz()):
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
    import numpy as np
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
    import numpy as np
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


def measure_framing(season: int = 2026, upto: Optional[str] = None,
                    save_dir: Path = SAVE_DIR, sigma: float = FR_SIGMA,
                    rounds: int = 8, with_pitcher: bool = True,
                    with_umpire: bool = True, verbose: bool = True,
                    out_path: Optional[Path] = None) -> dict:
    """Per-catcher and per-club framing runs, as of `upto`.

    Three layers: the location SURFACE, a CALIBRATION that makes the thing
    zero-sum by construction, then EB-shrunk random effects for umpire,
    pitcher and catcher fitted by coordinate ascent.

    **Why the pitcher and umpire effects.** `actual - expected` credits the
    catcher with everything location does not explain — including the
    pitcher's command and the umpire's zone. A catcher who receives
    good-command arms looks good. Statcast applies a pitcher adjustment;
    Baseball Prospectus's CSAA additionally fits umpire and batter. Umpire is
    available here at a 100% join and Statcast does not use it at all.
    """
    import numpy as np
    rows = load_framing_takes(season, upto, save_dir)
    if not rows:
        raise SystemExit(
            f"mlb_sim: no framing pitches for {season} under "
            f"{framing_pitch_dir(season, save_dir)}. Run "
            f"`python scrape_framing.py {season}` first.")
    try:
        with open(Path(save_dir) / f"umpires_{season}.json") as fh:
            ump = {int(k): v for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        ump = {}
        if with_umpire and verbose:
            print(f"[framing] no umpires_{season}.json — umpire effect OFF")
        with_umpire = False

    surf = build_framing_surface(rows, sigma)
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
        vals = sorted({fn(r) for r in rows}, key=str)
        idx = {v: i for i, v in enumerate(vals)}
        return np.array([idx[fn(r)] for r in rows]), vals

    groups = []
    if with_umpire:
        groups.append(("umpire", *codes_for(
            lambda r: ump.get(int(r["pk"]), "?") if r.get("pk") else "?")))
    if with_pitcher:
        groups.append(("pitcher", *codes_for(lambda r: r["pit"])))
    groups.append(("catcher", *codes_for(lambda r: r["c"])))

    eff = {nm: np.zeros(len(vals)) for nm, _, vals in groups}
    taus: Dict[str, float] = {}
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


def framing_validate_report(season: int = 2026,
                            path: Optional[Path] = None,
                            save_dir: Path = SAVE_DIR) -> dict:
    """Does the rebuild reproduce SAVANT's published per-club numbers?

    **The go/no-go.** The point is not to beat Statcast, it is to get a
    date-aware series; so the test is that the same code over the FULL season
    lands on Savant's own numbers, and the as-of versions then inherit that.

    Compared LIKE FOR LIKE. Savant's leaderboard applies a minimum-chances
    qualifier and `export_framing` summed only those catchers, while this
    includes every catcher — so each club is scored on its top-N by chances
    with N taken from Savant's own `catchers` column. A catcher's runs are
    pro-rated by the share of his chances at that club, because a traded
    catcher (Patrick Bailey, CLE 3,360 / SFG 2,053) belongs to both.
    """
    got = json.load(open(path or framing_model_path(season, None, save_dir)))
    ref: Dict[str, dict] = {}
    with open(MLBA_DIR / f"team_framing_{season}.csv") as fh:
        for r in csv.DictReader(fh):
            ref[normalize_club(r["team"])] = {
                "runs": float(r["framing_runs"]), "n": int(r["catchers"])}
    rows = load_framing_takes(season, got.get("upto"), save_dir)
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


def framing_repeatability_report(season: int = 2026,
                                 split: Optional[str] = None,
                                 min_chances: int = 400,
                                 save_dir: Path = SAVE_DIR) -> dict:
    """SPLIT-HALF: does adjusting for pitcher and umpire give a BETTER catcher
    estimate, or does it strip real skill?

    **Agreement with Savant cannot answer this** — Savant applies a pitcher
    adjustment and no umpire adjustment at all, so diverging from it is what
    the change is FOR. "We disagree because we are better" is a story, and
    this file's rule is that a story is not a measurement (5.11.2). So the
    instrument is the one 3d.8 used for the stuff prior: fit on the first half
    of a season, and score the estimate against what actually happened in the
    second.

    Both variants are scored the SAME way on the same held-out pitches, with
    only the catcher term differing, so the comparison is of the estimate and
    nothing else.

    > **Read the two numbers separately.** Raw predictive power can FAVOUR the
    > unadjusted estimate for a bad reason: a catcher works the same staff in
    > both halves, so a metric that quietly carries his pitchers' command will
    > "predict" the second half partly by carrying it again. That is exactly
    > the confound the adjustment exists to remove, and for THIS engine it is
    > disqualifying either way — the sim already prices the pitcher's own
    > K/BB rates, so framing that contains his command double-counts it.
    """
    import numpy as np
    rows = load_framing_takes(season, save_dir=save_dir)
    if not rows:
        raise SystemExit(f"mlb_sim: no framing pitches for {season}")
    dates = sorted({r["date"] for r in rows if r["date"]})
    split = split or dates[len(dates) // 2]
    h1 = [r for r in rows if r["date"] and r["date"] <= split]
    h2 = [r for r in rows if r["date"] and r["date"] > split]
    print(f"\nFRAMING split-half — {season}, split at {split}")
    print(f"  first half {len(h1):,} takes   second half {len(h2):,}")

    try:
        with open(Path(save_dir) / f"umpires_{season}.json") as fh:
            ump = {int(k): v for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        ump = {}

    # ONE surface, fitted on the first half only, used for both variants and
    # for scoring — so nothing about the location model differs between arms.
    surf = build_framing_surface(h1, FR_SIGMA)
    y1 = np.array([r["s"] for r in h1], dtype=float)
    e1 = _fr_logit(np.array([framing_expected(r, surf) for r in h1]))
    ca, cb = fit_framing_calibration(e1, y1)

    def codes(rws, fn):
        vals = sorted({fn(r) for r in rws}, key=str)
        idx = {v: i for i, v in enumerate(vals)}
        return np.array([idx[fn(r)] for r in rws]), vals

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


# **OFF until an A/B says otherwise.** The pitch-level series is better
# measured (split-half: held-out logloss 0.124600 against the unadjusted
# 0.124704, correlation with the held-out half +0.487 against +0.447) but
# "better measured" is not "prices better", and nothing in this file ships on
# the first of those. What it unlocks matters more than its accuracy: framing
# has been ABLATED in every backtest ever run, because Savant's leaderboard
# ignores `year` and a lagged file could not be built. A date-aware series can
# be lagged, so framing becomes A/B-able for the first time.
USE_PITCH_FRAMING = False
# The as-of cutoff for the pitch-level series. **A STRING, and "" rather than
# None on purpose**: `_SLATE_OVERRIDE_TYPES` is (int, float, str, bool, tuple),
# so a None-valued global is NOT captured and would silently fail to reach a
# pool worker — the exact class of defect
# `test_pool_overrides_capture_every_tunable_constant` exists for.
FRAMING_ASOF = ""

_FRAMING_MODEL: Dict[tuple, dict] = {}


def load_framing_model(season: int = 2026, upto: Optional[str] = None,
                       save_dir: Path = SAVE_DIR) -> dict:
    """The cached pitch-level framing model, or {} when it was not built."""
    key = (int(season), upto or "")
    if key in _FRAMING_MODEL:
        return _FRAMING_MODEL[key]
    try:
        with open(framing_model_path(season, upto, save_dir)) as fh:
            _FRAMING_MODEL[key] = json.load(fh)
    except (OSError, ValueError):
        _FRAMING_MODEL[key] = {}
    return _FRAMING_MODEL[key]


def catcher_framing_per_game(catcher_id: Optional[int], season: int,
                             save_dir: Path = SAVE_DIR) -> Optional[float]:
    """THIS catcher's framing runs per game, or None when he is not on file.

    **Framing is a PLAYER skill, and the club aggregate is the wrong object.**
    A club's number is a roster property: Patrick Bailey split CLE 3,360 /
    SFG 2,053 inside one season, so last year's Cleveland figure carries the
    framing of a man now in San Francisco. Lagging THAT is measuring the wrong
    thing — which is what the first framing A/B did, and why it came back
    negative (see sim_state.md).

    Lagging a CATCHER is fine: his skill travels with him, so a prior-season
    per-catcher value is both leak-free and meaningful, and needs no as-of
    snapshot.
    """
    if catcher_id is None:
        return None
    got = load_framing_model(season, FRAMING_ASOF or None, save_dir)
    rec = (got.get("catcher") or {}).get(str(int(catcher_id)))
    if not rec or not rec.get("games"):
        return None
    return float(rec["runs"]) / float(rec["games"])


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


def load_team_defense(season: int = 2026) -> Dict[str, dict]:
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
