import pandas as pd
import numpy as np
import argparse
import sqlite3
import os
import random
import datetime
import re
import json

import jax
import jax.numpy as jnp

# Everything in this file is marginal at best

# Many rows of data that are nans to filter out for model to properly train

# Command to start and mount repo into jax/rocm docker image:

# sudo systemctl start docker.service

# docker run --interactive --tty \
#    --network=host \

#    --device=/dev/kfd --device=/dev/dri \
#    --group-add video \
#    --user 1000 \
#    --volume "$(pwd)":/NHLvacuum \
#    --workdir /NHLvacuum \
#    rocm/jax \
#    /bin/bash

# workdir must be absolute path

# if docker says something like 'Error: container ... is not running', you have to restart it
# docker start wooptyROCM

# running command in containerf
# docker exec  --interactive --tty --user 1000 --workdir /NHLvacuum  wooptyROCM     python model_test.py --db ./nhl_analytics.db --mode train --epochs 10

# docker image needs "libdw1" to run properly on GPU (sudo apt-get install libdw1)

# Train command: python model_test.py --db ./nhl_analytics.db --mode train --epochs 400
# Simulation command: python model_test.py --mode manual --home DAL --away EDM --date 2025-12-04  --n_sims 250000
# Insane validation loss with this seed: 1951054394 ; statistical magic?

# ---------------------------
# Config & Constants
# ---------------------------
DEFAULT_DB_PATH = "./nhl_analytics.db"
MODEL_PARAMS_PATH = "advanced_model_params_v6.npz"
STATS_PATH = "advanced_standardize_stats_v6.npz"
CALIBRATION_PATH = "model_calibration_v6.npz"
RANDOM_SEED = None   # Best performer from random seed testing (4237426529, val loss -0.4517)
# ---------------------------
# Preseason exclusion (2026-10-02). NST imports include preseason games (game_type 1, ids 20YY01xxxx). Every
# connection model_test opens shadows these tables with TEMP views holding regular-season + playoff rows only, so
# no rolling window, goalie form, linemate/matchup feature or training row can ever see a preseason game.
# (SQLite resolves temp objects before main ones; queries are unchanged.)
# ---------------------------
_PRESEASON_FILTERED = ['games', 'team_game_overview', 'player_game_stats', 'goalie_game_stats', 'line_combinations',
                       'player_linemate_stats', 'player_opposition_stats', 'player_onice_stats', 'player_shift_stats',
                       'edge_pbp_events']

def _connect(db_path):
    con = sqlite3.connect(db_path)
    for t in _PRESEASON_FILTERED:
        con.execute(f"CREATE TEMP VIEW IF NOT EXISTS {t} AS SELECT * FROM main.{t} "
                    f"WHERE substr(game_id, 5, 2) IN ('02', '03')")
    return con

# Rest days are capped at REST_CAP (1 = back-to-back, 2 = one day off, 3 = 2+ days off). 2026-10-02: the old
# cap of 10 let the net extrapolate on season openers (10/10 rest -> ~6 pts off the home side), while the data
# shows no effect past a few days (home win 54.0% overall vs 53.6% when both teams rested 7+).
REST_CAP = 3

_TAG = os.environ.get('NHL_MODEL_TAG')   # EXPERIMENT 2026-10: write/read a separate model (never the production files)
if _TAG:
    MODEL_PARAMS_PATH = f"advanced_model_params_v6_{_TAG}.npz"
    STATS_PATH = f"advanced_standardize_stats_v6_{_TAG}.npz"
    CALIBRATION_PATH = f"model_calibration_v6_{_TAG}.npz"
FEATURE_LIST_PATH = f"feature_list_{_TAG}.npz" if _TAG else "feature_list.npz"
if os.environ.get('NHL_SEED'):   # EXPERIMENT 2026-09: fixed seed for paired A/B runs (unset = random, as before)
    RANDOM_SEED = int(os.environ['NHL_SEED'])

# ---------------------------
# Rolling-feature mode  (EXPERIMENT 2026-09 — production default is 'window'; see roll_mean)
#   window : x.shift(1).rolling(n).mean()  — the original features, bit-identical
#   shrink : exponentially weighted mean shrunk toward a 2022-23 league prior (NHL_ROLL_PRIORS json)
#   capture: window values + records each long series' games 83-164 (= 2022-23) to build those priors
# ---------------------------
ROLL_MODE = os.environ.get('NHL_ROLL_MODE', 'window')
_ROLL_PRIORS = {}
_ROLL_CAPTURE = {}
# (half-life games, shrink games) by stat type, fitted on 2022-23 targets only (before every walk-forward fold)
_ROLL_PARAMS = {'rate': (20, 5), 'luck': (40, 40), 'special': (80, 40)}
_ROLL_CLASS = {   # explicit, by raw column name; anything unlisted is 'rate'
    'sh_pct': 'luck', 'hd_finish_pct': 'luck', 'md_finish_pct': 'luck', 'hd_save_pct': 'luck',
    'md_save_pct': 'luck', 'goals_for': 'luck', 'gsax': 'luck', 'hd_gsax': 'luck', 'rcr': 'luck',
    'hd_shot_pct': 'luck', 'pp_xg60': 'special', 'pp_efficiency': 'special', 'pk_xga60': 'special',
    'pk_xgf60': 'special', 'pk_g60': 'special'}
_KNOWN_PRIORS = {'gsax': 0.0, 'hd_gsax': 0.0}   # GSAx is 0 by construction; everything else from the 2021-22 capture

def roll_mean(x, n, min_periods=1, col=None):
    """Pre-game rolling mean of one team's (or goalie's) per-game series x, in game order.
    col = the raw column name (inside groupby.transform the series name is the GROUP key, not the column)."""
    if ROLL_MODE in ('window', 'capture'):
        if ROLL_MODE == 'capture' and len(x) >= 300:   # existed since 2021-22 -> games 83-164 ~ 2022-23
            _ROLL_CAPTURE.setdefault(col, []).append(x.values[82:164].astype(float))   # (2021-22 EDGE is zero-filled)
        return x.shift(1).rolling(n, min_periods=min_periods).mean()
    from scipy.signal import lfilter
    H, k = _ROLL_PARAMS[_ROLL_CLASS.get(col, 'rate')]
    if n <= 3:
        H = 5                                   # short-memory 'trend' variant
    elif n >= 20:
        H = 2 * H
    mu = _KNOWN_PRIORS.get(col, _ROLL_PRIORS.get(col))
    v = x.shift(1).astype(float)
    ok = v.notna().values.astype(float)
    d = 0.5 ** (1.0 / H)
    S = lfilter([1.0], [1.0, -d], np.nan_to_num(v.values))     # decayed sum of prior games
    W = lfilter([1.0], [1.0, -d], ok)                           # decayed count
    if mu is None or not np.isfinite(mu):                       # no prior known: plain decayed mean
        est = np.where(W > 0, S / np.maximum(W, 1e-12), np.nan)
    else:
        est = np.where(W > 0, (S + k * mu) / (W + k), np.nan)  # no history -> NaN, as the window version
    return pd.Series(est, index=x.index)

def roll_ratio(df, num_col, den_col, n, col, team_col='team_id'):
    """Pre-game RATIO OF SUMS over a team's previous games (2026-10-02 fix for mean-of-ratios blow-ups, e.g. a
    5-second power play with a goal scoring 3.5 goals/min and dominating an 8-game average).
    window mode: sum(num)/sum(den) over the previous n games; shrink mode: exponentially decayed sums.
    Both add pseudo-games of league-average denominator at the PREVIOUS season's league ratio (leak-free),
    so a game counts in proportion to its denominator and early windows can't explode.
    df must be sorted by (team, game order). Returns a Series aligned to df."""
    from scipy.signal import lfilter
    yr = df['game_id'].astype(str).str[:4].astype(int)
    tot = df.groupby(yr)[[num_col, den_col]].sum()
    lr = tot[num_col] / tot[den_col].replace(0, np.nan)
    mu_y = lr.shift(1).fillna(lr)                                   # previous season's league ratio
    dbar_all = df.groupby(yr)[den_col].mean()
    dbar_y = dbar_all.shift(1).fillna(dbar_all)                     # previous season's mean denominator per game
    mu = yr.map(mu_y).astype(float).values
    dbar = yr.map(dbar_y).astype(float).values
    g = df.groupby(team_col, sort=False)
    if ROLL_MODE == 'shrink':
        H, k = _ROLL_PARAMS[_ROLL_CLASS.get(col, 'rate')]
        H = 5 if n <= 3 else (2 * H if n >= 20 else H)
        d = 0.5 ** (1.0 / H)
        dec = lambda x: pd.Series(lfilter([0, d], [1, -d], x.astype(float).fillna(0).values), index=x.index)
        S_n = g[num_col].transform(dec).values
        S_d = g[den_col].transform(dec).values
    else:
        k = 1.0
        S_n = g[num_col].transform(lambda x: x.shift(1).rolling(n, min_periods=1).sum()).fillna(0).values
        S_d = g[den_col].transform(lambda x: x.shift(1).rolling(n, min_periods=1).sum()).fillna(0).values
    est = (S_n + k * dbar * mu) / np.maximum(S_d + k * dbar, 1e-12)
    return pd.Series(est, index=df.index)

if ROLL_MODE == 'shrink':
    import json as _json
    _pp = os.environ.get('NHL_ROLL_PRIORS')
    if not _pp or not os.path.exists(_pp):
        raise SystemExit("NHL_ROLL_MODE=shrink needs NHL_ROLL_PRIORS=<priors json from a capture run>")
    _ROLL_PRIORS = _json.load(open(_pp))

DEFAULT_EPOCHS = 1000
DEFAULT_BATCH = 64
DEFAULT_LR = 0.005
DEFAULT_HIDDEN = 192
DEFAULT_N_SIMS = 5000
# the number of hidden neurons needs to be greater than the number of features, otherwise it has to compress / bottleneck them.
# but increasing it requires more training data to effectively fill the parameters.

# Historical values: 3.0, 2.17
EMPTY_NET_MULTIPLIER_FOR = 3.0
EMPTY_NET_MULTIPLIER_AGAINST = 2.17

# --- Simulation realism constants (calibrated to nhl_analytics.db, 2021-2026) ---
# Home/away goals are NOT independent: empirical within-game count correlation is
# ≈ -0.118 (score effects — the trailing team pushes, the leading team sits back).
# We inject this via a Gaussian copula on regulation scores; the Gaussian rho is
# calibrated through the FULL sim (the OT/SO +1-to-one-side mechanic adds its own
# negative correlation on top of the copula) so the resulting final-score COUNT
# correlation lands on the -0.118 target. rho_gauss=-0.088 -> final corr ≈ -0.118.
# NOTE: a linear copula matches the correlation but cannot reproduce the empirical
# *excess* of one-goal/tied games (~22% reach OT vs ~18% under independence) — that
# underdispersion is a score-effect phenomenon and is left for a future score-effect
# refinement of the regulation simulation.
HOME_AWAY_RHO_GAUSS = -0.088
# Overtime/shootout: every NHL game has a winner, so tied regulation games must be
# resolved. Empirically home teams win ~53% of games that reach OT (54% of
# OT-decided, ~even in the shootout), tilted by relative team strength (lh-la).
OT_HOME_WIN_BASE = 0.53
OT_STRENGTH_TILT = 0.10

# ---------------------------
# Global Helpers
# ---------------------------
def norm(s):
    return s.replace('.', '').upper()

# ---------------------------
# Situation Helper
# ---------------------------
_situation_id_cache = {}

def get_situation_id(con):
    try:
        db_path = con.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        db_path = str(id(con))
    if db_path not in _situation_id_cache:
        res = pd.read_sql_query("SELECT situation_id FROM situations WHERE LOWER(situation_code) LIKE '%all%' LIMIT 1", con)
        _situation_id_cache[db_path] = int(res.iloc[0]['situation_id']) if not res.empty else 2
    return _situation_id_cache[db_path]


# ---------------------------
# Sketchy at best
# ---------------------------
def process_special_teams(con) -> pd.DataFrame:
    query = """
    SELECT game_id, team_id, situation_code,
           SUM(COALESCE(mp_score_venue_adjusted_xgoals_for, mp_xgoals_for, 0)) as xg_for,
           SUM(COALESCE(mp_score_venue_adjusted_xgoals_against, mp_xgoals_against, 0)) as xg_against,
           SUM(COALESCE(mp_goals_for, 0)) as goals_for,
           SUM(COALESCE(mp_goals_against, 0)) as goals_against,
           SUM(mp_ice_time) as toi
    FROM mp_team_game_stats mp
    JOIN situations s ON mp.situation_id = s.situation_id
    WHERE s.situation_code IN ('PP', 'PK')
    GROUP BY game_id, team_id, situation_code
    """
    df = pd.read_sql_query(query, con)
    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_pp_xg60', 'roll_pk_xga60', 'roll_pk_xgf60', 'roll_pp_efficiency', 'roll_pk_g60'])

    pp = df[df['situation_code'] == 'PP'].copy()
    pk = df[df['situation_code'] == 'PK'].copy()

    # Rates per PP/PK MINUTE as a ratio of sums over the window (2026-10-02). The old per-game
    # goals/(minutes+0.2) then averaged let a 5-second PP with a goal score 3.5/min and dominate the window.
    pp['pp_min'] = pp['toi'] / 60.0
    pk['pk_min'] = pk['toi'] / 60.0
    pp = pp.sort_values(['team_id', 'game_id']).reset_index(drop=True)
    pk = pk.sort_values(['team_id', 'game_id']).reset_index(drop=True)

    pp['roll_pp_xg60'] = roll_ratio(pp, 'xg_for', 'pp_min', 8, 'pp_xg60')
    pp['roll_pp_efficiency'] = roll_ratio(pp, 'goals_for', 'pp_min', 8, 'pp_efficiency')
    pk['roll_pk_xga60'] = roll_ratio(pk, 'xg_against', 'pk_min', 8, 'pk_xga60')
    pk['roll_pk_xgf60'] = roll_ratio(pk, 'xg_for', 'pk_min', 8, 'pk_xgf60')
    pk['roll_pk_g60'] = roll_ratio(pk, 'goals_against', 'pk_min', 8, 'pk_g60')

    # Merge (a team-game missing one side gets that column's median, never 0)
    out = pd.merge(pp[['game_id', 'team_id', 'roll_pp_xg60', 'roll_pp_efficiency']],
                   pk[['game_id', 'team_id', 'roll_pk_xga60', 'roll_pk_xgf60', 'roll_pk_g60']],
                   on=['game_id', 'team_id'], how='outer')
    for c in ['roll_pp_xg60', 'roll_pp_efficiency', 'roll_pk_xga60', 'roll_pk_xgf60', 'roll_pk_g60']:
        out[c] = out[c].fillna(out[c].median())
                   
    # Fill defaults if missing (league avg approx)
    if out['roll_pp_xg60'].mean() == 0: out['roll_pp_xg60'] = 7.0
    if out['roll_pp_efficiency'].mean() == 0: out['roll_pp_efficiency'] = 7.0 # Approx 7 goals/60 on PP
    if out['roll_pk_g60'].mean() == 0: out['roll_pk_g60'] = 7.0
    
    return out

def identify_starting_goalie(con) -> pd.DataFrame:
    """
    Identifies the starting goalie for each game.
    Logic: Goalie with most TOI (time on ice) is the starter.
    Threshold: toi_seconds > 1800 (30+ minutes) ensures they played majority of game.

    Returns: DataFrame with columns [game_id, team_id, player_id, toi_seconds]
    """
    all_id = get_situation_id(con)

    # 1. Try NST first (preferred)
    nst_query = f"""
    SELECT
        game_id,
        team_id,
        player_id,
        toi_seconds,
        ROW_NUMBER() OVER (
            PARTITION BY game_id, team_id
            ORDER BY toi_seconds DESC
        ) as goalie_rank
    FROM goalie_game_stats
    WHERE situation_id = {all_id}
      AND toi_seconds > 60
    """
    
    try:
        nst_df = pd.read_sql_query(nst_query, con)
        # Clean player_id: remove " [G]" or similar
        if not nst_df.empty:
             nst_df['player_id'] = nst_df['player_id'].astype(str).str.replace(r' \[.*\]', '', regex=True).str.strip()
    except Exception:
        nst_df = pd.DataFrame()

    # 2. Try MoneyPuck as fallback
    mp_query = f"""
    SELECT
        game_id,
        team_id,
        player_id,
        mp_ice_time as toi_seconds,
        ROW_NUMBER() OVER (
            PARTITION BY game_id, team_id
            ORDER BY mp_ice_time DESC
        ) as goalie_rank
    FROM mp_goalie_game_stats
    WHERE situation_id = {all_id}
    """
    
    try:
        mp_df = pd.read_sql_query(mp_query, con)
    except Exception:
        mp_df = pd.DataFrame()

    # Combine: Use NST, fill missing games with MP
    if not nst_df.empty:
        df = nst_df
        if not mp_df.empty:
            # Find games in MP that are NOT in NST
            missing_games = set(mp_df['game_id']) - set(nst_df['game_id'])
            if missing_games:
                df = pd.concat([df, mp_df[mp_df['game_id'].isin(missing_games)]])
    elif not mp_df.empty:
        df = mp_df
    else:
        return pd.DataFrame(columns=['game_id', 'team_id', 'player_id', 'toi_seconds'])

    # Mark starter (goalie_rank = 1)
    df['is_starter'] = (df['goalie_rank'] == 1).astype(int)

    # Keep only starters
    starters = df[df['is_starter'] == 1][['game_id', 'team_id', 'player_id', 'toi_seconds']]
    return starters


# Use NST goalie data (goalie_game_stats) with MoneyPuck fallback
# NST has better coverage overall, but MP fills gaps for some games
def process_goalie_metrics(con) -> pd.DataFrame:
    """
    Calculate per-goalie advanced metrics.
    Returns goalie-level dataframe (NOT aggregated to team level).

    Returns: DataFrame with columns:
        - game_id, team_id, player_id
        - roll_gsax, roll_hd_gsax, roll_rcr, roll_fatigue_index
        - ghsf (deprecated, keep for backward compatibility)
    """
    all_id = get_situation_id(con)

    # 1. Get NST goalie data
    nst_query = f"""
    SELECT game_id, team_id, player_id,
           COALESCE(expected_goals_against, 0) as xga,
           COALESCE(goals_against, 0) as ga,
           COALESCE(hd_goals_against, 0) as hd_ga,
           COALESCE(ld_goals_against, 0) as ld_ga,
           shots_against,
           saves,
           toi_seconds
    FROM goalie_game_stats
    WHERE situation_id = {all_id}
    """
    nst_df = pd.read_sql_query(nst_query, con)
    if not nst_df.empty:
         nst_df['player_id'] = nst_df['player_id'].astype(str).str.replace(r' \[.*\]', '', regex=True).str.strip()

    # 2. Get MP goalie data (for RCR and HD_GSAx)
    mp_query = f"""
    SELECT game_id, team_id, player_id,
           COALESCE(mp_xgoals_against, 0) as mp_xga,
           COALESCE(mp_goals_against, 0) as mp_ga,
           COALESCE(mp_high_danger_xgoals_against, 0) as mp_hd_xga,
           COALESCE(mp_high_danger_goals_against, 0) as mp_hd_ga,
           COALESCE(mp_rebounds_against, 0) as mp_rebounds,
           COALESCE(mp_saves, 0) as mp_saves,
           COALESCE(mp_high_danger_shots_against, 0) as mp_hd_shots
    FROM mp_goalie_game_stats
    WHERE situation_id = {all_id}
    """
    mp_df = pd.read_sql_query(mp_query, con)
    if not mp_df.empty:
         mp_df['player_id'] = mp_df['player_id'].astype(str).str.strip()

    # 3. Merge NST + MP data
    if not nst_df.empty and not mp_df.empty:
        df = pd.merge(nst_df, mp_df, on=['game_id', 'team_id', 'player_id'], how='outer')
    elif not nst_df.empty:
        df = nst_df
        # Add missing MP cols
        for c in ['mp_xga', 'mp_ga', 'mp_hd_xga', 'mp_hd_ga', 'mp_rebounds', 'mp_saves', 'mp_hd_shots']:
            df[c] = 0
    elif not mp_df.empty:
        df = mp_df
        # Add missing NST cols
        for c in ['xga', 'ga', 'hd_ga', 'ld_ga', 'shots_against', 'saves', 'toi_seconds']:
            df[c] = 0
    else:
        return pd.DataFrame(columns=['game_id', 'team_id', 'player_id',
                                     'roll_gsax', 'roll_hd_gsax', 'roll_rcr',
                                     'roll_fatigue_index', 'ghsf'])

    df = df.fillna(0)

    # 4. Calculate per-game metrics
    # Use NST GSAx if available, else MP
    df['gsax'] = np.where(df['xga'] != 0, df['xga'] - df['ga'], df['mp_xga'] - df['mp_ga'])
    
    # PRIORITY 1 FEATURE: High Danger GSAx
    df['hd_gsax'] = df['mp_hd_xga'] - df['mp_hd_ga']
    
    # PRIORITY 1 FEATURE: Rebound Control Rating (RCR)
    # 1 - (Rebounds / Saves). careful of div by zero
    df['rcr'] = 1.0 - (df['mp_rebounds'] / (df['mp_saves'] + 0.1))
    
    # PRIORITY 2 FEATURE: Workload Fatigue Index
    # Weighted workload: HD shots count 1.5x (conservative start)
    # Use NST shots_against if available, else MP saves + goals
    df['raw_shots'] = np.where(df['shots_against'] > 0, df['shots_against'], df['mp_saves'] + df['mp_ga'])
    df['weighted_workload'] = df['raw_shots'] + (df['mp_hd_shots'] * 0.5) # +0.5 because it's already in raw_shots

    # 5. Sort by player and game for rolling calculations
    df = df.sort_values(['player_id', 'game_id'])
    grp_goalie = df.groupby('player_id')

    # 6. Goalie form, SHRUNK BY SAMPLE SIZE (2026-10-02). Last 10 games are pulled toward the goalie's career
    # level (worth K_WIN games), and the career level toward the average NEW goalie (worth K_CAREER games).
    # A 2-game call-up therefore sits near new-goalie level instead of looking elite off one hot night.
    n_prior = grp_goalie.cumcount()                          # this goalie's earlier games in the DB
    def _career_sum(c):
        return grp_goalie[c].cumsum() - df[c]
    def _window_sum(c, w=10):
        return grp_goalie[c].transform(lambda x: x.shift(1).rolling(w, min_periods=1).sum()).fillna(0.0)
    n_win = grp_goalie['gsax'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).count()).fillna(0.0)
    K_CAREER, K_WIN = 20.0, 5.0
    new_goalie = n_prior < 20
    # League constants come from the PREVIOUS season (first season: its own), so no row sees its own game or later.
    yr = df['game_id'].astype(str).str[:4].astype(int)
    def _prev_season(by_year):
        # every season present in df gets the latest EARLIER season's value (a season can be absent from by_year,
        # e.g. no new goalie has played yet this October); the first season falls back to its own value
        by_year = by_year.reindex(sorted(yr.unique()))
        prev = by_year.ffill().shift(1)
        return yr.map(prev.fillna(by_year).bfill()).astype(float)
    for c in ('gsax', 'hd_gsax'):
        mu_new = _prev_season(df[new_goalie].groupby(yr[new_goalie])[c].mean())   # avg goalie in his first 20 games
        career = (_career_sum(c) + K_CAREER * mu_new) / (n_prior + K_CAREER)
        df[f'roll_{c}'] = (_window_sum(c) + K_WIN * career) / (n_win + K_WIN)
    # rebound control = 1 - rebounds per save, as a ratio of sums with the same two-level shrink
    sv_bar = _prev_season(df.groupby(yr)['mp_saves'].mean())
    _tot = df.groupby(yr)[['mp_rebounds', 'mp_saves']].sum()
    mu_rate = _prev_season(_tot['mp_rebounds'] / _tot['mp_saves'].clip(lower=1.0))
    career_rate = (_career_sum('mp_rebounds') + K_CAREER * sv_bar * mu_rate) / (_career_sum('mp_saves') + K_CAREER * sv_bar)
    win_rate = (_window_sum('mp_rebounds') + K_WIN * sv_bar * career_rate) / (_window_sum('mp_saves') + K_WIN * sv_bar)
    df['roll_rcr'] = 1.0 - win_rate
    df['roll_fatigue_index'] = grp_goalie['weighted_workload'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).sum()
    )

    # 7. DEPRECATED: GHSF (kept for backward compatibility during transition)
    df['gsax_recent3'] = grp_goalie['gsax'].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    df['gsax_prior3'] = grp_goalie['gsax'].transform(lambda x: x.shift(4).rolling(3, min_periods=1).mean())
    df['gsax_trend'] = df['gsax_recent3'] - df['gsax_prior3']
    df['gsax_volatility'] = grp_goalie['gsax'].transform(lambda x: x.shift(1).rolling(5, min_periods=2).std())
    df['ghsf'] = df['gsax_trend'] / (df['gsax_volatility'] + 0.1)
    df['ghsf'] = df['ghsf'].fillna(0.0).clip(-5, 5)

    # 8. Fill NaNs with league averages or zeros
    # Check if games_played is small
    df['games_played_cum'] = grp_goalie.cumcount() + 1
    
    # Defaults. gsax / hd_gsax / rcr are already sample-size shrunk above (no NaNs, no first-games override);
    # fatigue keeps its league-average default for a goalie's first games.
    LEAGUE_AVG_RCR = 1.0 - mu_rate          # per-row previous-season value
    LEAGUE_AVG_FATIGUE = 150.0

    df['roll_gsax'] = df['roll_gsax'].fillna(0.0)
    df['roll_hd_gsax'] = df['roll_hd_gsax'].fillna(0.0)
    df['roll_rcr'] = df['roll_rcr'].fillna(LEAGUE_AVG_RCR)
    df['roll_fatigue_index'] = df['roll_fatigue_index'].fillna(LEAGUE_AVG_FATIGUE)

    mask_new = df['games_played_cum'] < 5
    df.loc[mask_new, 'roll_fatigue_index'] = LEAGUE_AVG_FATIGUE

    # 9. Return PER-GOALIE features (DO NOT AGGREGATE TO TEAM LEVEL)
    return df[['game_id', 'team_id', 'player_id',
               'roll_gsax', 'roll_hd_gsax', 'roll_rcr', 'roll_fatigue_index', 'ghsf']]



# NST data from team_game_overview - aggregate by game first, then expand to both teams
def process_nst_metrics(con) -> pd.DataFrame:
    all_id = get_situation_id(con)

    # Get aggregate NST data per game (one team's perspective per row)
    query = f"""
    SELECT game_id, team_id, COALESCE(hdcf,0) as hdcf, COALESCE(hdca,0) as hdca
    FROM team_game_overview WHERE period = 0 AND situation_id = {all_id}
    """
    df = pd.read_sql_query(query, con)
    if df.empty:
        df = pd.DataFrame(columns=['game_id', 'team_id', 'roll_hdcf_share', 'roll3_hdcf_share', 'hdsm'])
        df['roll_hdcf_share'] = 0.5
        df['roll3_hdcf_share'] = 0.5
        df['hdsm'] = 0.0
        return df

    # Calculate hdcf_share for each team
    df['hdcf_share'] = df['hdcf'] / (df['hdcf'] + df['hdca'] + 0.1)

    # Sort and calculate rolling share as a RATIO OF SUMS (2026-10-02)
    df = df.sort_values(['team_id', 'game_id']).reset_index(drop=True)
    df['_hd_tot'] = df['hdcf'] + df['hdca']

    # Standard 10-game rolling
    df['roll_hdcf_share'] = roll_ratio(df, 'hdcf', '_hd_tot', 10, 'hdcf_share')

    # HDSM (High-Danger Shot Momentum): 3-game vs 10-game differential
    df['roll3_hdcf_share'] = roll_ratio(df, 'hdcf', '_hd_tot', 3, 'hdcf_share')
    df['hdsm'] = df['roll3_hdcf_share'] - df['roll_hdcf_share']

    # Fill NaNs with league average
    league_avg = df['hdcf_share'].mean()
    df['roll_hdcf_share'] = df['roll_hdcf_share'].fillna(league_avg if not np.isnan(league_avg) else 0.5)
    df['roll3_hdcf_share'] = df['roll3_hdcf_share'].fillna(league_avg if not np.isnan(league_avg) else 0.5)
    df['hdsm'] = df['hdsm'].fillna(0.0)

    # Return relevant columns directly
    # The main data pipeline matches these to games based on (game_id, team_id)
    return df[['game_id', 'team_id', 'roll_hdcf_share', 'roll3_hdcf_share', 'hdsm']]


# NEW: Advanced MoneyPuck features for better predictions
def process_advanced_metrics(con) -> pd.DataFrame:
    all_id = get_situation_id(con)
    query = f"""
    SELECT game_id, team_id,
           COALESCE(mp_high_danger_shots_for, 0) as hd_shots_for,
           COALESCE(mp_shots_on_goal_for, 0) as sog_for,
           COALESCE(mp_goals_for, 0) as gf,
           COALESCE(mp_rebound_xgoals_for, 0) as rebound_xgf,
           COALESCE(mp_faceoffs_won_for, 0) as fo_won,
           COALESCE(mp_faceoffs_won_against, 0) as fo_against,
           COALESCE(mp_score_adjusted_shots_attempts_for, mp_shot_attempts_for, 0) as sa_corsi_for,
           COALESCE(mp_score_adjusted_shots_attempts_against, mp_shot_attempts_against, 0) as sa_corsi_against,
           COALESCE(mp_hits_for, 0) as hits_for,
           COALESCE(mp_freeze_against, 0) as freeze_ag,
           COALESCE(mp_rebounds_for, 0) as rebounds_for,
           COALESCE(mp_penalties_for, 0) as pen_for,
           COALESCE(mp_penalties_against, 0) as pen_ag,
           COALESCE(mp_flurry_adjusted_xgoals_for, 0) as flurry_xgf,
           COALESCE(mp_flurry_adjusted_xgoals_against, 0) as flurry_xga,
           COALESCE(mp_high_danger_goals_for, 0) as hd_goals_for,
           COALESCE(mp_high_danger_shots_against, 0) as hd_shots_against,
           COALESCE(mp_high_danger_goals_against, 0) as hd_goals_against,
           COALESCE(mp_medium_danger_shots_for, 0) as md_shots_for,
           COALESCE(mp_medium_danger_goals_for, 0) as md_goals_for,
           COALESCE(mp_medium_danger_shots_against, 0) as md_shots_against,
           COALESCE(mp_medium_danger_goals_against, 0) as md_goals_against,
           COALESCE(mp_blocked_shot_attempts_for, 0) as blocks_for,
           COALESCE(mp_shot_attempts_against, 0) as corsi_against_raw,
           
           -- New Pressure Metrics
           COALESCE(mp_play_continued_in_zone_for, 0) as play_cont_zone,
           COALESCE(mp_play_continued_outside_zone_for, 0) as play_cont_out,
           COALESCE(mp_play_continued_in_zone_against, 0) as play_cont_zone_ag,
           COALESCE(mp_shot_attempts_for, 0) as raw_attempts_for
    FROM mp_team_game_stats
    WHERE situation_id = {all_id}
    """
    df = pd.read_sql_query(query, con)
    
    new_features = ['roll_hd_shot_pct', 'roll_sh_pct', 'roll_rebound_xgf', 'roll_fo_pct', 'roll_sa_corsi_pct',
                    'roll_freeze_ag', 'roll_pen_diff',
                    'roll_flurry_delta', 'roll_hd_finish_pct', 'roll_hd_save_pct', 
                    'roll_md_finish_pct', 'roll_md_save_pct', 'roll_block_rate',
                    'roll_pressure_rate', 'roll_dzone_clearance_rate']

    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id'] + new_features)

    # Calculate per-game metrics
    df['hd_shot_pct'] = df['hd_shots_for'] / (df['sog_for'] + 0.1)
    df['sh_pct'] = df['gf'] / (df['sog_for'] + 0.1)
    df['fo_pct'] = df['fo_won'] / (df['fo_won'] + df['fo_against'] + 0.1)
    df['sa_corsi_pct'] = df['sa_corsi_for'] / (df['sa_corsi_for'] + df['sa_corsi_against'] + 0.1)
    df['pen_diff'] = df['pen_ag'] - df['pen_for']
    
    # New calculated metrics
    df['flurry_delta'] = df['flurry_xgf'] - df['flurry_xga']
    df['hd_finish_pct'] = df['hd_goals_for'] / (df['hd_shots_for'] + 0.1)
    df['hd_save_pct'] = 1.0 - (df['hd_goals_against'] / (df['hd_shots_against'] + 0.1))
    df['md_finish_pct'] = df['md_goals_for'] / (df['md_shots_for'] + 0.1)
    df['md_save_pct'] = 1.0 - (df['md_goals_against'] / (df['md_shots_against'] + 0.1))
    df['block_rate'] = df['blocks_for'] / (df['corsi_against_raw'] + 0.1)

    # Pressure Metrics Calculations
    # Pressure Rate: % of attempts that result in sustained pressure
    df['pressure_rate'] = df['play_cont_zone'] / (df['raw_attempts_for'] + 0.1)
    
    # D-Zone Clearance Rate: % of events where we clear the zone vs getting hemmed in
    # Denominator: Successful Clears + Failed Clears (Sustained Pressure Against)
    df['dzone_clearance_rate'] = df['play_cont_out'] / (df['play_cont_out'] + df['play_cont_zone_ag'] + 0.1)

    df = df.sort_values(['team_id', 'game_id']).reset_index(drop=True)
    grp = df.groupby('team_id')

    # Numerators / denominators for the ratio features (2026-10-02: rolled as RATIO OF SUMS via roll_ratio;
    # the per-game ratio columns above are kept only for the league-average fills further down)
    df['_fo_tot'] = df['fo_won'] + df['fo_against']
    df['_sac_tot'] = df['sa_corsi_for'] + df['sa_corsi_against']
    df['_hd_saves'] = df['hd_shots_against'] - df['hd_goals_against']
    df['_md_saves'] = df['md_shots_against'] - df['md_goals_against']
    df['_clear_tot'] = df['play_cont_out'] + df['play_cont_zone_ag']

    # Rolling averages
    df['roll_hd_shot_pct'] = roll_ratio(df, 'hd_shots_for', 'sog_for', 10, 'hd_shot_pct')
    df['roll_sh_pct'] = roll_ratio(df, 'gf', 'sog_for', 10, 'sh_pct')
    df['roll_rebound_xgf'] = grp['rebound_xgf'].transform(lambda x: roll_mean(x, 10, 1, 'rebound_xgf'))
    df['roll_fo_pct'] = roll_ratio(df, 'fo_won', '_fo_tot', 10, 'fo_pct')
    df['roll_sa_corsi_pct'] = roll_ratio(df, 'sa_corsi_for', '_sac_tot', 10, 'sa_corsi_pct')
    
    # New features rolling
    df['roll_freeze_ag'] = grp['freeze_ag'].transform(lambda x: roll_mean(x, 10, 1, 'freeze_ag'))
    df['roll_pen_diff'] = grp['pen_diff'].transform(lambda x: roll_mean(x, 10, 1, 'pen_diff'))
    
    # Added advanced features rolling
    df['roll_flurry_delta'] = grp['flurry_delta'].transform(lambda x: roll_mean(x, 10, 1, 'flurry_delta'))
    df['roll_hd_finish_pct'] = roll_ratio(df, 'hd_goals_for', 'hd_shots_for', 10, 'hd_finish_pct')
    df['roll_hd_save_pct'] = roll_ratio(df, '_hd_saves', 'hd_shots_against', 10, 'hd_save_pct')
    df['roll_md_finish_pct'] = roll_ratio(df, 'md_goals_for', 'md_shots_for', 10, 'md_finish_pct')
    df['roll_md_save_pct'] = roll_ratio(df, '_md_saves', 'md_shots_against', 10, 'md_save_pct')
    df['roll_block_rate'] = roll_ratio(df, 'blocks_for', 'corsi_against_raw', 10, 'block_rate')
    
    # Pressure Rolling
    df['roll_pressure_rate'] = roll_ratio(df, 'play_cont_zone', 'raw_attempts_for', 10, 'pressure_rate')
    df['roll_dzone_clearance_rate'] = roll_ratio(df, 'play_cont_out', '_clear_tot', 10, 'dzone_clearance_rate')

    # --- Trend Features (Short & Long Term) ---
    # Short term (Last 3) - Hot/Cold streaks
    df['roll3_sh_pct'] = roll_ratio(df, 'gf', 'sog_for', 3, 'sh_pct')
    df['roll3_sa_corsi_pct'] = roll_ratio(df, 'sa_corsi_for', '_sac_tot', 3, 'sa_corsi_pct')
    df['roll3_hd_save_pct'] = roll_ratio(df, '_hd_saves', 'hd_shots_against', 3, 'hd_save_pct')
    
    # Long term (Last 20) - Structural strength
    df['roll20_sh_pct'] = roll_ratio(df, 'gf', 'sog_for', 20, 'sh_pct')
    df['roll20_sa_corsi_pct'] = roll_ratio(df, 'sa_corsi_for', '_sac_tot', 20, 'sa_corsi_pct')
    df['roll20_hd_save_pct'] = roll_ratio(df, '_hd_saves', 'hd_shots_against', 20, 'hd_save_pct')

    # Update new_features list to include these
    trend_features = [
        'roll3_sh_pct', 'roll3_sa_corsi_pct', 'roll3_hd_save_pct',
        'roll20_sh_pct', 'roll20_sa_corsi_pct', 'roll20_hd_save_pct'
    ]
    
    final_cols = new_features + trend_features

    # Fill with league averages
    for col in final_cols:
        # For calculated percentages, the base col is just the name without 'roll_'
        # For raw counts (hits, freeze, rebounds, pen_diff), the base col matches the column created above
        base_col_map = {
            'roll_hd_shot_pct': 'hd_shot_pct',
            'roll_sh_pct': 'sh_pct',
            'roll_rebound_xgf': 'rebound_xgf',
            'roll_fo_pct': 'fo_pct',
            'roll_sa_corsi_pct': 'sa_corsi_pct',
            'roll_freeze_ag': 'freeze_ag',
            'roll_pen_diff': 'pen_diff',
            'roll_flurry_delta': 'flurry_delta',
            'roll_hd_finish_pct': 'hd_finish_pct',
            'roll_hd_save_pct': 'hd_save_pct',
            'roll_md_finish_pct': 'md_finish_pct',
            'roll_md_save_pct': 'md_save_pct',
            'roll_block_rate': 'block_rate',
            'roll_pressure_rate': 'pressure_rate',
            'roll_dzone_clearance_rate': 'dzone_clearance_rate'
        }
        
        base_col = base_col_map.get(col, col.replace('roll_', '').replace('roll3_', '').replace('roll20_', ''))
        if base_col in df.columns:
            league_avg = df[base_col].mean()
            df[col] = df[col].fillna(league_avg if not np.isnan(league_avg) else 0.0)
        else:
            df[col] = df[col].fillna(0.0)

    # After all calculations and fills, for debugging team 28
    return df[['game_id', 'team_id'] + final_cols]


def get_team_map(con):
    """
    Returns a dictionary mapping various team codes (L.A, LAK, etc.) to internal team_id.
    """
    teams_df = pd.read_sql_query("SELECT team_id, team_abbr FROM teams", con)
    mapping = {abbr.upper(): tid for tid, abbr in zip(teams_df['team_id'], teams_df['team_abbr'])}
    
    # Add MoneyPuck/NHL specific overrides
    overrides = {
        'L.A': mapping.get('LA'), 'LAK': mapping.get('LA'),
        'N.J': mapping.get('NJ'), 'NJD': mapping.get('NJ'),
        'S.J': mapping.get('SJ'), 'SJS': mapping.get('SJ'),
        'T.B': mapping.get('TB'), 'TBL': mapping.get('TB'),
        'UTA': mapping.get('UTA'), 'ARI': mapping.get('ARI'), # Utah/Arizona
        'VGK': mapping.get('VGK'), 'SEA': mapping.get('SEA')
    }
    # Update mapping with overrides (filtering out Nones)
    for k, v in overrides.items():
        if v is not None:
            mapping[k] = v
            
    return mapping

def process_shot_metrics(con) -> pd.DataFrame:
    # Aggregate directly in SQL to avoid loading all individual shot rows into memory.
    # rush_attempts replaced by process_rush_metrics; shot_rush/goal/shot_was_on_goal dropped.
    shots_query = """
    SELECT
        game_id,
        team_code,
        COUNT(*) as shots_total,
        AVG(shot_distance) as avg_dist,
        AVG(ABS(shot_angle)) as avg_angle,
        SUM(CASE WHEN shot_type = 'WRIST' THEN 1 ELSE 0 END) as cnt_wrist,
        SUM(CASE WHEN shot_type = 'SLAP'  THEN 1 ELSE 0 END) as cnt_slap,
        SUM(CASE WHEN shot_type = 'SNAP'  THEN 1 ELSE 0 END) as cnt_snap,
        SUM(CASE WHEN shot_type = 'BACK'  THEN 1 ELSE 0 END) as cnt_backhand,
        SUM(CASE WHEN shot_type = 'TIP'   THEN 1 ELSE 0 END) as cnt_tip,
        SUM(shot_generated_rebound) as cnt_rebound,
        SUM(off_wing) as cnt_off_wing
    FROM mp_shots
    WHERE period <= 4
    GROUP BY game_id, team_code
    """
    df_agg = pd.read_sql_query(shots_query, con)

    if df_agg.empty:
        return pd.DataFrame(columns=['game_id', 'team_id'])

    # Map team codes to internal team IDs
    team_map = get_team_map(con)
    df_agg['team_code_clean'] = df_agg['team_code'].astype(str).str.upper().str.strip()
    df_agg['team_id'] = df_agg['team_code_clean'].map(team_map)
    df_agg = df_agg.dropna(subset=['team_id'])
    df_agg['team_id'] = df_agg['team_id'].astype(int)
    df_agg = df_agg.drop(columns=['team_code', 'team_code_clean'])

    # We do NOT return rolling averages here. The central manager handles rolling.
    return df_agg


def process_skater_aggregates(con) -> pd.DataFrame:
    """
    Aggregates skater stats to team level (e.g. Top 3 F xG).
    """
    # Use All Situations
    all_id = get_situation_id(con)
    
    query = f"""
    SELECT game_id, team_id, player_id, mp_position, mp_i_f_xgoals, mp_i_f_goals
    FROM mp_skater_game_stats
    WHERE situation_id = {all_id}
    """
    df = pd.read_sql_query(query, con)
    
    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id'])
        
    # Standardize Positions
    # MP positions: 'C', 'L', 'R', 'D'
    df['pos_group'] = df['mp_position'].map({'C': 'F', 'L': 'F', 'R': 'F', 'D': 'D'}).fillna('F')
    
    # Sort by xG descending per game/team/pos
    df = df.sort_values(['game_id', 'team_id', 'pos_group', 'mp_i_f_xgoals'], ascending=[True, True, True, False])
    
    # Group by game/team/pos to pick top N
    # This is slightly complex in pandas without rank, but let's try rank
    df['rank'] = df.groupby(['game_id', 'team_id', 'pos_group']).cumcount() + 1
    
    # Define aggregations
    # Top 3 F
    top3f = df[(df['pos_group'] == 'F') & (df['rank'] <= 3)].groupby(['game_id', 'team_id'])['mp_i_f_xgoals'].sum().reset_index(name='top3_f_xg')
    
    # Top 2 D
    top2d = df[(df['pos_group'] == 'D') & (df['rank'] <= 2)].groupby(['game_id', 'team_id'])['mp_i_f_xgoals'].sum().reset_index(name='top2_d_xg')
    
    # Bottom 6 F (Rank 7-12)
    bot6f = df[(df['pos_group'] == 'F') & (df['rank'] >= 7) & (df['rank'] <= 12)].groupby(['game_id', 'team_id'])['mp_i_f_xgoals'].sum().reset_index(name='bot6_f_xg')
    
    # Finishing: Top 3 F Goals - xG
    top3f_finish = df[(df['pos_group'] == 'F') & (df['rank'] <= 3)].groupby(['game_id', 'team_id']).apply(
        lambda x: (x['mp_i_f_goals'] - x['mp_i_f_xgoals']).sum(), include_groups=False
    ).reset_index(name='top3_f_finish')
    
    # Merge all
    out = top3f
    out = pd.merge(out, top2d, on=['game_id', 'team_id'], how='outer')
    out = pd.merge(out, bot6f, on=['game_id', 'team_id'], how='outer')
    out = pd.merge(out, top3f_finish, on=['game_id', 'team_id'], how='outer')
    
    return out.fillna(0)


def process_skater_chemistry(con) -> pd.DataFrame:
    """
    Calculates advanced skater chemistry metrics:
    1. Top Linemate xGF Boost (Forwards)
    2. Top Linemate HDCF Synergy (Forwards)
    3. Top D-Pair xGF Boost (Defense)
    """
    # Use 5v5 for stable chemistry analysis
    query_sit = "SELECT situation_id FROM situations WHERE situation_code = '5v5' LIMIT 1"
    res = pd.read_sql_query(query_sit, con)
    if res.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_linemate_xgf_boost', 'roll_linemate_hdcf_synergy', 'roll_dpair_xgf_boost'])
    
    fv5_id = int(res.iloc[0]['situation_id'])
    
    # Fetch linemate stats
    query = f"""
    SELECT 
        l.game_id, l.team_id, l.player_id, l.linemate_id, 
        l.toi_seconds,
        l.xgf_pct_with, l.xgf_pct_without,
        l.hdcf,
        p.position
    FROM player_linemate_stats l
    JOIN players p ON l.player_id = p.player_id
    WHERE l.situation_id = {fv5_id}
      AND l.toi_seconds > 60
    """
    try:
        df = pd.read_sql_query(query, con)
    except Exception:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_linemate_xgf_boost', 'roll_linemate_hdcf_synergy', 'roll_dpair_xgf_boost'])
    
    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_linemate_xgf_boost', 'roll_linemate_hdcf_synergy', 'roll_dpair_xgf_boost'])

    # Standardize positions
    df['pos_group'] = df['position'].map({'C': 'F', 'L': 'F', 'R': 'F', 'D': 'D'}).fillna('F')
    
    # Calculate base metrics
    df['xgf_diff'] = df['xgf_pct_with'] - df['xgf_pct_without']
    df['hdcf_rate'] = (df['hdcf'] / (df['toi_seconds'] + 1)) * 3600
    
    # Sort linemates by TOI for each player
    df = df.sort_values(['game_id', 'team_id', 'player_id', 'toi_seconds'], ascending=[True, True, True, False])
    
    # Rank linemates
    df['rank'] = df.groupby(['game_id', 'team_id', 'player_id']).cumcount() + 1
    
    # --- Forwards: Top 3 Linemates ---
    fwd_mask = (df['pos_group'] == 'F') & (df['rank'] <= 3)
    # We need to aggregate per player first (avg of top 3 linemates)
    fwds = df[fwd_mask].groupby(['game_id', 'team_id', 'player_id']).agg(
        avg_xgf_diff=('xgf_diff', 'mean'),
        sum_hdcf_rate=('hdcf_rate', 'mean'), # Avg rate with top linemates
        total_toi=('toi_seconds', 'sum')
    ).reset_index()
    
    # Aggregate to team level (weighted by TOI)
    team_fwd = fwds.groupby(['game_id', 'team_id']).apply(
        lambda x: pd.Series({
            'linemate_xgf_boost': np.average(x['avg_xgf_diff'], weights=x['total_toi']),
            'linemate_hdcf_synergy': np.average(x['sum_hdcf_rate'], weights=x['total_toi'])
        }), include_groups=False
    ).reset_index()
    
    # --- Defense: Top 1 Partner ---
    def_mask = (df['pos_group'] == 'D') & (df['rank'] <= 1)
    defs = df[def_mask].groupby(['game_id', 'team_id', 'player_id']).agg(
        avg_xgf_diff=('xgf_diff', 'mean'),
        total_toi=('toi_seconds', 'sum')
    ).reset_index()
    
    if not defs.empty:
        team_def = defs.groupby(['game_id', 'team_id']).apply(
            lambda x: pd.Series({
                'dpair_xgf_boost': np.average(x['avg_xgf_diff'], weights=x['total_toi'])
            }), include_groups=False
        ).reset_index()
    else:
        team_def = pd.DataFrame(columns=['game_id', 'team_id', 'dpair_xgf_boost'])
        
    # Merge
    out = pd.merge(team_fwd, team_def, on=['game_id', 'team_id'], how='outer').fillna(0)
    
    # Rolling averages
    out = out.sort_values(['team_id', 'game_id'])
    grp = out.groupby('team_id')
    
    cols = ['linemate_xgf_boost', 'linemate_hdcf_synergy', 'dpair_xgf_boost']
    for c in cols:
        out[f'roll_{c}'] = grp[c].transform(lambda x: roll_mean(x, 10, 1, c))
        
    # Fill NaNs
    out['roll_linemate_xgf_boost'] = out['roll_linemate_xgf_boost'].fillna(0.0)
    out['roll_linemate_hdcf_synergy'] = out['roll_linemate_hdcf_synergy'].fillna(10.0) # Approx avg
    out['roll_dpair_xgf_boost'] = out['roll_dpair_xgf_boost'].fillna(0.0)
    
    return out[['game_id', 'team_id', 'roll_linemate_xgf_boost', 'roll_linemate_hdcf_synergy', 'roll_dpair_xgf_boost']]

def process_matchup_metrics(con) -> pd.DataFrame:
    """
    Calculates opposition matchup metrics:
    1. Opposition Suppression Factor (Defense vs Elite)
    2. Favorable Matchup Rate (Deployment)
    """
    # Use 5v5 for stable matchups
    query_sit = "SELECT situation_id FROM situations WHERE situation_code = '5v5' LIMIT 1"
    res = pd.read_sql_query(query_sit, con)
    if res.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_suppression_factor', 'roll_matchup_rate'])
        
    fv5_id = int(res.iloc[0]['situation_id'])
    
    # Fetch opposition stats
    # Approximation: Elite = Top 5 opponents by TOI
    query = f"""
    SELECT 
        o.game_id, o.team_id, o.player_id, o.opponent_id,
        o.toi_seconds,
        o.xgf_pct_with, o.xgf_pct_without
    FROM player_opposition_stats o
    WHERE o.situation_id = {fv5_id}
      AND o.toi_seconds > 30 
    """
    try:
        df = pd.read_sql_query(query, con)
    except Exception:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_suppression_factor', 'roll_matchup_rate'])
    
    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_suppression_factor', 'roll_matchup_rate'])
        
    # --- Feature 1: Opposition Suppression Factor ---
    # Aggregate TOI per opponent per game to find Elites
    opp_toi = df.groupby(['game_id', 'team_id', 'opponent_id'])['toi_seconds'].sum().reset_index()
    opp_toi = opp_toi.sort_values(['game_id', 'team_id', 'toi_seconds'], ascending=[True, True, False])
    opp_toi['rank'] = opp_toi.groupby(['game_id', 'team_id']).cumcount() + 1
    
    top5_opps = opp_toi[opp_toi['rank'] <= 5][['game_id', 'team_id', 'opponent_id']]
    top5_opps['is_elite'] = True
    
    df = pd.merge(df, top5_opps, on=['game_id', 'team_id', 'opponent_id'], how='left')
    
    # fixes "Warning: Downcasting object dtype arrays on .fillna is deprecated"
    pd.set_option('future.no_silent_downcasting', True)
    df['is_elite'] = df['is_elite'].fillna(False)
    
    df['xgf_diff'] = df['xgf_pct_with'] - df['xgf_pct_without']
    
    # Calc suppression factor: Avg xgf_diff vs Elite opponents
    # Filter for is_elite, then aggregate
    elite_matchups = df[df['is_elite']]
    if not elite_matchups.empty:
        suppression = elite_matchups.groupby(['game_id', 'team_id']).apply(
            lambda x: np.average(x['xgf_diff'], weights=x['toi_seconds']),
          include_groups=False
        ).reset_index(name='suppression_factor')
    else:
        suppression = pd.DataFrame(columns=['game_id', 'team_id', 'suppression_factor'])
    
    # --- Feature 2: Favorable Matchup Rate ---
    # % of TOI where xgf_diff > 0
    df['is_winning'] = (df['xgf_diff'] > 0).astype(int)
    df['winning_toi'] = df['is_winning'] * df['toi_seconds']
    
    matchup_rate = df.groupby(['game_id', 'team_id']).agg(
        total_winning_toi=('winning_toi', 'sum'),
        total_toi=('toi_seconds', 'sum')
    ).reset_index()
    
    matchup_rate['matchup_rate'] = matchup_rate['total_winning_toi'] / (matchup_rate['total_toi'] + 1)
    
    # Merge
    out = pd.merge(suppression, matchup_rate[['game_id', 'team_id', 'matchup_rate']], on=['game_id', 'team_id'], how='outer').fillna(0)
    
    # Rolling averages
    out = out.sort_values(['team_id', 'game_id'])
    grp = out.groupby('team_id')
    
    out['roll_suppression_factor'] = grp['suppression_factor'].transform(lambda x: roll_mean(x, 10, 1, 'suppression_factor'))
    out['roll_matchup_rate'] = grp['matchup_rate'].transform(lambda x: roll_mean(x, 10, 1, 'matchup_rate'))
    
    # Defaults
    out['roll_suppression_factor'] = out['roll_suppression_factor'].fillna(0.0)
    out['roll_matchup_rate'] = out['roll_matchup_rate'].fillna(0.5)
    
    return out[['game_id', 'team_id', 'roll_suppression_factor', 'roll_matchup_rate']]


def process_linemate_synergy(con) -> pd.DataFrame:
    """
    LSS (Linemate Synergy Score)
    Measures line chemistry by comparing performance WITH vs WITHOUT linemates
    """
    # Use 5v5 situation for most stable line combinations
    query = """
    SELECT situation_id FROM situations WHERE situation_code = '5v5' LIMIT 1
    """
    result = pd.read_sql_query(query, con)
    if result.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'lss'])

    fv5_id = int(result.iloc[0]['situation_id'])

    # Get linemate stats for recent games
    linemate_query = f"""
    SELECT
        l.game_id,
        l.player_id,
        l.linemate_id,
        l.team_id,
        l.toi_seconds,
        COALESCE(l.cf_pct_with, 0.5) as cf_pct_with,
        COALESCE(l.cf_pct_without, 0.5) as cf_pct_without,
        COALESCE(l.xgf_pct_with, 0.5) as xgf_pct_with,
        COALESCE(l.xgf_pct_without, 0.5) as xgf_pct_without
    FROM player_linemate_stats l
    WHERE l.situation_id = {fv5_id}
      AND l.toi_seconds > 60
    """
    df = pd.read_sql_query(linemate_query, con)

    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'lss'])

    # Calculate synergy score for each player-linemate pair
    # Synergy = (CF% WITH - CF% WITHOUT) + (xGF% WITH - xGF% WITHOUT)
    df['synergy_score'] = (
        (df['cf_pct_with'] - df['cf_pct_without']) +
        (df['xgf_pct_with'] - df['xgf_pct_without'])
    )

    # Weight by TOI (more ice time together = more reliable signal)
    df['weighted_synergy'] = df['synergy_score'] * df['toi_seconds']

    # For each player in each game, average synergy with their top linemates
    player_game_synergy = df.groupby(['game_id', 'team_id', 'player_id']).agg(
        total_toi=('toi_seconds', 'sum'),
        weighted_synergy_sum=('weighted_synergy', 'sum')
    ).reset_index()

    player_game_synergy['player_lss'] = (
        player_game_synergy['weighted_synergy_sum'] /
        (player_game_synergy['total_toi'] + 1.0)
    )

    # Aggregate to team level: average LSS of top 6 players by TOI
    # Sort by TOI and take top 6 per game/team
    player_game_synergy = player_game_synergy.sort_values(
        ['game_id', 'team_id', 'total_toi'],
        ascending=[True, True, False]
    )
    player_game_synergy['rank'] = player_game_synergy.groupby(['game_id', 'team_id']).cumcount() + 1

    top6 = player_game_synergy[player_game_synergy['rank'] <= 6]
    team_lss = top6.groupby(['game_id', 'team_id'])['player_lss'].mean().reset_index(name='lss')

    # Fill missing values with 0 (neutral chemistry)
    team_lss['lss'] = team_lss['lss'].fillna(0.0).clip(-0.3, 0.3)  # Cap extreme values

    return team_lss[['game_id', 'team_id', 'lss']]


def process_opposition_adjusted_xg(con) -> pd.DataFrame:
    """
    OSA_xG (Opposition Strength Adjusted Expected Goals)
    Adjusts team xG by opponent's defensive quality
    """
    all_id = get_situation_id(con)

    # Get team defensive quality (xGA/60 over recent games)
    query = f"""
    SELECT game_id, team_id,
           COALESCE(mp_xgoals_against, 0) as xga,
           COALESCE(mp_ice_time, 1) as toi
    FROM mp_team_game_stats
    WHERE situation_id = {all_id}
    """
    df = pd.read_sql_query(query, con)

    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'opp_xg_suppression'])

    # Calculate xGA per 60 minutes (toi is in seconds)
    df['xga_per_60'] = (df['xga'] / (df['toi'] + 1)) * 3600

    # Sort and calculate rolling average defensive quality
    df = df.sort_values(['team_id', 'game_id'])
    df['roll_xga_per_60'] = df.groupby('team_id')['xga_per_60'].transform(
        lambda x: roll_mean(x, 10, 1, 'xga_per_60')
    )

    # Calculate league average for normalization
    league_avg_xga_60 = df['xga_per_60'].mean()
    df['roll_xga_per_60'] = df['roll_xga_per_60'].fillna(league_avg_xga_60 if not np.isnan(league_avg_xga_60) else 2.5)

    # Suppression factor: Team_xGA_60 / League_Avg
    # Lower value = better defense (suppresses opponent xG more)
    # Factor > 1 means bad defense (inflates opponent xG)
    df['opp_xg_suppression'] = df['roll_xga_per_60'] / (league_avg_xga_60 + 0.001)

    # Clip to reasonable range (0.7 to 1.3 = 70% to 130% of league average)
    df['opp_xg_suppression'] = df['opp_xg_suppression'].clip(0.7, 1.3)

    return df[['game_id', 'team_id', 'opp_xg_suppression']]


# --- Define valid edge columns (removing nonsensical combinations) ---
# Only include features that make hockey sense:
# - blocked_shot: only defensive zone (not n, o, u)
# - giveaway: all zones except unknown (d, n, o)
# - hit: all zones except unknown (d, n, o)
# - missed_shot: only offensive zone (not d, n, u)
# - takeaway: all zones except unknown (d, n, o)

ALL_EXPECTED_EDGE_COLUMNS = [
    # Blocked shots - only defensive zone
    'edge_blocked_shot_d',

    # Giveaways - all zones except unknown
    'edge_giveaway_d',
    'edge_giveaway_n',
    'edge_giveaway_o',

    # Hits - all zones except unknown
    'edge_hit_d',
    'edge_hit_n',
    'edge_hit_o',

    # Missed shots - only offensive zone
    'edge_missed_shot_o',

    # Takeaways - all zones except unknown
    'edge_takeaway_d',
    'edge_takeaway_n',
    'edge_takeaway_o',
]
# --- End edge column definitions ---

# ... (rest of the file) ...

def process_edge_metrics(con) -> pd.DataFrame:
    # Optimized query: Aggregate in SQL to reduce data transfer and memory usage
    query = """
    SELECT 
        CAST(game_id AS TEXT) as game_id, 
        event_type,
        COALESCE(zone_code, 'U') as zone_code,
        eventOwnerTeamId as nhl_api_team_id
    FROM edge_pbp_events
    WHERE eventOwnerTeamId IS NOT NULL
      AND event_type IN ('hit', 'giveaway', 'takeaway', 'blocked-shot', 'missed-shot')
    """
    
    try:
        df_events = pd.read_sql_query(query, con)
    except Exception as e:
        print(f"Warning: Could not fetch EDGE stats: {e}")
        return pd.DataFrame(columns=['game_id', 'team_id'] + ALL_EXPECTED_EDGE_COLUMNS)
    
    if df_events.empty:
        return pd.DataFrame(columns=['game_id', 'team_id'] + ALL_EXPECTED_EDGE_COLUMNS)

    # Map NHL API IDs to Internal IDs
    # team_nhl_id_aliases: extra NHL API ids after rebrands (Utah Mammoth = 68, teams row has 59)
    teams_map_query = ("SELECT NHL_TEAM_ID, team_id FROM teams WHERE NHL_TEAM_ID IS NOT NULL "
                       "UNION SELECT nhl_team_id, team_id FROM team_nhl_id_aliases")
    api_to_internal = pd.read_sql_query(teams_map_query, con).set_index('NHL_TEAM_ID')['team_id'].to_dict()
            
    df_events['team_id'] = df_events['nhl_api_team_id'].map(api_to_internal)
    df_events = df_events.dropna(subset=['team_id'])
    df_events['team_id'] = df_events['team_id'].astype(int)

    # Process metrics
    # normalize event_type: blocked-shot -> blocked_shot
    df_events['norm_event'] = df_events['event_type'].str.replace('-', '_')
    df_events['norm_zone'] = df_events['zone_code'].str.lower()
    df_events['metric'] = 'edge_' + df_events['norm_event'] + '_' + df_events['norm_zone']

    # Aggregate
    agg = df_events.groupby(['game_id', 'team_id', 'metric']).size().reset_index(name='val')

    # Pivot
    pivot = agg.pivot_table(
        index=['game_id', 'team_id'],
        columns='metric',
        values='val',
        fill_value=0
    ).reset_index()
    
    # Ensure types
    pivot['game_id'] = pivot['game_id'].astype(str)
    pivot['team_id'] = pivot['team_id'].astype(int)

    # Ensure all expected edge columns are present
    for col in ALL_EXPECTED_EDGE_COLUMNS:
        if col not in pivot.columns:
            pivot[col] = 0
    
    return pivot[['game_id', 'team_id'] + ALL_EXPECTED_EDGE_COLUMNS]


def process_rush_metrics(con) -> pd.DataFrame:
    all_id = get_situation_id(con)
    query = f"""
    SELECT game_id, team_id, SUM(rush_attempts) as rush_attempts_for
    FROM player_game_stats
    WHERE situation_id = {all_id}
    GROUP BY game_id, team_id
    """
    df = pd.read_sql_query(query, con)
    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'rush_attempts_for'])
    return df


# Lineup-aggregated RAPM (roster-aware team strength). Ratings come from
# compute_rapm_by_season.py (rapm_by_season.csv) — leakage-safe per-season
# (each season uses prior seasons). For each team-game we TOI-weight the actual
# dressed skaters' D-RAPM (validated defensive signal) + O-RAPM. Low-TOI players
# (call-ups who barely played in the rating window) and players with no rating
# fall back to that season's league-average rating so a 1-2 game cameo can't skew
# the team aggregate.
PLAYER_PROJ = os.environ.get('NHL_PLAYER_PROJ') == '1'   # EXPERIMENT 2026-09 (off = production)

def process_player_projection(con) -> pd.DataFrame:
    """EXPERIMENT 2026-09. Lineup-built 5v5 projection per team-game from PRIOR games only:
    each dressed skater's on-ice xGF/xGA/CF/CA per hour over his earlier games (any team, since 2018),
    half-life 40 of his games, shrunk 60 5v5-min toward the previous season's league rate; weighted by
    his prior mean 5v5 TOI share (half-life 20) — never the target game's own ice time.
    TOI-weighted on-ice rates == the team rate within a game, so these are directly team-scale."""
    from scipy.signal import lfilter
    cols = ['proj_xgf60', 'proj_xga60', 'proj_cf_pct', 'proj_xg_pct']
    sk = pd.read_sql_query("""
        SELECT game_id, team_id, player_id, mp_game_date d, mp_ice_time toi, mp_onice_f_xgoals fxg,
               mp_onice_a_xgoals axg, mp_onice_f_shot_attempts fsa, mp_onice_a_shot_attempts asa
        FROM mp_skater_game_stats WHERE situation_id = 1 AND mp_ice_time > 0 AND game_id >= '2018'""", con)
    if sk.empty:
        return pd.DataFrame(columns=['game_id', 'team_id'] + cols)
    sk['game_id'] = sk['game_id'].astype(str)
    sk = sk[sk['game_id'].str[4:6].isin(['02', '03'])]
    sk['yr'] = sk['game_id'].str[:4].astype(int)
    q = ['fxg', 'axg', 'fsa', 'asa']
    lg = sk.groupby('yr')[q + ['toi']].sum()
    lg_rate = lg[q].div(lg['toi'], axis=0) * 3600                    # league per-hour rates by season
    sk = sk.sort_values(['player_id', 'd', 'game_id']).reset_index(drop=True)
    mu = lg_rate.shift(1).bfill().reindex(sk['yr']).values            # previous season's (first season: own); AFTER the sort
    sk['share'] = sk['toi'] / sk.groupby(['game_id', 'team_id'])['toi'].transform('sum')
    starts = np.r_[np.where(np.r_[True, sk['player_id'].values[1:] != sk['player_id'].values[:-1]])[0], len(sk)]
    def prior(x, H):   # per player: decayed sum over his EARLIER rows only
        dd = 0.5 ** (1.0 / H); out = np.empty(len(x))
        for a, b in zip(starts[:-1], starts[1:]):
            out[a:b] = lfilter([0, dd], [1, -dd], x[a:b])
        return out
    cnt, sh = prior(np.ones(len(sk)), 20), prior(sk['share'].values, 20)
    w = np.where(cnt > 1e-9, sh / np.maximum(cnt, 1e-9), sk['share'].median())
    S_toi = prior(sk['toi'].values.astype(float), 40)
    S = np.stack([prior(sk[c].values.astype(float) * 3600, 40) for c in q], 1)
    kps = 60 * 60.0
    rate = (S + kps * mu) / (S_toi + kps)[:, None]                    # shrunk per-hour on-ice rates
    out = pd.DataFrame(rate * w[:, None], columns=q)
    out['w'] = w; out['game_id'] = sk['game_id'].values; out['team_id'] = sk['team_id'].values
    g = out[out['game_id'] >= '2021'].groupby(['game_id', 'team_id']).sum()
    f, a, fs, as_ = [g[c] / g['w'] for c in q]
    res = pd.DataFrame({'proj_xgf60': f, 'proj_xga60': a, 'proj_cf_pct': fs / (fs + as_),
                        'proj_xg_pct': f / (f + a)}).reset_index()
    res['team_id'] = res['team_id'].astype(int)
    return res[['game_id', 'team_id'] + cols]

LINEUP_OVERRIDE = os.environ.get('NHL_LINEUP_OVERRIDE') == '1'   # EXPERIMENT 2026-09 (off = production)
# EXPERIMENT 2026-10 bet-time test: NHL_BETTIME=L|LG -> validation rows use only what was knowable at the open:
#   L : lineup-driven inputs from the team's PREVIOUS game lineup;  LG: + previous game's starting goalie
_BETTIME = None   # set only inside train() while building the bet-time feature frame
_BT_LINEUP_COLS = ['roster_drapm', 'roster_orapm', 'proj_xgf60', 'proj_xga60', 'proj_cf_pct', 'proj_xg_pct']
_BT_GOALIE_COLS = ['goalie_roll_gsax', 'goalie_roll_hd_gsax', 'goalie_roll_rcr', 'goalie_roll_fatigue_index', 'goalie_ghsf']

def _bettime_shift(df, cols):
    """replace each team-game's values with the team's previous game's values (first game keeps its own)"""
    df = df.sort_values(['team_id', 'mp_game_date'])
    for c in cols:
        if c in df.columns:
            df[c] = df.groupby('team_id')[c].shift(1).fillna(df[c])
    return df
OVR_FULL_TRUST = 0.85      # continuity at/above which team form is fully trusted
OVR_WINDOW = 10            # games behind the 10-game team-form inputs
# team-form column -> (lineup-projection column, scale): form + scale * (proj_tonight - proj_window)
OVR_DELTA = {'roll_xgf': ('proj_xgf60', 0.8), 'roll3_xgf': ('proj_xgf60', 0.8),
             'roll_goals_for': ('proj_xgf60', 0.8), 'roll3_goals_for': ('proj_xgf60', 0.8),
             'roll_xga': ('proj_xga60', 0.8), 'roll3_xga': ('proj_xga60', 0.8),
             'roll_sa_corsi_pct': ('proj_cf_pct', 1.0), 'roll3_sa_corsi_pct': ('proj_cf_pct', 1.0),
             'roll_hdcf_share': ('proj_xg_pct', 1.0), 'roll3_hdcf_share': ('proj_xg_pct', 1.0)}

def _lineup_continuity(con) -> pd.DataFrame:
    """EXPERIMENT 2026-09. Per team-game: ice-time overlap between this game's dressed skaters (weighted by
    each one's PRIOR mean TOI share, any team) and the skaters who played the team's previous OVR_WINDOW games."""
    from scipy.signal import lfilter
    lu = pd.read_sql_query("""SELECT s.game_id, s.team_id, s.player_id, s.mp_ice_time toi, g.game_date d
        FROM mp_skater_game_stats s JOIN games g ON g.game_id = s.game_id
        WHERE s.situation_id = 2 AND s.mp_ice_time > 0""", con)
    lu['game_id'] = lu['game_id'].astype(str); lu['player_id'] = lu['player_id'].astype(str)
    lu['share'] = lu['toi'] / lu.groupby(['game_id', 'team_id'])['toi'].transform('sum')
    lu = lu.sort_values(['player_id', 'd', 'game_id']).reset_index(drop=True)
    starts = np.r_[np.where(np.r_[True, lu['player_id'].values[1:] != lu['player_id'].values[:-1]])[0], len(lu)]
    dd = 0.5 ** (1 / 20.0); cnt = np.empty(len(lu)); sh = np.empty(len(lu))
    for a_, b_ in zip(starts[:-1], starts[1:]):
        cnt[a_:b_] = lfilter([0, dd], [1, -dd], np.ones(b_ - a_))
        sh[a_:b_] = lfilter([0, dd], [1, -dd], lu['share'].values[a_:b_])
    lu['w'] = np.where(cnt > 1e-9, sh / np.maximum(cnt, 1e-9), lu['share'].median())   # prior share only
    out = []
    for tid, t in lu.groupby('team_id'):
        order = t[['game_id', 'd']].drop_duplicates().sort_values(['d', 'game_id'])['game_id'].tolist()
        A = t.pivot_table(index='game_id', columns='player_id', values='toi', aggfunc='sum').reindex(order).fillna(0).values
        Wt = t.pivot_table(index='game_id', columns='player_id', values='w', aggfunc='sum').reindex(order).fillna(0).values
        Wt = Wt / np.maximum(Wt.sum(1, keepdims=True), 1e-12)
        C = np.vstack([np.zeros(A.shape[1]), np.cumsum(A, 0)])
        cont = np.full(len(order), np.nan)
        for g in range(1, len(order)):
            win = C[g] - C[max(0, g - OVR_WINDOW)]
            if win.sum() > 0:
                cont[g] = np.minimum(win / win.sum(), Wt[g]).sum()
        out.append(pd.DataFrame({'game_id': order, 'team_id': int(tid), 'lineup_continuity': cont}))
    return pd.concat(out, ignore_index=True)

def apply_lineup_override(df, con) -> pd.DataFrame:
    """EXPERIMENT 2026-09 (NHL_LINEUP_OVERRIDE=1, needs NHL_ROLL_MODE=shrink + NHL_PLAYER_PROJ=1).
    Team-form inputs re-anchored to the lineup actually dressed, using only pre-game information:
      * columns with a lineup analogue: form + scale * (projection of this lineup - mean projection of the
        lineups that played the previous OVR_WINDOW games)
      * other rolling team stats: shrunk toward the 2022-23 league prior by trust w = min(1, continuity/0.85)
      * adds `lineup_continuity` as a feature."""
    cont = _lineup_continuity(con)
    df = df.merge(cont, on=['game_id', 'team_id'], how='left')
    df['lineup_continuity'] = df['lineup_continuity'].fillna(1.0)
    if _BETTIME:   # tonight's lineup unknown -> previous game's continuity
        df = _bettime_shift(df, ['lineup_continuity'])
    df = df.sort_values(['team_id', 'mp_game_date'])
    for pc in {v[0] for v in OVR_DELTA.values()}:
        win = df.groupby('team_id')[pc].transform(lambda x: x.shift(1).rolling(OVR_WINDOW, min_periods=1).mean())
        df[f'_d_{pc}'] = (df[pc] - win).fillna(0.0)
    _override_columns(df, {pc: df[f'_d_{pc}'] for pc in {v[0] for v in OVR_DELTA.values()}}, df['lineup_continuity'])
    return df.drop(columns=[c for c in df.columns if c.startswith('_d_')])

def _override_columns(obj, deltas, continuity):
    """Shared by training (DataFrame) and the sim (one team Series): roster-delta on analogue columns,
    continuity-weighted shrink toward the 2022-23 prior on the rest, rebuild consolidated EDGE columns. In place."""
    cols = list(obj.columns) if isinstance(obj, pd.DataFrame) else list(obj.index)
    for col, (pc, k) in OVR_DELTA.items():
        if col in cols:
            obj[col] = obj[col] + k * deltas[pc]
    w = np.minimum(1.0, continuity / OVR_FULL_TRUST)
    for col in cols:
        if not col.startswith(('roll_', 'roll3_', 'roll5_', 'roll10_', 'roll20_')) or col in OVR_DELTA:
            continue
        base = col.split('_', 1)[1]
        base = {'win_rate': 'win'}.get(base, base)      # roll5/roll10_win_rate are rolled from raw column 'win'
        mu = _ROLL_PRIORS.get(base)
        if mu is not None and np.isfinite(mu):
            obj[col] = w * obj[col] + (1 - w) * mu
    # consolidated EDGE features are sums/ratios of the (now shrunk) zone columns: rebuild them
    if all(c in cols for c in ['roll10_edge_giveaway_d', 'roll10_edge_giveaway_n', 'roll10_edge_giveaway_o']):
        obj['roll_edge_giveaway_total'] = obj['roll10_edge_giveaway_d'] + obj['roll10_edge_giveaway_n'] + obj['roll10_edge_giveaway_o']
        obj['roll_edge_giveaway_dzone_pct'] = obj['roll10_edge_giveaway_d'] / (obj['roll_edge_giveaway_total'] + 0.1)
        obj['roll3_edge_giveaway_total'] = obj['roll3_edge_giveaway_d'] + obj['roll3_edge_giveaway_n'] + obj['roll3_edge_giveaway_o']
    if 'roll10_edge_blocked_shot_d' in cols:
        obj['roll_edge_dzone_blocks'] = obj['roll10_edge_blocked_shot_d']
        obj['roll3_edge_dzone_blocks'] = obj['roll3_edge_blocked_shot_d']
    return obj

def project_lineup_asof(con, player_ids, before_date):
    """EXPERIMENT 2026-10 (sim side). Same math as process_player_projection, for ANY lineup, using each
    player's games strictly before `before_date` (YYYY-MM-DD). Returns {proj_xgf60, proj_xga60, proj_cf_pct, proj_xg_pct}."""
    from scipy.signal import lfilter
    sk = pd.read_sql_query("""
        SELECT game_id, team_id, player_id, mp_game_date d, mp_ice_time toi, mp_onice_f_xgoals fxg,
               mp_onice_a_xgoals axg, mp_onice_f_shot_attempts fsa, mp_onice_a_shot_attempts asa
        FROM mp_skater_game_stats WHERE situation_id = 1 AND mp_ice_time > 0 AND game_id >= '2018'""", con)
    sk['game_id'] = sk['game_id'].astype(str); sk['player_id'] = sk['player_id'].astype(str)
    sk = sk[sk['game_id'].str[4:6].isin(['02', '03'])]
    sk['yr'] = sk['game_id'].str[:4].astype(int)
    q = ['fxg', 'axg', 'fsa', 'asa']
    lg = sk.groupby('yr')[q + ['toi']].sum(); lg_rate = lg[q].div(lg['toi'], axis=0) * 3600
    tonight_yr = int(before_date[:4]) if int(before_date[5:7]) >= 9 else int(before_date[:4]) - 1
    prev = [y for y in lg_rate.index if y < tonight_yr]
    mu = lg_rate.loc[prev[-1] if prev else lg_rate.index.min()].values
    sk['share'] = sk['toi'] / sk.groupby(['game_id', 'team_id'])['toi'].transform('sum')
    dkey = before_date.replace('-', '')
    hist = sk[sk['d'].astype(str).str.replace('-', '') < dkey].sort_values(['player_id', 'd', 'game_id'])
    kps = 60 * 60.0
    rates, weights = [], []
    for pid in [str(p) for p in player_ids]:
        h = hist[hist['player_id'] == pid]
        if h.empty:
            rates.append(mu); weights.append(sk['share'].median()); continue
        def post(x, H):   # decayed sum over ALL his earlier rows, as of the next game
            dd = 0.5 ** (1.0 / H)
            return lfilter([0, dd], [1, -dd], np.r_[x, 0.0])[-1]
        cnt, shs = post(np.ones(len(h)), 20), post(h['share'].values, 20)
        weights.append(shs / cnt if cnt > 1e-9 else sk['share'].median())
        S_toi = post(h['toi'].values.astype(float), 40)
        S = np.array([post(h[c].values.astype(float) * 3600, 40) for c in q])
        rates.append((S + kps * mu) / (S_toi + kps))
    rates, weights = np.array(rates), np.array(weights)
    f, a, fs, as_ = (rates * weights[:, None]).sum(0) / weights.sum()
    return {'proj_xgf60': f, 'proj_xga60': a, 'proj_cf_pct': fs / (fs + as_), 'proj_xg_pct': f / (f + a)}

def continuity_asof(con, team_id, player_ids, before_date):
    """EXPERIMENT 2026-10 (sim side). Same measure as _lineup_continuity, for ANY lineup: overlap between the
    lineup (each player weighted by his prior mean TOI share, any team, H=20) and the team's last OVR_WINDOW games
    before `before_date`."""
    from scipy.signal import lfilter
    lu = pd.read_sql_query("""SELECT s.game_id, s.team_id, s.player_id, s.mp_ice_time toi, g.game_date d
        FROM mp_skater_game_stats s JOIN games g ON g.game_id = s.game_id
        WHERE s.situation_id = 2 AND s.mp_ice_time > 0 AND g.game_date < ?""", con, params=[before_date])
    lu['game_id'] = lu['game_id'].astype(str); lu['player_id'] = lu['player_id'].astype(str)
    lu['share'] = lu['toi'] / lu.groupby(['game_id', 'team_id'])['toi'].transform('sum')
    med = lu['share'].median()
    wts = {}
    for pid in [str(p) for p in player_ids]:
        h = lu[lu['player_id'] == pid].sort_values(['d', 'game_id'])
        dd = 0.5 ** (1 / 20.0)
        cnt = lfilter([0, dd], [1, -dd], np.r_[np.ones(len(h)), 0.0])[-1]
        sh = lfilter([0, dd], [1, -dd], np.r_[h['share'].values, 0.0])[-1]
        wts[pid] = sh / cnt if cnt > 1e-9 else med
    t = lu[lu['team_id'] == int(team_id)]
    last = t[['game_id', 'd']].drop_duplicates().sort_values(['d', 'game_id']).tail(OVR_WINDOW)['game_id']
    win = t[t['game_id'].isin(last)].groupby('player_id')['toi'].sum()
    if win.sum() <= 0:
        return 1.0
    win = win / win.sum()
    W = pd.Series(wts); W = W / W.sum()
    return float(np.minimum(win.reindex(W.index).fillna(0.0), W).sum())

ROSTER_RAPM_PATH = "rapm_by_season.csv"
ROSTER_MIN_TOI = 150.0  # 5v5 minutes in the rating window to trust an individual rating

def process_roster_rapm(con) -> pd.DataFrame:
    cols = ['game_id', 'team_id', 'roster_drapm', 'roster_orapm']
    if not os.path.exists(ROSTER_RAPM_PATH):
        print(f"Note: {ROSTER_RAPM_PATH} not found — roster RAPM features will be 0. "
              f"Run compute_rapm_by_season.py to enable.")
        return pd.DataFrame(columns=cols)

    rt = pd.read_csv(ROSTER_RAPM_PATH)
    rt['player_id'] = rt['player_id'].astype(str)
    rt['ok'] = rt['toi_5v5_min'] >= ROSTER_MIN_TOI

    # per-season league averages over trustworthy (qualified-TOI) players
    qual = rt[rt['ok']]
    lg = qual.groupby('season').agg(lg_d=('d_rapm', 'mean'), lg_o=('o_rapm', 'mean'))
    lg_d_all, lg_o_all = qual['d_rapm'].mean(), qual['o_rapm'].mean()

    all_id = get_situation_id(con)
    ros = pd.read_sql_query(f"""
        SELECT s.game_id, s.team_id, s.player_id, s.mp_ice_time AS toi, g.season
        FROM mp_skater_game_stats s JOIN games g ON s.game_id = g.game_id
        WHERE s.situation_id = {all_id} AND s.mp_ice_time > 0
    """, con)
    if ros.empty:
        return pd.DataFrame(columns=cols)
    ros['player_id'] = ros['player_id'].astype(str)
    ros['game_id'] = ros['game_id'].astype(str)

    ros = ros.merge(rt[['season', 'player_id', 'd_rapm', 'o_rapm', 'ok']],
                    on=['season', 'player_id'], how='left')
    ros['ok'] = ros['ok'].fillna(False)
    # league-average fallback (per season, else global)
    ros['lg_d'] = ros['season'].map(lg['lg_d']).fillna(lg_d_all)
    ros['lg_o'] = ros['season'].map(lg['lg_o']).fillna(lg_o_all)
    ros['d_use'] = np.where(ros['ok'], ros['d_rapm'], ros['lg_d'])
    ros['o_use'] = np.where(ros['ok'], ros['o_rapm'], ros['lg_o'])

    def wavg(g, c):
        w = g['toi'].values
        return float(np.average(g[c].values, weights=w)) if w.sum() > 0 else 0.0
    agg = (ros.groupby(['game_id', 'team_id'])
              .apply(lambda g: pd.Series({'roster_drapm': wavg(g, 'd_use'),
                                          'roster_orapm': wavg(g, 'o_use')}),
                     include_groups=False)
              .reset_index())
    agg['team_id'] = agg['team_id'].astype(int)
    return agg[cols]


# ---------------------------
# Data Prep
# ---------------------------
def get_complete_games(con):
    """
    Return only game_ids with complete data in all required tables.
    This ensures training data has no missing features.

    OPTIMIZED: Uses simple queries + set intersection instead of nested EXISTS.
    ~300x faster than original implementation (70s -> 0.2s)
    """
    ALL_ID = get_situation_id(con)

    # Get all game IDs as baseline
    all_games = set(pd.read_sql_query("SELECT DISTINCT game_id FROM games", con)['game_id'])

    # 1. MoneyPuck All situation (both teams with correct team IDs)
    mp_all_query = f"""
    SELECT g.game_id
    FROM games g
    WHERE EXISTS (
        SELECT 1 FROM mp_team_game_stats mp_home
        WHERE mp_home.game_id = g.game_id
        AND mp_home.team_id = g.home_team_id
        AND mp_home.situation_id = {ALL_ID}
    )
    AND EXISTS (
        SELECT 1 FROM mp_team_game_stats mp_away
        WHERE mp_away.game_id = g.game_id
        AND mp_away.team_id = g.away_team_id
        AND mp_away.situation_id = {ALL_ID}
    )
    """
    mp_all_games = set(pd.read_sql_query(mp_all_query, con)['game_id'])

    # 2. MoneyPuck PP data
    mp_pp_query = """
    SELECT DISTINCT game_id
    FROM mp_team_game_stats mp
    JOIN situations s ON mp.situation_id = s.situation_id
    WHERE s.situation_code = 'PP'
    """
    mp_pp_games = set(pd.read_sql_query(mp_pp_query, con)['game_id'])

    # 3. MoneyPuck PK data
    mp_pk_query = """
    SELECT DISTINCT game_id
    FROM mp_team_game_stats mp
    JOIN situations s ON mp.situation_id = s.situation_id
    WHERE s.situation_code = 'PK'
    """
    mp_pk_games = set(pd.read_sql_query(mp_pk_query, con)['game_id'])

    # 4. NST team_game_overview
    nst_query = f"""
    SELECT DISTINCT game_id
    FROM team_game_overview
    WHERE situation_id = {ALL_ID}
    """
    nst_games = set(pd.read_sql_query(nst_query, con)['game_id'])

    # 5. Shot data (minimum 20 shots)
    shot_query = """
    SELECT game_id
    FROM mp_shots
    GROUP BY game_id
    HAVING COUNT(*) >= 20
    """
    shot_games = set(pd.read_sql_query(shot_query, con)['game_id'])

    # 6. Goalie data (both teams, NST or MP)
    goalie_query = f"""
    SELECT game_id
    FROM (
        SELECT game_id, team_id FROM goalie_game_stats WHERE situation_id = {ALL_ID}
        UNION
        SELECT game_id, team_id FROM mp_goalie_game_stats WHERE situation_id = {ALL_ID}
    )
    GROUP BY game_id
    HAVING COUNT(DISTINCT team_id) = 2
    """
    goalie_games = set(pd.read_sql_query(goalie_query, con)['game_id'])

    # Set intersection - games that meet ALL criteria
    complete_games = (
        all_games &
        mp_all_games &
        mp_pp_games &
        mp_pk_games &
        nst_games &
        shot_games &
        goalie_games
    )

    # Create temp table for efficient downstream filtering
    con.execute("DROP TABLE IF EXISTS temp_game_filter")
    con.execute("CREATE TEMP TABLE temp_game_filter (game_id TEXT PRIMARY KEY)")

    if complete_games:
        con.executemany(
            "INSERT INTO temp_game_filter VALUES (?)",
            [(gid,) for gid in complete_games]
        )
        con.commit()

    return list(complete_games)


def validate_training_data(df, verbose=True):
    """
    Validate data quality and report potential issues.
    Returns True if data passes quality checks.
    """
    feature_cols = [c for c in df.columns if c not in ['game_id', 'goals_home', 'goals_away', 'mp_game_date']]

    issues = []
    warnings = []

    for col in feature_cols:
        zero_pct = (df[col] == 0).sum() / len(df) * 100
        nan_pct = df[col].isna().sum() / len(df) * 100

        if nan_pct > 0:
            issues.append(f"{col}: {nan_pct:.1f}% NaNs")
        elif zero_pct > 30:
            warnings.append(f"{col}: {zero_pct:.1f}% zeros")

    if verbose:
        print("\n" + "=" * 70)
        print("DATA QUALITY VALIDATION")
        print("=" * 70)
        print(f"Dataset size: {len(df)} games")
        print(f"Column count (pre-selection): {len(feature_cols)}  (model feature count printed at training start)")

        if issues:
            print(f"\n⚠️  CRITICAL ISSUES ({len(issues)}):")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print("\n✓ No NaN values detected")

        if warnings:
            print(f"\n⚠️  WARNINGS ({len(warnings)} features >30% zeros):")
            for warning in warnings[:5]:  # Show first 5
                print(f"  - {warning}")
            if len(warnings) > 5:
                print(f"  ... and {len(warnings) - 5} more")
        else:
            print("✓ No excessive zero values detected")

        print("=" * 70 + "\n")

    return len(issues) == 0


def process_win_rate_features(con) -> pd.DataFrame:
    """
    Calculate rolling win rate features for each team.
    Win rate has strong correlation with goals (r=0.15 for 5-game window).
    """
    all_id = get_situation_id(con)
    
    # Get game results
    query = f"""
    SELECT 
        g.game_id,
        g.game_date,
        g.home_team_id,
        g.away_team_id,
        COALESCE(h.mp_goals_for, 0) as home_goals,
        COALESCE(a.mp_goals_for, 0) as away_goals
    FROM games g
    LEFT JOIN mp_team_game_stats h ON g.game_id = h.game_id AND g.home_team_id = h.team_id AND h.situation_id = {all_id}
    LEFT JOIN mp_team_game_stats a ON g.game_id = a.game_id AND g.away_team_id = a.team_id AND a.situation_id = {all_id}
    ORDER BY g.game_date
    """
    df = pd.read_sql_query(query, con)
    
    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll5_win_rate', 'roll10_win_rate'])
    
    # Stack home and away to get team-centric view
    home = df[['game_id', 'game_date', 'home_team_id', 'home_goals', 'away_goals']].copy()
    home.columns = ['game_id', 'game_date', 'team_id', 'goals_for', 'goals_against']
    
    away = df[['game_id', 'game_date', 'away_team_id', 'away_goals', 'home_goals']].copy()
    away.columns = ['game_id', 'game_date', 'team_id', 'goals_for', 'goals_against']
    
    team_games = pd.concat([home, away]).sort_values(['team_id', 'game_date'])
    
    # Calculate win indicator
    team_games['goal_diff'] = team_games['goals_for'] - team_games['goals_against']
    team_games['win'] = (team_games['goal_diff'] > 0).astype(int)
    
    grp = team_games.groupby('team_id')
    
    # Rolling win rates (shift to avoid leakage)
    team_games['roll5_win_rate'] = grp['win'].transform(
        lambda x: roll_mean(x, 5, 1, 'win')
    )
    team_games['roll10_win_rate'] = grp['win'].transform(
        lambda x: roll_mean(x, 10, 1, 'win')
    )
    
    # Fill NaNs with 0.5 (neutral)
    team_games['roll5_win_rate'] = team_games['roll5_win_rate'].fillna(0.5)
    team_games['roll10_win_rate'] = team_games['roll10_win_rate'].fillna(0.5)
    
    return team_games[['game_id', 'team_id', 'roll5_win_rate', 'roll10_win_rate']]


def process_home_ice_features(con) -> pd.DataFrame:
    """
    Calculate home ice advantage features:
    1. Per-team rolling home/away xG differential (how much better at home?)
    2. Per-team rolling home/away win rate differential

    These capture team-specific venue effects (altitude, crowd, last change, etc.)
    After home/away split in get_base_team_stats, the model sees:
    - home_home_ice_xg_boost: high = this home team gains a lot from playing at home
    - away_home_ice_xg_boost: high = this away team is *missing* their usual home boost
    """
    all_id = get_situation_id(con)

    query = f"""
    SELECT
        g.game_id,
        g.game_date,
        g.home_team_id,
        g.away_team_id,
        COALESCE(h.mp_xgoals_for, 0) as home_xgf,
        COALESCE(a.mp_xgoals_for, 0) as away_xgf,
        COALESCE(h.mp_goals_for, 0) as home_goals,
        COALESCE(a.mp_goals_for, 0) as away_goals
    FROM games g
    LEFT JOIN mp_team_game_stats h ON g.game_id = h.game_id AND g.home_team_id = h.team_id AND h.situation_id = {all_id}
    LEFT JOIN mp_team_game_stats a ON g.game_id = a.game_id AND g.away_team_id = a.team_id AND a.situation_id = {all_id}
    ORDER BY g.game_date
    """
    df = pd.read_sql_query(query, con)

    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'home_ice_xg_boost', 'home_ice_win_boost'])

    # Build per-team home-only and away-only stat histories
    # Home perspective: team played at home
    home_rows = df[['game_id', 'game_date', 'home_team_id', 'home_xgf', 'home_goals', 'away_goals']].copy()
    home_rows.columns = ['game_id', 'game_date', 'team_id', 'xgf', 'goals_for', 'goals_against']
    home_rows['is_home_game'] = 1

    # Away perspective: team played on the road
    away_rows = df[['game_id', 'game_date', 'away_team_id', 'away_xgf', 'away_goals', 'home_goals']].copy()
    away_rows.columns = ['game_id', 'game_date', 'team_id', 'xgf', 'goals_for', 'goals_against']
    away_rows['is_home_game'] = 0

    all_games = pd.concat([home_rows, away_rows]).sort_values(['team_id', 'game_date'])
    all_games['win'] = (all_games['goals_for'] > all_games['goals_against']).astype(float)

    # For each team, compute rolling averages separately for home and away games
    # Then attach the differential to each game row
    results = []

    for team_id, team_df in all_games.groupby('team_id'):
        team_df = team_df.sort_values('game_date').copy()

        # Separate home and away game histories
        home_mask = team_df['is_home_game'] == 1
        away_mask = team_df['is_home_game'] == 0

        # Rolling xGF at home (last 10 home games, shifted)
        home_xgf_vals = team_df.loc[home_mask, 'xgf']
        away_xgf_vals = team_df.loc[away_mask, 'xgf']

        home_win_vals = team_df.loc[home_mask, 'win']
        away_win_vals = team_df.loc[away_mask, 'win']

        # Expanding rolling mean for home games (shift 1 to avoid leakage)
        team_df.loc[home_mask, 'roll_home_xgf'] = home_xgf_vals.shift(1).rolling(10, min_periods=3).mean()
        team_df.loc[away_mask, 'roll_away_xgf'] = away_xgf_vals.shift(1).rolling(10, min_periods=3).mean()

        team_df.loc[home_mask, 'roll_home_win'] = home_win_vals.shift(1).rolling(10, min_periods=3).mean()
        team_df.loc[away_mask, 'roll_away_win'] = away_win_vals.shift(1).rolling(10, min_periods=3).mean()

        # Forward-fill so away games have the latest home stats and vice versa
        team_df['roll_home_xgf'] = team_df['roll_home_xgf'].ffill()
        team_df['roll_away_xgf'] = team_df['roll_away_xgf'].ffill()
        team_df['roll_home_win'] = team_df['roll_home_win'].ffill()
        team_df['roll_away_win'] = team_df['roll_away_win'].ffill()

        # Differentials: positive = team plays better at home
        team_df['home_ice_xg_boost'] = team_df['roll_home_xgf'] - team_df['roll_away_xgf']
        team_df['home_ice_win_boost'] = team_df['roll_home_win'] - team_df['roll_away_win']

        results.append(team_df[['game_id', 'team_id', 'home_ice_xg_boost', 'home_ice_win_boost']])

    out = pd.concat(results)

    # Fill NaNs with 0 (neutral - no home ice data yet)
    out['home_ice_xg_boost'] = out['home_ice_xg_boost'].fillna(0.0)
    out['home_ice_win_boost'] = out['home_ice_win_boost'].fillna(0.0)

    return out


# EXPERIMENT 2026-10: cross-season FORM DISCOUNT. Rolling team-form windows run straight through the offseason, so on
# opening night a team's "form" is last spring's games played by a different roster. The share of each window that
# comes from LAST season is pulled toward the previous season's league mean by (1 - carry); carry per feature is the
# measured cross-season persistence relative to within-season persistence (form_carry.json, fit on seasons <= 2022-23).
FORM_DISCOUNT = os.environ.get('NHL_FORM_DISCOUNT') == '1'
FORM_CARRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'form_carry.json')
_FORM_RE = re.compile(r'^(roll\d*_|home_ice_|opp_xg_suppression$|hdsm$)')

def _form_window(c):
    mm = re.match(r'roll(\d+)_', c)
    return int(mm.group(1)) if mm else (20 if c.startswith('home_ice_') else (3 if c == 'hdsm' else 10))

def form_discount_factor(c, games_this_season, carry):
    """Multiplier on (value - league mean): 1 when the window is all this season, carry when it is all last season."""
    n = _form_window(c)
    w_prev = np.clip((n - np.asarray(games_this_season, dtype=float)) / n, 0.0, 1.0)
    return 1.0 - w_prev * (1.0 - carry.get(c, carry['_default']))

def _load_form_carry():
    with open(FORM_CARRY_PATH) as fh:
        return {k: v for k, v in json.load(fh).items() if k != '_note'}

def apply_form_discount(df):
    """df: team-level rows (team_id, game_id, roll_* ...). Leak-free: league means are the PREVIOUS season's."""
    if ROLL_MODE != 'window':
        raise SystemExit("NHL_FORM_DISCOUNT is only defined for window rolling (NHL_ROLL_MODE unset)")
    carry = _load_form_carry()
    df = df.sort_values(['team_id', 'mp_game_date', 'game_id']).copy()
    season = df['game_id'].astype(str).str[:4].astype(int)
    g = df.groupby(['team_id', season]).cumcount().values                # this team's games this season before this one
    had_prev = (df.groupby('team_id').cumcount().values - g) > 0         # team has games from an earlier season
    g = np.where(had_prev, g, 10_000)                                    # first season in the DB: nothing to discount
    cols = [c for c in df.columns if _FORM_RE.match(c) and pd.api.types.is_numeric_dtype(df[c])]
    for c in cols:
        by_year = df.groupby(season)[c].mean()
        mu = season.map(by_year.shift(1).fillna(by_year)).astype(float).values
        df[c] = mu + (df[c].values - mu) * form_discount_factor(c, g, carry)
    return df

def get_base_team_stats(db_path, use_complete_games_filter=True):
    con = _connect(db_path)
    ALL_ID = get_situation_id(con)

    # Get filtered game list if enabled
    if use_complete_games_filter:
        complete_games = get_complete_games(con)
        print(f"✓ Filtering to {len(complete_games)} games with complete data (from {pd.read_sql_query('SELECT COUNT(*) as c FROM games', con).iloc[0]['c']} total)")

        if len(complete_games) == 0:
            print("⚠️  WARNING: No games passed the complete data filter!")
            print("   Falling back to unfiltered data...")
            # Create temp table with all games
            con.execute("DROP TABLE IF EXISTS temp_game_filter")
            con.execute("CREATE TEMP TABLE temp_game_filter AS SELECT DISTINCT game_id FROM games")
    else:
        print("⚠️  Running WITHOUT complete games filter (expect missing data)")
        # Create temp table with all games
        con.execute("DROP TABLE IF EXISTS temp_game_filter")
        con.execute("CREATE TEMP TABLE temp_game_filter AS SELECT DISTINCT game_id FROM games")

    # Use temp table JOIN instead of IN clause (much faster)
    base = f"""
    WITH teamsplit AS (
        SELECT g.game_id, g.home_team_id AS team_id, 'HOME' AS side, g.game_date
        FROM temp_game_filter f
        JOIN games g ON f.game_id = g.game_id
        UNION ALL
        SELECT g.game_id, g.away_team_id AS team_id, 'AWAY' AS side, g.game_date
        FROM temp_game_filter f
        JOIN games g ON f.game_id = g.game_id
    )
    SELECT t.game_id, t.team_id, t.game_date as mp_game_date, t.side,
           COALESCE(m.mp_goals_for,0) as goals_for,
           COALESCE(m.mp_xgoals_for,0) as xgf,
           COALESCE(m.mp_xgoals_against,0) as xga,
           COALESCE(m.mp_penalties_for,0) as pens
    FROM teamsplit t
    LEFT JOIN mp_team_game_stats m ON m.game_id = t.game_id AND m.team_id = t.team_id AND m.situation_id = {ALL_ID}
    """
    df = pd.read_sql_query(base, con)
    # print(f"DEBUG: get_base_team_stats initial df size: {len(df)}")
    # if not df.empty:
    #      print(f"DEBUG: 2025020450 in base df? { '2025020450' in df['game_id'].astype(str).values }")

    df['mp_game_date'] = pd.to_datetime(df['mp_game_date'])

    for func in ([process_special_teams, process_nst_metrics,
                  process_shot_metrics, process_advanced_metrics, process_edge_metrics,
                  process_opposition_adjusted_xg, process_linemate_synergy, process_rush_metrics,
                  process_skater_chemistry, process_matchup_metrics, process_win_rate_features,
                  process_home_ice_features, process_roster_rapm]
                 + ([process_player_projection] if PLAYER_PROJ else [])):
        extra = func(con)
        if not extra.empty:
            df = pd.merge(df, extra, on=['game_id', 'team_id'], how='left')

    # NEW: Get GOALIE features (per-goalie level)
    goalie_features = process_goalie_metrics(con)

    # NEW: Identify starting goalies
    starting_goalies = identify_starting_goalie(con)

    # NEW: Join starting goalie features to games
    # Match on game_id + team_id, get player_id from starting_goalies
    if not starting_goalies.empty and not goalie_features.empty:
        goalie_features_starters = pd.merge(
            starting_goalies,
            goalie_features,
            on=['game_id', 'team_id', 'player_id'],
            how='left'
        )

        # Rename goalie features to include 'goalie_' prefix
        goalie_features_starters = goalie_features_starters.rename(columns={
            'player_id': 'goalie_player_id',
            'roll_gsax': 'goalie_roll_gsax',
            'roll_hd_gsax': 'goalie_roll_hd_gsax',
            'roll_rcr': 'goalie_roll_rcr',
            'roll_fatigue_index': 'goalie_roll_fatigue_index',
            'ghsf': 'goalie_ghsf'
        })
        
        # Drop toi_seconds/is_starter/goalie_rank if present from starting_goalies merge
        drop_cols = [c for c in goalie_features_starters.columns 
                     if c not in ['game_id', 'team_id', 'goalie_player_id', 
                                  'goalie_roll_gsax', 'goalie_roll_hd_gsax', 
                                  'goalie_roll_rcr', 'goalie_roll_fatigue_index', 'goalie_ghsf']]
        # actually, keep game_id and team_id for merge
        
        goalie_features_starters = goalie_features_starters[['game_id', 'team_id', 'goalie_player_id',
                                                             'goalie_roll_gsax', 'goalie_roll_hd_gsax',
                                                             'goalie_roll_rcr', 'goalie_roll_fatigue_index', 'goalie_ghsf']]

        # Merge goalie features into main dataframe
        df = pd.merge(df, goalie_features_starters, on=['game_id', 'team_id'], how='left')
    
    # Fill missing goalie features (games where goalie data unavailable)
    # Default values based on league averages calculated in process_goalie_metrics
    LEAGUE_AVG_RCR = 0.92   # measured mean rebound control (1 - rebounds/saves); was 0.82
    LEAGUE_AVG_FATIGUE = 150.0
    
    for c in ['goalie_roll_gsax', 'goalie_roll_hd_gsax', 'goalie_ghsf']:
        if c in df.columns: df[c] = df[c].fillna(0.0)
        else: df[c] = 0.0
            
    if 'goalie_roll_rcr' in df.columns: df['goalie_roll_rcr'] = df['goalie_roll_rcr'].fillna(LEAGUE_AVG_RCR)
    else: df['goalie_roll_rcr'] = LEAGUE_AVG_RCR
        
    if 'goalie_roll_fatigue_index' in df.columns: df['goalie_roll_fatigue_index'] = df['goalie_roll_fatigue_index'].fillna(LEAGUE_AVG_FATIGUE)
    else: df['goalie_roll_fatigue_index'] = LEAGUE_AVG_FATIGUE

    con.close()
    # 2026-10-02: a rolled input that is MISSING for a team-game (e.g. no linemate/matchup rows) gets the column
    # median, not 0 — a 0 read as a real extreme value (roll_matchup_rate = 0 was -21 SD).
    for _c in [c for c in df.columns if c.startswith(('roll_', 'roll3_', 'roll5_', 'roll10_', 'roll20_'))]:
        if df[_c].isna().any():
            df[_c] = df[_c].fillna(df[_c].median())
    df = df.fillna(0);
    assert(df is not None), "dataframe got nuked";
    df = df.sort_values(['team_id', 'mp_game_date'])

    grp = df.groupby('team_id')
    for c in ['xgf', 'xga', 'pens', 'goals_for', 'rush_attempts_for', 'avg_dist', 'avg_angle']:
        # Standard 10-game rolling
        df[f'roll_{c}'] = grp[c].transform(lambda x: roll_mean(x, 10, 1, c))

        # Trend rolling (3 and 20)
        df[f'roll3_{c}'] = grp[c].transform(lambda x: roll_mean(x, 3, 1, c))
        df[f'roll20_{c}'] = grp[c].transform(lambda x: roll_mean(x, 20, 1, c))

        # Fill NaNs
        league_avg = df[c].mean()
        for prefix in ['roll_', 'roll3_', 'roll20_']:
            df[f'{prefix}{c}'] = df[f'{prefix}{c}'].fillna(league_avg if not np.isnan(league_avg) else 0.0)

    # Roll LSS: 10-game shifted window so it's always pre-game information
    if 'lss' in df.columns:
        df['roll_lss'] = grp['lss'].transform(lambda x: roll_mean(x, 10, 1, 'lss'))
        df['roll_lss'] = df['roll_lss'].fillna(0.0)

    # Apply rolling averages to edge_giveaway and edge_blocked_shot features
    # These need 3-game and 10-game rolling averages
    # Only process valid features (as defined in ALL_EXPECTED_EDGE_COLUMNS)
    edge_cols_to_roll = []
    for event_type in ['giveaway', 'blocked_shot']:
        for zone in ['d', 'n', 'o']:  # Exclude 'u' (unknown)
            col_name = f'edge_{event_type}_{zone}'
            # Additional validation: only include if it's in our whitelist
            if col_name in ALL_EXPECTED_EDGE_COLUMNS and col_name in df.columns:
                edge_cols_to_roll.append(col_name)

    for c in edge_cols_to_roll:
        # 3-game rolling average
        df[f'roll3_{c}'] = grp[c].transform(lambda x: roll_mean(x, 3, 1, c))

        # 10-game rolling average
        df[f'roll10_{c}'] = grp[c].transform(lambda x: roll_mean(x, 10, 1, c))

        # Fill NaNs with league average
        league_avg = df[c].mean()
        df[f'roll3_{c}'] = df[f'roll3_{c}'].fillna(league_avg if not np.isnan(league_avg) else 0.0)
        df[f'roll10_{c}'] = df[f'roll10_{c}'].fillna(league_avg if not np.isnan(league_avg) else 0.0)

        # Replace the raw single-game value with the 10-game rolling average as the default
        df[c] = df[f'roll10_{c}']

    # CONSOLIDATED EDGE FEATURES: Combine zone-specific features into more robust metrics
    # This reduces feature count while preserving signal

    # Giveaway consolidation: total + defensive zone ratio (d-zone giveaways are more costly)
    giveaway_cols = ['edge_giveaway_d', 'edge_giveaway_n', 'edge_giveaway_o']
    if all(c in df.columns for c in giveaway_cols):
        # Total giveaways (using 10-game rolling values)
        df['roll_edge_giveaway_total'] = (
            df['roll10_edge_giveaway_d'] +
            df['roll10_edge_giveaway_n'] +
            df['roll10_edge_giveaway_o']
        )
        # D-zone giveaway percentage (higher = worse, more costly turnovers)
        df['roll_edge_giveaway_dzone_pct'] = df['roll10_edge_giveaway_d'] / (df['roll_edge_giveaway_total'] + 0.1)

        # Also create 3-game versions for trend detection
        df['roll3_edge_giveaway_total'] = (
            df['roll3_edge_giveaway_d'] +
            df['roll3_edge_giveaway_n'] +
            df['roll3_edge_giveaway_o']
        )
    else:
        df['roll_edge_giveaway_total'] = 0.0
        df['roll_edge_giveaway_dzone_pct'] = 0.0
        df['roll3_edge_giveaway_total'] = 0.0

    # Blocked shot consolidation: keep d-zone as primary (that's where blocks matter most)
    # and rename for clarity
    if 'roll10_edge_blocked_shot_d' in df.columns:
        df['roll_edge_dzone_blocks'] = df['roll10_edge_blocked_shot_d']
        df['roll3_edge_dzone_blocks'] = df['roll3_edge_blocked_shot_d']
    else:
        df['roll_edge_dzone_blocks'] = 0.0
        df['roll3_edge_dzone_blocks'] = 0.0

    if FORM_DISCOUNT:   # EXPERIMENT 2026-10 (off = production)
        df = apply_form_discount(df)

    df['prev_date'] = grp['mp_game_date'].shift(1)
    df['rest_days'] = (df['mp_game_date'] - df['prev_date']).dt.days.fillna(2).clip(0, REST_CAP)

    if _BETTIME:   # EXPERIMENT 2026-10: bet-time information only (see train())
        df = _bettime_shift(df, _BT_LINEUP_COLS + (_BT_GOALIE_COLS if _BETTIME == 'LG' else []))

    if LINEUP_OVERRIDE:   # EXPERIMENT 2026-09
        if not (PLAYER_PROJ and ROLL_MODE == 'shrink'):
            raise SystemExit("NHL_LINEUP_OVERRIDE=1 needs NHL_ROLL_MODE=shrink and NHL_PLAYER_PROJ=1")
        _c = _connect(db_path); df = apply_lineup_override(df, _c); _c.close()

    home = df[df['side'] == 'HOME'].rename(columns=lambda c: f"home_{c}" if c not in ['game_id'] else c)
    away = df[df['side'] == 'AWAY'].rename(columns=lambda c: f"away_{c}" if c not in ['game_id'] else c)

    home = home.rename(columns={'home_goals_for': 'goals_home', 'home_rest_days': 'home_rest'})
    away = away.rename(columns={'away_goals_for': 'goals_away', 'away_rest_days': 'away_rest'})

    final = pd.merge(home, away, on='game_id')
    # final['rest_diff'] = final['home_rest'] - final['away_rest']
    # STED (Special Teams Efficiency Differential): Matchup-specific ST advantage
    # home_sted > 0  →  home team has the ST edge
    # Term 1: home PP xG60 vs away PK xGA60 (positive = home PP beats away PK)
    # Term 2: away PP xG60 vs home PK xGA60 (positive = away has PP edge, so SUBTRACT for home)
    if 'home_roll_pp_xg60' in final.columns and 'away_roll_pk_xga60' in final.columns:
        final['home_sted'] = (
            (final['home_roll_pp_xg60'] - final['away_roll_pk_xga60']) -
            (final['away_roll_pp_xg60'] - final['home_roll_pk_xga60'])
        )
        final['away_sted'] = -final['home_sted']
    else:
        final['home_sted'] = 0.0
        final['away_sted'] = 0.0

    # OSA_xG (Opposition-Adjusted xG): Adjust raw xG by opponent defensive quality
    if 'home_roll_xgf' in final.columns and 'away_opp_xg_suppression' in final.columns:
        # Home team's xG adjusted by away team's defensive strength
        final['home_osa_xg'] = final['home_roll_xgf'] * final['away_opp_xg_suppression']
        # Away team's xG adjusted by home team's defensive strength
        final['away_osa_xg'] = final['away_roll_xgf'] * final['home_opp_xg_suppression']
    else:
        final['home_osa_xg'] = final.get('home_roll_xgf', 0.0)
        final['away_osa_xg'] = final.get('away_roll_xgf', 0.0)

    # GOALIE QUALITY DIFFERENTIAL: Direct matchup comparison (boosted signal)
    # These features capture the goalie matchup advantage directly
    GOALIE_BOOST_FACTOR = 2.0  # Amplify goalie signal relative to other features

    if 'home_goalie_roll_gsax' in final.columns and 'away_goalie_roll_gsax' in final.columns:
        # GSAx differential: positive = home goalie is better
        final['goalie_gsax_diff'] = (final['home_goalie_roll_gsax'] - final['away_goalie_roll_gsax']) * GOALIE_BOOST_FACTOR
        # HD GSAx differential: high-danger save quality comparison
        final['goalie_hd_gsax_diff'] = (final['home_goalie_roll_hd_gsax'] - final['away_goalie_roll_hd_gsax']) * GOALIE_BOOST_FACTOR
        # Combined goalie quality score (weighted average of metrics)
        final['home_goalie_quality'] = (
            final['home_goalie_roll_gsax'] * 0.4 +
            final['home_goalie_roll_hd_gsax'] * 0.4 +
            (1.0 - final['home_goalie_roll_rcr']) * 0.2  # Lower RCR = more consistent = better
        ) * GOALIE_BOOST_FACTOR
        final['away_goalie_quality'] = (
            final['away_goalie_roll_gsax'] * 0.4 +
            final['away_goalie_roll_hd_gsax'] * 0.4 +
            (1.0 - final['away_goalie_roll_rcr']) * 0.2
        ) * GOALIE_BOOST_FACTOR
    else:
        final['goalie_gsax_diff'] = 0.0
        final['goalie_hd_gsax_diff'] = 0.0
        final['home_goalie_quality'] = 0.0
        final['away_goalie_quality'] = 0.0

    # ROSTER RAPM MATCHUP: lineup-aggregated defensive RAPM is the validated signal.
    # d_rapm is lower=better defense; a team's GOALS are driven by the OPPONENT's
    # lineup defense, so expose each side's offense-vs-opponent-defense matchup.
    if 'home_roster_drapm' in final.columns and 'away_roster_drapm' in final.columns:
        for c in ['home_roster_drapm', 'away_roster_drapm', 'home_roster_orapm', 'away_roster_orapm']:
            if c in final.columns:
                final[c] = final[c].fillna(0.0)
        # home attack faces away defense; away attack faces home defense
        final['home_roster_off_vs_def'] = final['home_roster_orapm'] - final['away_roster_drapm']
        final['away_roster_off_vs_def'] = final['away_roster_orapm'] - final['home_roster_drapm']
        # net lineup-defense edge (positive = home has the stronger defensive lineup)
        final['roster_drapm_diff'] = final['away_roster_drapm'] - final['home_roster_drapm']
    else:
        for c in ['home_roster_drapm', 'away_roster_drapm', 'home_roster_orapm', 'away_roster_orapm',
                  'home_roster_off_vs_def', 'away_roster_off_vs_def', 'roster_drapm_diff']:
            final[c] = 0.0

    # LEAGUE-WIDE HOME ICE TREND: Rolling home win % across entire league
    # Captures macro trend (e.g., 2024-25 season's depressed ~42.5% home win rate)
    # Sort by date to compute chronological rolling average
    if 'home_mp_game_date' in final.columns:
        final = final.sort_values('home_mp_game_date')
    final['home_won'] = (final['goals_home'] > final['goals_away']).astype(float)
    # Use 300-game window (~18-20 days of NHL) - responsive but stable
    final['league_home_win_pct'] = final['home_won'].shift(1).rolling(300, min_periods=30).mean()
    final['league_home_win_pct'] = final['league_home_win_pct'].fillna(0.50)  # Historical neutral default
    final = final.drop(columns=['home_won'])

    # Keep home_mp_game_date for forecasting (renamed from mp_game_date during home_ prefix)
    drop = [c for c in final.columns if any(x in c for x in ['side', 'prev_date', 'team_id_x', 'team_id_y', 'away_mp_game_date'])]
    final = final.drop(columns=drop, errors='ignore')

    # Rename home_mp_game_date back to mp_game_date for consistency
    if 'home_mp_game_date' in final.columns:
        final = final.rename(columns={'home_mp_game_date': 'mp_game_date'})

    return final.dropna(subset=['goals_home', 'goals_away']).reset_index(drop=True)


def prepare_training_data(db_path, use_complete_games_filter=True):
    """
    Prepare training data with optional complete games filtering.
    Set use_complete_games_filter=False to use all games (old behavior).
    """
    df = get_base_team_stats(db_path, use_complete_games_filter=use_complete_games_filter)

    # TARGET GUARD: drop games with a degenerate / missing-data target so they can
    # never train the model, regardless of the complete-games filter.
    #  - An NHL game can never end 0-0 (every game has a winner), so a 0-0 final is
    #    the signature of missing MoneyPuck goals COALESCE'd to 0.
    #  - Every real game has expected goals > 0; xgf <= 0 on either side means the
    #    MoneyPuck team row was absent and goals_for was zero-filled.
    n_before = len(df)
    bad_target = (
        ((df['goals_home'] == 0) & (df['goals_away'] == 0)) |
        (df.get('home_xgf', 1.0) <= 0) |
        (df.get('away_xgf', 1.0) <= 0)
    )
    n_bad = int(bad_target.sum())
    if n_bad:
        print(f"⚠  Target guard: dropping {n_bad} game(s) with degenerate/missing targets "
              f"(0-0 finals or missing MoneyPuck data) out of {n_before}.")
        df = df[~bad_target].reset_index(drop=True)

    # Validate data quality
    is_valid = validate_training_data(df, verbose=True)

    if not is_valid:
        print("\n⚠️  WARNING: Data quality issues detected!")
        print("   Consider enabling use_complete_games_filter=True\n")
        exit(1)

    return df


# ---------------------------
# Safe Standardisation + Model
# ---------------------------
def standardize_data(df, cols, path, mode):
    data = df[cols].copy().astype(np.float32)
    if mode == 'train':
        stats = {}
        for c in cols:
            mu = data[c].mean()
            sd = data[c].std()
            if sd < 1e-6:
                sd = 1.0
            stats[c] = [mu, sd]
            data[c] = (data[c] - mu) / sd
        np.savez(path, **stats)
    else:
        loaded = np.load(path, allow_pickle=True)
        for c in cols:
            mu, sd = loaded[c] if c in loaded else (0.0, 1.0)
            data[c] = (data[c] - mu) / max(sd, 1e-6)
    return data


def init_params(key, d_in, hidden):
    k1, k2, k3 = jax.random.split(key, 3)
    W1 = jax.random.normal(k1, (d_in, hidden)) * jnp.sqrt(2.0 / d_in)
    b1 = jnp.zeros(hidden)
    W2 = jax.random.normal(k2, (hidden, hidden)) * jnp.sqrt(2.0 / hidden)
    b2 = jnp.zeros(hidden)
    W3 = jax.random.normal(k3, (hidden, 2)) * 0.05
    b3 = jnp.full(2, 2.5)

    params = {'W1': W1, 'b1': b1, 'W2': W2, 'b2': b2, 'W3': W3, 'b3': b3}

    return params


def forward(p, x, training=False, rng_key=None, dropout_rate=0.2):
    """
    Forward pass through the network with optional dropout.

    Args:
        p: Parameters dict
        x: Input features
        training: Boolean - if True, applies dropout
        rng_key: JAX random key (required if training=True and dropout_rate > 0)
        dropout_rate: Dropout probability (default 0.2 = 20%)
    """
    h = jax.nn.elu(x @ p['W1'] + p['b1'])

    # Apply dropout to first hidden layer
    if training and dropout_rate > 0.0 and rng_key is not None:
        key1, key2 = jax.random.split(rng_key)
        keep_prob = 1.0 - dropout_rate
        mask1 = jax.random.bernoulli(key1, keep_prob, h.shape)
        h = jnp.where(mask1, h / keep_prob, 0.0)
    else:
        key2 = rng_key

    h = jax.nn.elu(h @ p['W2'] + p['b2'])

    # Apply dropout to second hidden layer
    if training and dropout_rate > 0.0 and key2 is not None:
        keep_prob = 1.0 - dropout_rate
        mask2 = jax.random.bernoulli(key2, keep_prob, h.shape)
        h = jnp.where(mask2, h / keep_prob, 0.0)

    return jax.nn.softplus(h @ p['W3'] + p['b3']) + 1e-6


def loss_fn(p, x, y, training=False, rng_key=None, dropout_rate=0.2, weights=None):
    """Loss function with optional dropout and per-sample time weighting."""
    lam = forward(p, x, training=training, rng_key=rng_key, dropout_rate=dropout_rate)
    lam = jnp.clip(lam, 0.5, 5.0)
    l2_reg = jnp.sum(p['W1'] ** 2) + jnp.sum(p['W2'] ** 2) + jnp.sum(p['W3'] ** 2)
    per_sample = jnp.mean(lam - y * jnp.log(lam), axis=1)  # per-game loss (avg over home/away)
    if weights is not None:
        return jnp.sum(per_sample * weights) / jnp.sum(weights) + 2e-5 * l2_reg
    return jnp.mean(per_sample) + 2e-5 * l2_reg


def adam_update(params, grads, adam_state, lr, beta1=0.9, beta2=0.999, eps=1e-8):
    """Adam optimizer update step."""
    t = adam_state['t'] + 1
    m = adam_state['m']
    v = adam_state['v']

    new_m = {}
    new_v = {}
    new_params = {}

    for key in params:
        # Update biased first moment estimate
        new_m[key] = beta1 * m[key] + (1 - beta1) * grads[key]

        # Update biased second moment estimate
        new_v[key] = beta2 * v[key] + (1 - beta2) * (grads[key] ** 2)

        # Bias correction
        m_hat = new_m[key] / (1 - beta1 ** t)
        v_hat = new_v[key] / (1 - beta2 ** t)

        # Update parameters
        new_params[key] = params[key] - lr * m_hat / (jnp.sqrt(v_hat) + eps)

    new_adam_state = {'m': new_m, 'v': new_v, 't': t}

    return new_params, new_adam_state


def cosine_decay_schedule(epoch, total_epochs, lr_max, lr_min=1e-6, warmup_epochs=10):
    """
    Cosine annealing with warmup.
    - Warmup: Linear increase from lr_min to lr_max over warmup_epochs
    - Decay: Cosine decay from lr_max to lr_min over remaining epochs
    """
    if epoch < warmup_epochs:
        # Linear warmup
        return lr_min + (lr_max - lr_min) * (epoch / warmup_epochs)
    else:
        # Cosine decay
        progress = (epoch - warmup_epochs) / (total_epochs - warmup_epochs)
        return lr_min + 0.5 * (lr_max - lr_min) * (1 + jnp.cos(jnp.pi * progress))


def update_step(p, x, y, lr, rng_key, dropout_rate, w=None):
    """Single training step with dropout (vanilla SGD) and optional sample weights."""
    loss_and_grad = jax.value_and_grad(lambda params: loss_fn(params, x, y, training=True, rng_key=rng_key, dropout_rate=dropout_rate, weights=w))
    loss, grads = loss_and_grad(p)
    new_params = {k: p[k] - lr * grads[k] for k in p}
    return new_params, loss


# JIT compile the update step for speed
update_step = jax.jit(update_step, static_argnums=(5,))  # static_argnums for dropout_rate


def update_step_adam(params, adam_state, x, y, lr, rng_key, dropout_rate, w=None, beta1=0.9, beta2=0.999):
    """Single training step with Adam optimizer and optional sample weights."""
    # Compute loss and gradients with dropout enabled
    loss_and_grad = jax.value_and_grad(
        lambda p: loss_fn(p, x, y, training=True, rng_key=rng_key, dropout_rate=dropout_rate, weights=w)
    )
    loss, grads = loss_and_grad(params)

    # Adam update
    new_params, new_adam_state = adam_update(params, grads, adam_state, lr, beta1, beta2)

    return new_params, new_adam_state, loss


# JIT compile Adam update step (note: adam_state is now part of the function signature)
update_step_adam = jax.jit(update_step_adam, static_argnums=(6,))  # static for dropout_rate


def get_features(df):
    # Meta columns to always exclude
    exclude_meta = ['game_id', 'goals_home', 'goals_away', 'h_odd', 'a_odd', 'mp_game_date',
               'home_ghsf', 'away_ghsf', 'home_lss', 'away_lss',
               #'home_goalie_player_id', 'away_goalie_player_id', 'primary_goalie_id'
    ]

    # FEATURE REDUCTION: Skip redundant rolling windows to reduce from 152 to ~80 features
    # Keep roll_ (10-game) as primary, keep roll3_ only for key trend features
    # Drop roll20_ entirely (highly correlated with roll_)
    redundant_roll20_bases = ['xgf', 'xga', 'pens', 'goals_for', 'rush_attempts_for',
                              'avg_dist', 'avg_angle', 'sh_pct', 'sa_corsi_pct', 'hd_save_pct']

    # Also drop roll3 for less important features (keep only for key momentum indicators)
    skip_roll3_bases = ['pens', 'rush_attempts_for', 'avg_dist', 'avg_angle']

    features = []
    for c in df.columns:
        if c in exclude_meta:
            continue

        # Strip prefix to check the base feature nature
        base_c = c.replace('home_', '').replace('away_', '')

        # 1. KEEP Rolling averages (Historical data) with REDUCTION
        # EXCEPTION: Exclude zone-specific EDGE features in favor of consolidated versions
        if base_c.startswith('roll'):
            # Skip zone-specific edge features (e.g., roll3_edge_giveaway_d, roll10_edge_giveaway_n)
            # These are replaced by consolidated features: roll_edge_giveaway_total, roll_edge_giveaway_dzone_pct
            zone_specific_patterns = ['edge_giveaway_d', 'edge_giveaway_n', 'edge_giveaway_o',
                                       'edge_blocked_shot_d', 'edge_hit_', 'edge_takeaway_', 'edge_missed_shot_']
            is_zone_specific = any(pattern in base_c for pattern in zone_specific_patterns)

            # Keep consolidated edge features
            is_consolidated_edge = any(pattern in base_c for pattern in
                                       ['edge_giveaway_total', 'edge_giveaway_dzone_pct', 'edge_dzone_blocks'])

            if is_zone_specific and not is_consolidated_edge:
                continue  # Skip zone-specific, use consolidated instead

            # FEATURE REDUCTION: Skip roll20_ for redundant features
            if base_c.startswith('roll20_'):
                base_metric = base_c.replace('roll20_', '')
                if base_metric in redundant_roll20_bases:
                    continue  # Skip this redundant feature

            # FEATURE REDUCTION: Skip roll3_ for less important features
            if base_c.startswith('roll3_'):
                base_metric = base_c.replace('roll3_', '')
                if base_metric in skip_roll3_bases:
                    continue  # Skip this redundant feature

            features.append(c)
            continue

        # 1b. KEEP Goalie Rolling averages & Metrics
        if base_c.startswith('goalie_roll') or base_c == 'goalie_ghsf':
            features.append(c)
            continue

        # 2. KEEP Computed Historical Metrics
        # - rest: derived from schedule (known pre-game)
        # - ice: home ice advantage indicator (always known pre-game)
        # - osa_xg: derived from rolling xG * rolling suppression (known pre-game)
        # - hdsm: derived from roll3 - roll10 (known pre-game)
        # - opp_xg_suppression: derived from rolling xGA (known pre-game)
        # - sted: derived from rolling special teams (known pre-game)
        # - goalie_gsax_diff, goalie_hd_gsax_diff: goalie matchup differentials (known pre-game)
        # - goalie_quality: composite goalie quality score (known pre-game)
        # 2a. KEEP Home Ice Features (known pre-game)
        # Note: base_c strips ALL 'home_'/'away_' occurrences, so
        # 'home_home_ice_xg_boost' -> 'ice_xg_boost'
        if base_c in ['ice_xg_boost', 'ice_win_boost']:
            features.append(c)
            continue

        # 2b. KEEP League-wide home ice trend (game-level, known pre-game)
        if c == 'league_home_win_pct':
            features.append(c)
            continue

        # 2c. KEEP roster-aggregated RAPM matchup (game-level diff, known pre-game)
        if c == 'roster_drapm_diff':
            features.append(c)
            continue

        if base_c in (['rest', 'osa_xg', 'hdsm', 'opp_xg_suppression', 'sted',
                      'goalie_gsax_diff', 'goalie_hd_gsax_diff', 'goalie_quality',
                      # roster-aware lineup RAPM (D-RAPM validated; O-RAPM low-signal but kept)
                      'roster_drapm', 'roster_orapm', 'roster_off_vs_def']
                     + (['proj_xgf60', 'proj_xga60', 'proj_cf_pct', 'proj_xg_pct'] if PLAYER_PROJ else [])
                     + (['lineup_continuity'] if LINEUP_OVERRIDE else [])):
            features.append(c)
            continue

        # 3. EXCLUDE Everything else (Raw Game Stats)
        # This drops: xgf, xga, pens, goals_for, shots_total, avg_dist, avg_angle,
        # cnt_*, edge_* (raw), rush_attempts_for, etc.
        # These are "Post-Game" stats and constitute data leakage if used for prediction.
        pass

    return features


def get_features_pruned(df):
    """
    PRUNED FEATURE SET - ~40 features based on correlation/importance analysis.
    
    Keeps only high-signal features:
    - Top correlated: OSA_xG, baseline xGF/xGA, PP/PK
    - Top RF importance: roll20_sa_corsi_pct, flurry_delta, hdcf_share
    - Goalie metrics: GSAx, HD_GSAx, RCR
    - Context: rest days
    
    Removes:
    - Redundant rolling windows (keep only ONE per metric)
    - Broken features (GHSF, LSS, STED)
    - Low-signal edge/zone features
    - Features with <0.03 correlation
    """
    
    # Curated feature list based on analysis
    PRUNED_FEATURES = [
        # === CORE xG FEATURES (highest correlation) ===
        'home_roll_xgf', 'away_roll_xgf',           # Baseline offensive xG
        'home_roll_xga', 'away_roll_xga',           # Baseline defensive xG
        'home_osa_xg', 'away_osa_xg',               # Opposition-adjusted xG (r=0.337!)
        
        # === POSSESSION (top RF importance) ===
        'home_roll20_sa_corsi_pct', 'away_roll20_sa_corsi_pct',  # Long-term possession (0.128 importance)
        'home_roll_hdcf_share', 'away_roll_hdcf_share',          # High danger chance share
        
        # === SPECIAL TEAMS (strong correlation) ===
        'home_roll_pp_xg60', 'away_roll_pp_xg60',       # Power play efficiency
        'home_roll_pk_xga60', 'away_roll_pk_xga60',     # Penalty kill
        'home_roll_pp_efficiency', 'away_roll_pp_efficiency',

        # === SPECIAL TEAMS MATCHUP ===
        'home_sted', 'away_sted',                        # ST efficiency differential (home PP edge - away PP edge)

        # === SHOOTING/FINISHING ===
        'home_roll_sh_pct', 'away_roll_sh_pct',         # Shooting percentage
        'home_roll_hd_finish_pct', 'away_roll_hd_finish_pct',  # HD finishing
        'home_roll_hd_save_pct', 'away_roll_hd_save_pct',      # HD save pct
        
        # === FLURRY/PRESSURE (moderate importance) ===
        'home_roll_flurry_delta', 'away_roll_flurry_delta',
        'home_roll_pressure_rate', 'away_roll_pressure_rate',
        
        # === GOALIE FEATURES ===
        'home_goalie_roll_gsax', 'away_goalie_roll_gsax',       # Goals saved above expected
        'home_goalie_roll_hd_gsax', 'away_goalie_roll_hd_gsax', # HD saves above expected
        'home_goalie_roll_rcr', 'away_goalie_roll_rcr',         # Rebound control
        'goalie_gsax_diff', 'goalie_hd_gsax_diff',              # Goalie matchup differentials
        'home_goalie_quality', 'away_goalie_quality',          # Composite goalie score
        
        # === CONTEXT ===
        'home_rest', 'away_rest',                      # Rest days
        
        # === CHEMISTRY (moderate importance) ===
        'home_roll_linemate_xgf_boost', 'away_roll_linemate_xgf_boost',
        'home_roll_dpair_xgf_boost', 'away_roll_dpair_xgf_boost',

        # === LINEMATE SYNERGY ===
        'home_roll_lss', 'away_roll_lss',                # Rolling linemate synergy score (10-game)

        # === WIN RATE MOMENTUM (strong correlation r=0.15) ===
        'home_roll5_win_rate', 'away_roll5_win_rate',     # 5-game rolling win rate (best window)
        'home_roll10_win_rate', 'away_roll10_win_rate',   # 10-game rolling win rate

        # === HOME ICE CALIBRATION ===
        'home_home_ice_xg_boost', 'away_home_ice_xg_boost',   # Per-team home/away xG differential
        'home_home_ice_win_boost', 'away_home_ice_win_boost',  # Per-team home/away win rate differential
        'league_home_win_pct',                                  # League-wide rolling home win %
    ]
    
    # Filter to only features that exist in the dataframe
    available = [f for f in PRUNED_FEATURES if f in df.columns]
    
    missing = [f for f in PRUNED_FEATURES if f not in df.columns]
    if missing:
        print(f"Note: {len(missing)} pruned features not found in data: {missing[:5]}...")
    
    return available


# ---------------------------
# Odds Backtesting
# ---------------------------
def load_odds_data():
    """Load all moneypuck_odds CSV files and return a DataFrame keyed by traditional_game_id."""
    import glob as glob_mod
    csv_files = glob_mod.glob('moneypuck_odds_*.csv')
    csv_files = [f for f in csv_files if not f.endswith('_all_playoffs.csv')]

    if not csv_files:
        return pd.DataFrame()

    dfs = []
    for f in csv_files:
        try:
            dfs.append(pd.read_csv(f))
        except Exception as e:
            print(f"  Warning: Could not load {f}: {e}")

    if not dfs:
        return pd.DataFrame()

    odds_df = pd.concat(dfs, ignore_index=True)
    odds_df['traditional_game_id'] = odds_df['traditional_game_id'].astype(str)
    return odds_df


def odds_backtest(val_game_ids, val_pred_home, val_pred_away, val_actual_home, val_actual_away, label=""):
    """
    Backtest model predictions against sportsbook closing lines.
    Uses Pinnacle as primary (sharpest line), FanDuel as fallback.
    Simulates flat $100 bets where model disagrees with market.
    """
    tag = f" ({label})" if label else ""
    print(f"\n{'=' * 60}")
    print(f"ODDS BACKTESTING: Model vs Market{tag}")
    print('=' * 60)

    odds_df = load_odds_data()
    if odds_df.empty:
        print("  No odds CSV files found. Skipping backtest.")
        print('=' * 60)
        return

    # Match val games to odds data
    val_ids_str = [str(gid) for gid in val_game_ids]
    matched = odds_df[odds_df['traditional_game_id'].isin(val_ids_str)].copy()

    if matched.empty:
        print("  No val games matched to odds data. Skipping backtest.")
        print('=' * 60)
        return

    # Deduplicate (keep first occurrence per game)
    matched = matched.drop_duplicates('traditional_game_id', keep='first')

    # Pick best available closing odds: Pinnacle > FanDuel > DraftKings
    def get_closing_odds(row):
        for book in ['pinnacle', 'fanduel', 'draftkings']:
            away_col = f'{book}_closing_away_odds'
            home_col = f'{book}_closing_home_odds'
            if away_col in row.index and home_col in row.index:
                a, h = row[away_col], row[home_col]
                try:
                    a, h = float(a), float(h)
                    if not (np.isnan(a) or np.isnan(h)):
                        return pd.Series({'close_away': a, 'close_home': h, 'book': book})
                except (ValueError, TypeError):
                    continue
        return pd.Series({'close_away': np.nan, 'close_home': np.nan, 'book': None})

    closing = matched.apply(get_closing_odds, axis=1)
    matched = pd.concat([matched, closing], axis=1)
    matched = matched.dropna(subset=['close_away', 'close_home'])

    if matched.empty:
        print("  No valid closing odds found for val games. Skipping.")
        print('=' * 60)
        return

    # Build lookup: game_id -> index in val arrays
    id_to_idx = {str(gid): i for i, gid in enumerate(val_game_ids)}
    matched['val_idx'] = matched['traditional_game_id'].map(id_to_idx)
    matched = matched.dropna(subset=['val_idx'])
    matched['val_idx'] = matched['val_idx'].astype(int)

    n_matched = len(matched)
    book_counts = matched['book'].value_counts()
    print(f"  Matched {n_matched}/{len(val_game_ids)} val games to closing odds")
    for book, count in book_counts.items():
        print(f"    {book}: {count} games")

    # Convert American odds to implied probability (no-vig)
    def american_to_implied(odds):
        if odds > 0:
            return 100.0 / (odds + 100.0)
        else:
            return abs(odds) / (abs(odds) + 100.0)

    # American odds to decimal (for payout calculation)
    def american_to_decimal(odds):
        if odds > 0:
            return 1.0 + odds / 100.0
        else:
            return 1.0 + 100.0 / abs(odds)

    matched['market_home_prob'] = matched['close_home'].apply(american_to_implied)
    matched['market_away_prob'] = matched['close_away'].apply(american_to_implied)
    # Remove vig: normalize to sum to 1
    total_prob = matched['market_home_prob'] + matched['market_away_prob']
    matched['market_home_prob'] /= total_prob
    matched['market_away_prob'] /= total_prob

    matched['decimal_home'] = matched['close_home'].apply(american_to_decimal)
    matched['decimal_away'] = matched['close_away'].apply(american_to_decimal)

    # Model implied win probabilities from Poisson rates
    # P(home wins) approx from lambda comparison (simple: higher lambda = higher win prob)
    idxs = matched['val_idx'].values
    m_home = val_pred_home[idxs]
    m_away = val_pred_away[idxs]
    act_home = val_actual_home[idxs]
    act_away = val_actual_away[idxs]

    # Poisson win probability: P(X>Y) where X~Pois(lh), Y~Pois(la)
    from scipy.stats import poisson
    max_goals = 12
    model_home_win_prob = np.zeros(n_matched)
    model_away_win_prob = np.zeros(n_matched)
    for i in range(n_matched):
        lh, la = m_home[i], m_away[i]
        h_pmf = poisson.pmf(np.arange(max_goals), lh)
        a_pmf = poisson.pmf(np.arange(max_goals), la)
        # Joint probability grid
        grid = np.outer(h_pmf, a_pmf)
        p_home_win = np.sum(np.tril(grid, -1))  # below diagonal = home > away
        p_away_win = np.sum(np.triu(grid, 1))   # above diagonal = away > home
        # Normalize (exclude ties for moneyline)
        total = p_home_win + p_away_win
        model_home_win_prob[i] = p_home_win / total if total > 0 else 0.5
        model_away_win_prob[i] = p_away_win / total if total > 0 else 0.5

    matched['model_home_prob'] = model_home_win_prob
    matched['model_away_prob'] = model_away_win_prob

    # Actual outcomes
    actual_home_won = act_home > act_away
    actual_away_won = act_away > act_home
    actual_tie = act_home == act_away  # regulation tie

    # --- Model pick accuracy (on games with odds) ---
    model_picks_home = model_home_win_prob > 0.5
    decided = ~actual_tie
    if decided.sum() > 0:
        correct = (model_picks_home[decided] == actual_home_won[decided]).sum()
        print(f"\n  Model pick accuracy (odds-matched games): {correct}/{decided.sum()} = {correct/decided.sum()*100:.1f}%")

    # --- Market pick accuracy ---
    market_picks_home = matched['market_home_prob'].values > 0.5
    if decided.sum() > 0:
        mkt_correct = (market_picks_home[decided] == actual_home_won[decided]).sum()
        print(f"  Market pick accuracy (odds-matched games): {mkt_correct}/{decided.sum()} = {mkt_correct/decided.sum()*100:.1f}%")

    # --- MoneyPuck pick accuracy ---
    mp_home_prob = matched['mp_home_win_prob'].apply(lambda x: float(str(x).replace('%', '')) / 100.0 if pd.notna(x) else np.nan)
    mp_valid = mp_home_prob.notna().values & decided
    if mp_valid.sum() > 0:
        mp_picks_home = mp_home_prob.values[mp_valid] > 0.5
        mp_correct = (mp_picks_home == actual_home_won[mp_valid]).sum()
        print(f"  MoneyPuck pick accuracy (odds-matched games): {mp_correct}/{mp_valid.sum()} = {mp_correct/mp_valid.sum()*100:.1f}%")

    # --- Flat bet simulation: bet $100 on model's pick at closing line ---
    STAKE = 100.0
    total_profit = 0.0
    n_bets = 0
    wins = 0
    losses = 0

    # Edge-filtered bets: only bet when model edge > threshold
    EDGE_THRESHOLD = 0.03  # 3% edge over market
    edge_profit = 0.0
    edge_bets = 0
    edge_wins = 0

    for i in range(n_matched):
        if actual_tie[i]:
            continue  # Skip ties (push)

        home_won = actual_home_won[i]
        m_prob_home = model_home_win_prob[i]
        mkt_prob_home = matched['market_home_prob'].iloc[i]
        dec_home = matched['decimal_home'].iloc[i]
        dec_away = matched['decimal_away'].iloc[i]

        # Model picks home
        if m_prob_home > 0.5:
            bet_won = home_won
            payout = STAKE * dec_home if bet_won else 0.0
        else:
            bet_won = not home_won
            payout = STAKE * dec_away if bet_won else 0.0

        profit = payout - STAKE
        total_profit += profit
        n_bets += 1
        if bet_won:
            wins += 1
        else:
            losses += 1

        # Edge-filtered bet
        edge_home = m_prob_home - mkt_prob_home
        edge_away = (1.0 - m_prob_home) - (1.0 - mkt_prob_home)
        max_edge = max(edge_home, edge_away)

        if max_edge >= EDGE_THRESHOLD:
            edge_bets += 1
            # Bet on side with edge
            if edge_home > edge_away:
                e_won = home_won
                e_payout = STAKE * dec_home if e_won else 0.0
            else:
                e_won = not home_won
                e_payout = STAKE * dec_away if e_won else 0.0
            edge_profit += e_payout - STAKE
            if e_won:
                edge_wins += 1

    print(f"\n  --- Flat Bet Simulation ($100/game on model's pick) ---")
    if n_bets > 0:
        roi = total_profit / (n_bets * STAKE) * 100
        print(f"  Total bets: {n_bets} | W-L: {wins}-{losses} ({wins/n_bets*100:.1f}%)")
        print(f"  Total profit: ${total_profit:+.2f} | ROI: {roi:+.2f}%")

    print(f"\n  --- Edge-Filtered Bets (>{EDGE_THRESHOLD*100:.0f}% edge over market) ---")
    if edge_bets > 0:
        e_roi = edge_profit / (edge_bets * STAKE) * 100
        print(f"  Total bets: {edge_bets} | W: {edge_wins} ({edge_wins/edge_bets*100:.1f}%)")
        print(f"  Total profit: ${edge_profit:+.2f} | ROI: {e_roi:+.2f}%")
    else:
        print(f"  No bets met the {EDGE_THRESHOLD*100:.0f}% edge threshold.")

    print('=' * 60)


# ---------------------------
# Train & Forecast
# ---------------------------
def train(db, epochs, batch, lr, hidden, seed, use_complete_games_filter=True, use_pruned_features=False, use_adam=False, val_start=None, val_end=None, dump_preds=None):
    print("preparing training data...")
    print(f"game filtering: {'ENABLED' if use_complete_games_filter else 'DISABLED'}")
    print(f"feature set: {'PRUNED' if use_pruned_features else 'FULL'} (exact count printed at training start)")
    print(f"optimizer: {'ADAM' if use_adam else 'SGD'}\n")
    df = prepare_training_data(db, use_complete_games_filter=use_complete_games_filter)
    if df.empty:
        print("No data!")
        return
    
    if seed is None: seed = random.randint(0, 2**32);
    print(f"[RANDOM_SEED: {seed}]")
    print("beginning training...\n\n")
    
    feats = get_features_pruned(df) if use_pruned_features else get_features(df)
    print("using features: "); print(feats);

    # Temporal Validation Split: train on older games, validate on most recent
    # Sort by date so split is chronological (no future leakage)
    df = df.sort_values('mp_game_date').reset_index(drop=True)
    if val_start is not None:
        # Walk-forward fold: train = games before val_start; val = [val_start, val_end);
        # games on/after val_end are FUTURE and dropped entirely (no leakage).
        vs = pd.to_datetime(val_start)
        ve = pd.to_datetime(val_end) if val_end else (df['mp_game_date'].max() + pd.Timedelta(days=1))
        df = df[df['mp_game_date'] < ve].reset_index(drop=True)
        train_size = int((df['mp_game_date'] < vs).sum())
        if train_size < 100 or train_size >= len(df):
            print(f"⚠  walk-forward fold has too few train/val games (train={train_size}, total={len(df)}); skipping")
            return
        val_size = len(df) - train_size
    else:
        val_size = int(len(df) * 0.08)
        train_size = len(df) - val_size

    if os.environ.get('NHL_BETTIME') in ('L', 'LG'):   # EXPERIMENT 2026-10
        global _BETTIME
        _BETTIME = os.environ['NHL_BETTIME']
        try:
            dfb = prepare_training_data(db, use_complete_games_filter=use_complete_games_filter).set_index('game_id')
        finally:
            _BETTIME = None
        vids = df.loc[train_size:, 'game_id'].values
        missing = set(vids) - set(dfb.index)
        assert not missing, f"bet-time frame lacks {len(missing)} validation games"
        before = df.loc[train_size:, feats].values.copy()
        df.loc[train_size:, feats] = dfb.loc[vids, feats].values
        changed = (np.abs(df.loc[train_size:, feats].values - before) > 1e-12).any(axis=0)
        print(f"[BET-TIME {os.environ['NHL_BETTIME']}] validation rows rebuilt from pre-game information; "
              f"{int(changed.sum())} feature columns differ from actual-lineup values")

    split_date = df.iloc[train_size]['mp_game_date']
    print(f"\nTemporal split: train up to {df.iloc[train_size - 1]['mp_game_date'].date()} | validate from {split_date.date()}")

    X_df = standardize_data(df, feats, STATS_PATH, 'train')
    X_all = jnp.array(X_df.values)
    Y_all = jnp.array(df[['goals_home', 'goals_away']].values)

    X, X_val = X_all[:train_size], X_all[train_size:]
    Y, Y_val = Y_all[:train_size], Y_all[train_size:]

    # Dixon-Coles time weighting: exponential decay so recent games matter more
    # xi (decay rate): higher = more aggressive recency bias
    # xi=0.002 gives a half-life of ~347 days (~1 full season)
    # A game from 1 year ago gets weight ~0.48, 2 years ago ~0.23
    TIME_DECAY_XI = 0.002
    train_dates = df.iloc[:train_size]['mp_game_date']
    max_train_date = train_dates.max()
    days_ago = (max_train_date - train_dates).dt.days.values.astype(np.float32)
    time_weights = np.exp(-TIME_DECAY_XI * days_ago)
    # Normalize so weights sum to len(train) (preserves effective learning rate)
    time_weights = time_weights * len(time_weights) / time_weights.sum()
    W_train = jnp.array(time_weights)

    half_life = np.log(2) / TIME_DECAY_XI
    oldest_weight = float(time_weights.min())
    newest_weight = float(time_weights.max())
    print(f"\nTime weighting: xi={TIME_DECAY_XI}, half-life={half_life:.0f} days")
    print(f"  Newest game weight: {newest_weight:.2f} | Oldest game weight: {oldest_weight:.2f} | Ratio: {newest_weight/oldest_weight:.1f}x")

    key = jax.random.PRNGKey(seed)

    steps = max(1, len(X) // batch)
    print(f"\nTraining on {len(X)} games | Validation on {len(X_val)} games | {len(feats)} features")

    params = init_params(key, len(feats), hidden)

    # Adam optimizer state (only used when use_adam=True)
    adam_state = {
        'm': {k: jnp.zeros_like(v) for k, v in params.items()},
        'v': {k: jnp.zeros_like(v) for k, v in params.items()},
        't': 0,
    }

    best_val_loss = float('inf')
    best_params = None
    patience_counter = 0

    # Default dropout rate
    dropout_rate = 0.3

    for e in range(epochs):
        # Calculate learning rate for this epoch
        current_lr = cosine_decay_schedule(e, epochs, lr_max=lr, lr_min=1e-6, warmup_epochs=10)

        # Generate new random key for this epoch
        key, subkey = jax.random.split(key)
        # Permute INDICES each epoch and gather batches from the original arrays.
        # Do NOT mutate X/Y/W_train: in-place shuffling accumulates across epochs,
        # which (a) desyncs the time weights from X/Y after the first epoch and
        # (b) leaves post-training train_predictions misaligned with train_game_ids.
        perm = jax.random.permutation(subkey, len(X))
        loss_sum = 0.0

        # Train Loop
        for i in range(steps):
            idx = perm[i * batch:(i + 1) * batch]
            xb = X[idx]
            yb = Y[idx]
            wb = W_train[idx]

            # Generate unique random key for dropout in this batch
            key, dropout_key = jax.random.split(key)
            if use_adam:
                params, adam_state, l = update_step_adam(params, adam_state, xb, yb, current_lr, dropout_key, dropout_rate, wb)
            else:
                params, l = update_step(params, xb, yb, current_lr, dropout_key, dropout_rate, wb)
            loss_sum += l

        # Validation & Logging
        if e % 10 == 0 or e == epochs - 1:
            # Full validation pass WITHOUT dropout (training=False)
            val_loss = loss_fn(params, X_val, Y_val, training=False, rng_key=None, dropout_rate=0.0)
            train_loss = loss_sum / steps

            improved = ""
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                best_params = params
                improved = "*" # Indicator of new best model
                patience_counter = 0
            else:
                patience_counter += 1

            print(f"Epoch {e:3d} | LR {current_lr:.6f} | Train Loss {train_loss:.4f} | Val Loss {val_loss:.4f} {improved}")

            # Early Stopping: Stop if no improvement for 100 epochs
            if patience_counter > 100:
                print(f"Early stopping triggered at epoch {e}. No improvement for 100 epochs.")
                break

    print(f"\nBest Validation Loss: {best_val_loss:.4f}")
    print("saving best model...")
    if best_params is not None:
        np.savez(MODEL_PARAMS_PATH, **{k: np.array(v) for k, v in best_params.items()})
    else:
        # Fallback if weirdly nothing improved (unlikely)
        np.savez(MODEL_PARAMS_PATH, **{k: np.array(v) for k, v in params.items()})

    np.savez(FEATURE_LIST_PATH, features=np.array(feats))

    # Calculate calibration factor on validation set
    print("\nCalculating calibration factor on validation set...")
    final_params = best_params if best_params is not None else params
    # Forward pass WITHOUT dropout for calibration
    val_predictions = forward(final_params, X_val, training=False, rng_key=None, dropout_rate=0.0)

    # Optionally dump val-set predictions (raw lambdas) for external backtests
    # (e.g. walk-forward against opening lines). game_ids are the val rows of the
    # date-sorted df.
    if dump_preds:
        vp = np.array(val_predictions)
        val_gids = df.iloc[train_size:]['game_id'].values
        pd.DataFrame({
            'game_id': val_gids,
            'pred_home': vp[:, 0], 'pred_away': vp[:, 1],
            'actual_home': np.array(Y_val[:, 0]), 'actual_away': np.array(Y_val[:, 1]),
        }).to_csv(dump_preds, index=False)
        print(f"dumped {len(val_gids)} val predictions -> {dump_preds}")

    # Calculate predicted average goals per team
    predicted_home_avg = float(jnp.mean(val_predictions[:, 0]))
    predicted_away_avg = float(jnp.mean(val_predictions[:, 1]))
    predicted_total_avg = predicted_home_avg + predicted_away_avg

    # Calculate actual average goals per team from validation set
    actual_home_avg = float(jnp.mean(Y_val[:, 0]))
    actual_away_avg = float(jnp.mean(Y_val[:, 1]))
    actual_total_avg = actual_home_avg + actual_away_avg

    # Calculate calibration factors
    calibration_factor_home = actual_home_avg / max(predicted_home_avg, 0.1)
    calibration_factor_away = actual_away_avg / max(predicted_away_avg, 0.1)
    calibration_factor_total = actual_total_avg / max(predicted_total_avg, 0.1)

    print(f"Validation Set Statistics:")
    print(f"  Actual:    Home {actual_home_avg:.3f} | Away {actual_away_avg:.3f} | Total {actual_total_avg:.3f}")
    print(f"  Predicted: Home {predicted_home_avg:.3f} | Away {predicted_away_avg:.3f} | Total {predicted_total_avg:.3f}")
    print(f"  Calibration Factors: Home {calibration_factor_home:.4f} | Away {calibration_factor_away:.4f} | Total {calibration_factor_total:.4f}")

    # --- WIN PREDICTION ACCURACY (comparable to MoneyPuck's 60%) ---
    def compute_win_accuracy(pred_home, pred_away, actual_home, actual_away, label=""):
        """Compute win prediction accuracy from Poisson rate predictions."""
        pred_h = np.array(pred_home)
        pred_a = np.array(pred_away)
        act_h = np.array(actual_home)
        act_a = np.array(actual_away)

        model_picks_home = pred_h > pred_a
        actual_home_won = act_h > act_a
        actual_away_won = act_a > act_h
        actual_tie = act_h == act_a

        decided_mask = ~actual_tie
        n_decided = decided_mask.sum()
        n_total = len(act_h)

        if n_decided > 0:
            correct = (model_picks_home[decided_mask] == actual_home_won[decided_mask]).sum()
            win_acc = correct / n_decided * 100
            print(f"  [{label}] Games with regulation winner: {n_decided}/{n_total}")
            print(f"  [{label}] Correct picks: {correct}/{n_decided}")
            print(f"  [{label}] Win Accuracy: {win_acc:.1f}%")
        else:
            win_acc = 0.0

        # Ties count as 0.5
        correct_full = (model_picks_home & actual_home_won).sum() + (~model_picks_home & actual_away_won).sum()
        correct_with_ties = correct_full + 0.5 * actual_tie.sum()
        win_acc_full = correct_with_ties / n_total * 100
        print(f"  [{label}] Win Accuracy (ties=0.5): {win_acc_full:.1f}%")
        print(f"  [{label}] Home win rate in set: {actual_home_won.mean() * 100:.1f}%")

        return win_acc, actual_home_won, model_picks_home

    print(f"\n{'=' * 60}")
    print("WIN PREDICTION ACCURACY (MoneyPuck benchmark: ~60%)")
    print('=' * 60)

    # Training set accuracy
    train_predictions = forward(final_params, X, training=False, rng_key=None, dropout_rate=0.0)
    compute_win_accuracy(train_predictions[:, 0], train_predictions[:, 1], Y[:, 0], Y[:, 1], "TRAIN")

    print()

    # Validation set accuracy
    val_pred_home = np.array(val_predictions[:, 0])
    val_pred_away = np.array(val_predictions[:, 1])
    val_actual_home = np.array(Y_val[:, 0])
    val_actual_away = np.array(Y_val[:, 1])

    _, actual_home_won, model_picks_home = compute_win_accuracy(
        val_pred_home, val_pred_away, val_actual_home, val_actual_away, "VAL"
    )

    # Confidence calibration: how well do predicted margins match reality?
    print(f"\n  Confidence Calibration (VAL set):")
    pred_margin = val_pred_home - val_pred_away
    confident_home = pred_margin > 0.3
    confident_away = pred_margin < -0.3
    close_games = ~confident_home & ~confident_away

    for label, mask in [("Strong Home (margin>0.3)", confident_home),
                        ("Close Game (|margin|<0.3)", close_games),
                        ("Strong Away (margin<-0.3)", confident_away)]:
        n = mask.sum()
        if n > 0:
            home_win_rate = actual_home_won[mask].mean() * 100
            pred_avg_margin = pred_margin[mask].mean()
            print(f"  {label}: {n} games | Home win rate: {home_win_rate:.1f}% | Avg pred margin: {pred_avg_margin:+.2f}")

    # --- Breakdown by game type (regular season vs playoff) ---
    val_game_ids = df.iloc[train_size:]['game_id'].values
    # NHL game_id format: YYYYTTNNNN where TT=02 (regular) or TT=03 (playoff)
    is_playoff = np.array([str(gid)[4:6] == '03' for gid in val_game_ids])
    is_regular = ~is_playoff
    n_reg = is_regular.sum()
    n_play = is_playoff.sum()
    print(f"\n  Val Set Breakdown: {n_reg} regular season | {n_play} playoff")
    if n_reg > 5:
        compute_win_accuracy(val_pred_home[is_regular], val_pred_away[is_regular],
                             val_actual_home[is_regular], val_actual_away[is_regular], "VAL-RegSeason")
    if n_play > 5:
        compute_win_accuracy(val_pred_home[is_playoff], val_pred_away[is_playoff],
                             val_actual_home[is_playoff], val_actual_away[is_playoff], "VAL-Playoff")

    print('=' * 60)

    # --- ODDS BACKTESTING: Model vs Market ---
    train_game_ids = df.iloc[:train_size]['game_id'].values
    train_pred_home = np.array(train_predictions[:, 0])
    train_pred_away = np.array(train_predictions[:, 1])
    train_actual_home = np.array(Y[:, 0])
    train_actual_away = np.array(Y[:, 1])
    odds_backtest(train_game_ids, train_pred_home, train_pred_away, train_actual_home, train_actual_away, label="TRAIN")
    odds_backtest(val_game_ids, val_pred_home, val_pred_away, val_actual_home, val_actual_away, label="VAL")

    # Save calibration factors
    np.savez(CALIBRATION_PATH,
             calibration_factor_home=calibration_factor_home,
             calibration_factor_away=calibration_factor_away,
             calibration_factor_total=calibration_factor_total,
             actual_home_avg=actual_home_avg,
             actual_away_avg=actual_away_avg,
             predicted_home_avg=predicted_home_avg,
             predicted_away_avg=predicted_away_avg)
    print(f"\nCalibration factors saved to {CALIBRATION_PATH}")

    print("Model saved.")

    return best_val_loss


def american_to_prob(o):
    o = float(o)
    if o > 0:
        return 100 / (o + 100)
    else:
        return abs(o) / (abs(o) + 100)


def lookup_player_id(player_name: str, db_conn, is_goalie: bool = True) -> str:
    """
    Look up a player ID by name. When is_goalie=True (default), queries goalie_game_stats
    to ensure only actual goaltenders are returned.

    Args:
        player_name: Full name ("Dustin Wolf") or last name ("Wolf")
        db_conn: SQLite database connection
        is_goalie: If True, only return goaltenders (via goalie_game_stats table)

    Returns:
        Player ID string (with [G] suffix for goalies)
    """
    print(f"playerID lookup for: '{player_name}'")

    search_name = player_name.strip().replace("'", "''")

    if is_goalie:
        # Query only goalies by using goalie_game_stats table
        query = f"""
            SELECT DISTINCT p.player_id, p.player_name
            FROM goalie_game_stats g
            JOIN players p ON g.player_id = p.player_id
            WHERE p.player_name LIKE '%{search_name}%' ESCAPE '\\'
            ORDER BY
                CASE WHEN LOWER(p.player_name) = LOWER('{search_name}') THEN 0 ELSE 1 END,
                p.player_name
        """
    else:
        query = f"""
            SELECT player_id, player_name
            FROM players
            WHERE player_name LIKE '%{search_name}%' ESCAPE '\\'
        """

    result = pd.read_sql_query(query, db_conn)
    print(f"results:\n{result}")

    if result.empty:
        print(f"ERROR: No {'goalie' if is_goalie else 'player'} found matching '{player_name}'")
        raise ValueError(f"No {'goalie' if is_goalie else 'player'} found matching '{player_name}'")

    if len(result) > 1:
        # Check for exact match
        exact = result[result['player_name'].str.lower() == search_name.lower()]
        if len(exact) == 1:
            player_id = exact['player_id'].iloc[0]
            print(f"  Exact match: {exact['player_name'].iloc[0]}")
            return f"{str(player_id).replace(' [G]', '').strip()} [G]" if is_goalie else player_id

        # Multiple matches - need disambiguation
        print(f"ERROR: Multiple {'goalies' if is_goalie else 'players'} match '{player_name}':")
        for _, row in result.iterrows():
            print(f"    - {row['player_name']} (ID: {row['player_id']})")
        print(f"  Please specify full name, e.g.: --home-goalie \"{result['player_name'].iloc[0]}\"")
        raise ValueError(f"Ambiguous: multiple matches for '{player_name}'")

    player_id = result['player_id'].iloc[0]
    print(f"  Found: {result['player_name'].iloc[0]} ({player_id})")
    return f"{str(player_id).replace(' [G]', '').strip()} [G]" if is_goalie else player_id

def get_latest_stats_for_manual(db_path):
    """
    Get latest team stats for manual prediction.
    NOW INCLUDES: Identifying primary starting goalie for each team.
    """
    df = get_base_team_stats(db_path, use_complete_games_filter=False)
    # Sort by mp_game_date (now preserved in final output)
    df = df.sort_values('mp_game_date')
    
    # print(f"DEBUG: get_latest_stats df columns sample: {[c for c in df.columns if 'give' in c]}")

    # Get latest for home teams - select home_team_id, mp_game_date, and all home_ columns
    home_cols = ['home_team_id', 'mp_game_date'] + [c for c in df.columns if c.startswith('home_') and c != 'home_team_id']
    home_latest = df[home_cols].copy()

    # Filter out games with no stats (using xgf > 0 as proxy)
    if 'home_xgf' in home_latest.columns:
         home_latest = home_latest[home_latest['home_xgf'] > 0]

    # Filter out games with missing NST data (roll_hdcf_share should be > 0 for complete games)
    if 'home_roll_hdcf_share' in home_latest.columns:
         home_latest = home_latest[home_latest['home_roll_hdcf_share'] > 0]

    home_latest = home_latest.drop_duplicates('home_team_id', keep='last')
    # Rename columns: home_team_id -> team_id, keep mp_game_date, strip home_ prefix from rest
    # Use slicing [5:] to remove 'home_' prefix safely (avoiding replace() issues with substrings like 'home_' inside column names)
    new_home_cols = ['team_id', 'mp_game_date'] + [c[5:] for c in home_cols[2:]]
    home_latest.columns = new_home_cols

    # Get latest for away teams
    away_cols = ['away_team_id', 'mp_game_date'] + [c for c in df.columns if c.startswith('away_') and c != 'away_team_id']
    away_latest = df[away_cols].copy()

    # Filter out games with no stats
    if 'away_xgf' in away_latest.columns:
         away_latest = away_latest[away_latest['away_xgf'] > 0]

    # Filter out games with missing NST data (roll_hdcf_share should be > 0 for complete games)
    if 'away_roll_hdcf_share' in away_latest.columns:
         away_latest = away_latest[away_latest['away_roll_hdcf_share'] > 0]

    away_latest = away_latest.drop_duplicates('away_team_id', keep='last')
    # Rename columns: away_team_id -> team_id, keep mp_game_date, strip away_ prefix from rest
    # Use slicing [5:] to remove 'away_' prefix safely (avoiding replace() issues with substrings like 'away_' inside column names e.g. 'giveaway_d')
    new_away_cols = ['team_id', 'mp_game_date'] + [c[5:] for c in away_cols[2:]]
    away_latest.columns = new_away_cols

    # Combine and keep most recent
    combined = pd.concat([home_latest, away_latest]).sort_values(['team_id', 'mp_game_date'])
    latest = combined.drop_duplicates('team_id', keep='last')

    teams = pd.read_sql_query("SELECT team_id, team_abbr FROM teams", _connect(db_path))
    teams['normalized_abbr'] = teams['team_abbr'].apply(lambda x: norm(x))
    latest = pd.merge(teams, latest, on='team_id', how='left').fillna(0)
    latest['mp_game_date'] = pd.to_datetime(latest['mp_game_date'])
    if FORM_DISCOUNT:   # per-season league means of the form columns, for discounting a stale (last-season) row
        _sea = df['game_id'].astype(str).str[:4].astype(int)
        _fc = [c[5:] for c in df.columns if c.startswith('home_') and _FORM_RE.match(c[5:]) and f'away_{c[5:]}' in df.columns]
        _form_mu = pd.DataFrame({c: pd.concat([df[f'home_{c}'], df[f'away_{c}']]).groupby(
            pd.concat([_sea, _sea])).mean() for c in _fc})

    # NEW: Identify primary starter for each team
    # (Goalie who has started the most games in the last 10 games)

    con = _connect(db_path)
    recent_starters_query = """
    WITH recent_games AS (
        SELECT g.game_id, g.game_date, g.home_team_id, g.away_team_id
        FROM games g
        ORDER BY g.game_date DESC
        LIMIT 200
    ),
    all_goalie_starts AS (
        SELECT game_id, team_id, player_id, toi_seconds FROM goalie_game_stats
        WHERE toi_seconds > 1800
        UNION ALL
        SELECT game_id, team_id, player_id, mp_ice_time as toi_seconds FROM mp_goalie_game_stats
        WHERE mp_ice_time > 1800
    ),
    goalie_starts AS (
        SELECT
            gg.team_id,
            gg.player_id,
            COUNT(*) as games_started,
            MAX(rg.game_date) as last_start_date
        FROM all_goalie_starts gg
        JOIN recent_games rg ON gg.game_id = rg.game_id
        GROUP BY gg.team_id, gg.player_id
    ),
    primary_starters AS (
        SELECT
            team_id,
            player_id,
            games_started,
            ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY games_started DESC, last_start_date DESC) as starter_rank
        FROM goalie_starts
    )
    SELECT team_id, player_id as primary_goalie_id, games_started
    FROM primary_starters
    WHERE starter_rank = 1
    """
    try:
        primary_starters = pd.read_sql_query(recent_starters_query, con)
        # Merge primary starter info into latest stats
        latest = pd.merge(latest, primary_starters[['team_id', 'primary_goalie_id']], on='team_id', how='left')
    except Exception as e:
        print(f"Warning: Could not identify primary starters: {e}")
        latest['primary_goalie_id'] = None
        
    con.close()
    
    if FORM_DISCOUNT:
        latest.attrs['form_mu'] = _form_mu   # set last: merges drop attrs
    return latest


def _resolve_goalie_features(goalie_features_df, goalie_id, team_id, primary_goalie_id, side_label, team_verified=False):
    """Return the latest per-goalie feature row for a forecast.

    Falls back to the team's primary starter (never league averages / zeros) when
    the requested goalie is missing or belongs to a different team. Returns an
    empty DataFrame only if even the primary starter cannot be resolved.
    """
    def clean(gid):
        return str(gid).replace(' [G]', '').strip() if gid is not None else None

    def latest_row_for(gid, restrict_team=None):
        gid = clean(gid)
        if not gid:
            return pd.DataFrame()
        rows = goalie_features_df[goalie_features_df['player_id'] == gid]
        if restrict_team is not None:
            rows = rows[rows['team_id'] == int(restrict_team)]
        rows = rows.sort_values('game_id')
        return rows.tail(1) if not rows.empty else pd.DataFrame()

    primary_id = clean(primary_goalie_id)

    # 1. Requested goalie, validated against the requested team.
    row = latest_row_for(goalie_id)
    if not row.empty:
        row_team = int(row['team_id'].iloc[0])
        if int(team_id) == row_team or team_verified:   # team_verified: starter confirmed on the CURRENT roster (pregame)
            if int(team_id) != row_team:
                print(f"  {side_label}: goalie {clean(goalie_id)} last played for team {row_team}; "
                      f"accepted (on the current roster per pregame feed), using his latest form.")
            return row
        print(f"⚠  {side_label}: goalie {clean(goalie_id)} last played for team {row_team}, "
              f"not the requested team {int(team_id)} (likely a typo/swapped or traded goalie). "
              f"Falling back to primary starter {primary_id}.")
    elif clean(goalie_id):
        print(f"⚠  {side_label}: no data for requested goalie {clean(goalie_id)}. "
              f"Falling back to primary starter {primary_id}.")

    # 2. Team primary starter (restricted to their games for this team).
    if primary_id and primary_id != clean(goalie_id):
        prow = latest_row_for(primary_id, restrict_team=team_id)
        if not prow.empty:
            return prow

    print(f"⚠  {side_label}: could not resolve any goalie features "
          f"(requested={clean(goalie_id)}, primary={primary_id}); features will default to 0.")
    return pd.DataFrame()


def manual_forecast(db, home_abbr, away_abbr, date_str, h_rest, a_rest, h_odd, a_odd, n_sims, home_goalie_id=None, away_goalie_id=None, use_calibration=True, pregame=None, ensemble_tags=None):
    global LINEUP_OVERRIDE
    if (PLAYER_PROJ or LINEUP_OVERRIDE) and pregame is None:
        raise SystemExit("NHL_PLAYER_PROJ / NHL_LINEUP_OVERRIDE need tonight's lineup: run without --offline.")
    if LINEUP_OVERRIDE and not (PLAYER_PROJ and ROLL_MODE == 'shrink'):
        raise SystemExit("NHL_LINEUP_OVERRIDE=1 needs NHL_ROLL_MODE=shrink and NHL_PLAYER_PROJ=1")
    if not os.path.exists(MODEL_PARAMS_PATH):
        print("No model – train first.")
        return

    norm = lambda s: s.replace('.', '').upper()
    h_norm = norm(home_abbr)
    a_norm = norm(away_abbr)

    # Model set(s): production = one model; --override = the tagged seed models, averaged (pooled sims)
    if ensemble_tags:
        sets = [(f"advanced_model_params_v6_{t}.npz", f"advanced_standardize_stats_v6_{t}.npz",
                 f"model_calibration_v6_{t}.npz", f"feature_list_{t}.npz") for t in ensemble_tags]
    else:
        sets = [(MODEL_PARAMS_PATH, STATS_PATH, CALIBRATION_PATH, FEATURE_LIST_PATH)]
    models = []
    for mp, sp, cp, fp in sets:
        if not all(os.path.exists(f) for f in (mp, sp, fp)):
            raise SystemExit(f"missing model files for {mp} — train it first")
        models.append(({k: jnp.array(v) for k, v in np.load(mp).items()}, sp, cp, np.load(fp)['features'].tolist()))
    feats = models[0][3]
    assert all(m[3] == feats for m in models), "ensemble models were trained on different feature lists"
    params = models[0][0]

    _ovr_flag = LINEUP_OVERRIDE
    LINEUP_OVERRIDE = False   # history rows un-overridden; tonight's override is applied below from TONIGHT's lineup
    try:
        latest = get_latest_stats_for_manual(db)
    finally:
        LINEUP_OVERRIDE = _ovr_flag

    h_candidates = latest[latest['normalized_abbr'] == h_norm]
    if h_candidates.empty:
        print(f"Error: Home team '{h_norm}' not found.")
        return
    h_row = h_candidates.sort_values('roll_goals_for', ascending=False).iloc[0]

    a_candidates = latest[latest['normalized_abbr'] == a_norm]
    if a_candidates.empty:
        print(f"Error: Away team '{a_norm}' not found.")
        return
    a_row = a_candidates.sort_values('roll_goals_for', ascending=False).iloc[0]

    # Pre-game context (pregame.py): tonight's lineup ratings, real rest, feed goalies
    goalie_verified = {'home': False, 'away': False}
    if pregame is not None:
        h_row, a_row = h_row.copy(), a_row.copy()
        for side, row in (('home', h_row), ('away', a_row)):
            row['roster_drapm'] = pregame[side]['roster_drapm']
            row['roster_orapm'] = pregame[side]['roster_orapm']
        print(f"pregame: roster ratings from tonight's projected lineups "
              f"(home D {h_row['roster_drapm']:+.4f} O {h_row['roster_orapm']:+.4f} | "
              f"away D {a_row['roster_drapm']:+.4f} O {a_row['roster_orapm']:+.4f})")
        if home_goalie_id is None and pregame['home'].get('goalie_id'):
            home_goalie_id = pregame['home']['goalie_id']; goalie_verified['home'] = True
            print(f"pregame: home goalie {pregame['home']['goalie_name']} [{pregame['home']['goalie_status']}]")
        if away_goalie_id is None and pregame['away'].get('goalie_id'):
            away_goalie_id = pregame['away']['goalie_id']; goalie_verified['away'] = True
            print(f"pregame: away goalie {pregame['away']['goalie_name']} [{pregame['away']['goalie_status']}]")

    if FORM_DISCOUNT:   # EXPERIMENT 2026-10: a team that has not played yet this season carries last season's form
        _d = pd.to_datetime(date_str or str(datetime.date.today()))
        _tonight_season = _d.year if _d.month >= 8 else _d.year - 1
        _carry, _mu = _load_form_carry(), latest.attrs['form_mu']
        h_row, a_row = h_row.copy(), a_row.copy()
        for side, row in (('home', h_row), ('away', a_row)):
            _ld = pd.Timestamp(row['mp_game_date']); _row_season = _ld.year if _ld.month >= 8 else _ld.year - 1
            if _row_season < _tonight_season and _row_season in _mu.index:
                for c in _mu.columns:
                    if c in row.index:
                        row[c] = _mu.at[_row_season, c] + (row[c] - _mu.at[_row_season, c]) * float(form_discount_factor(c, 0, _carry))
                print(f"form-discount {side}: first game of {_tonight_season}-{_tonight_season + 1 - 2000} -> "
                      f"{_row_season} form carried at {_carry['_default']:.0%} (roll_xgf now {row['roll_xgf']:.2f})")
    if PLAYER_PROJ and pregame is not None:   # EXPERIMENT 2026-10: tonight's lineup projections (+ override)
        tonight = date_str or str(datetime.date.today())
        _c = _connect(db)
        pcols = ['proj_xgf60', 'proj_xga60', 'proj_cf_pct', 'proj_xg_pct']
        proj_hist = process_player_projection(_c) if LINEUP_OVERRIDE else None
        gd = dict(_c.execute("SELECT game_id, game_date FROM games")) if LINEUP_OVERRIDE else {}
        for side, row in (('home', h_row), ('away', a_row)):
            ids = [p['id'] for p in pregame[side]['lineup'] if p.get('id')]
            pj = project_lineup_asof(_c, ids, tonight)
            msg = f"pregame-proj {side}: " + ", ".join(f"{k} {v:.3f}" for k, v in pj.items())
            if LINEUP_OVERRIDE:
                tid = int(row['team_id'])
                ph = proj_hist[proj_hist['team_id'] == tid].copy()
                ph['d'] = ph['game_id'].map(gd)
                ph = ph[ph['d'] < tonight].sort_values(['d', 'game_id']).tail(OVR_WINDOW)
                cont = continuity_asof(_c, tid, ids, tonight)
                _override_columns(row, {k: pj[k] - ph[k].mean() for k in pcols}, cont)
                row['lineup_continuity'] = cont
                msg += f" | continuity {cont:.2f} | Δxgf60 {pj['proj_xgf60'] - ph['proj_xgf60'].mean():+.3f} Δxga60 {pj['proj_xga60'] - ph['proj_xga60'].mean():+.3f}"
            for k, v in pj.items():
                row[k] = v
            print(msg)
        _c.close()

    # Calculate rest days from game date
    if (date_str is not None):
      print(f"\ncalculating rest days from date: {date_str}")
      target = pd.to_datetime(date_str)
      h_rest = min(max((target - h_row['mp_game_date']).days, 0), REST_CAP)
      a_rest = min(max((target - a_row['mp_game_date']).days, 0), REST_CAP)
    else:
      # Clip manual rest inputs to match training distribution [0, REST_CAP]
      h_rest = min(max(h_rest, 0), REST_CAP)
      a_rest = min(max(a_rest, 0), REST_CAP)
    
    if pregame is not None:   # real last-game dates (the DB's last game may be behind)
        h_rest = min(max(pregame['home']['rest'], 0), REST_CAP)
        a_rest = min(max(pregame['away']['rest'], 0), REST_CAP)
        print("pregame: rest days from each team's actual last game")
    print(f"rest-days (home): {h_rest}")
    print(f"rest-days (away): {a_rest}")
    print(f"rest-days (diff): {h_rest - a_rest}")
    
    # NEW: Determine which goalies to use
    if home_goalie_id is None:
        home_goalie_id = h_row.get('primary_goalie_id', None)
        print(f"Using primary starter for {h_norm}: {home_goalie_id}")
    else:
        print(f"{'Pregame-feed' if goalie_verified['home'] else 'User-specified'} goalie for {h_norm}: {home_goalie_id}")

    if away_goalie_id is None:
        away_goalie_id = a_row.get('primary_goalie_id', None)
        print(f"Using primary starter for {a_norm}: {away_goalie_id}")
    else:
        print(f"{'Pregame-feed' if goalie_verified['away'] else 'User-specified'} goalie for {a_norm}: {away_goalie_id}")
        
    # Clean IDs for lookup
    if home_goalie_id: home_goalie_id = str(home_goalie_id).replace(' [G]', '').strip()
    if away_goalie_id: away_goalie_id = str(away_goalie_id).replace(' [G]', '').strip()
        
    print("\n")

    # NEW: Get goalie features for specified goalies
    con = _connect(db)
    goalie_features_df = process_goalie_metrics(con)
    con.close()

    # Resolve goalie features with a primary-starter fallback.
    # We have full per-goalie data, so we NEVER fall back to league averages /
    # zeros. Resolution order:
    #   1. The requested goalie, IF their latest game was for the requested team
    #      (validates team membership: catches typos / swapped home-away goalies /
    #       a goalie who has since been traded away).
    #   2. The team's primary starter (most-started goalie in recent games).
    # team_id is intentionally NOT used to filter the row set (a mid-season-traded
    # goalie's latest row already carries the correct current team) — it is only
    # used to validate membership after selecting the goalie's latest row.
    home_goalie_features = _resolve_goalie_features(
        goalie_features_df, home_goalie_id, h_row['team_id'],
        h_row.get('primary_goalie_id', None), f"{h_norm} (home)", team_verified=goalie_verified['home'])
    away_goalie_features = _resolve_goalie_features(
        goalie_features_df, away_goalie_id, a_row['team_id'],
        a_row.get('primary_goalie_id', None), f"{a_norm} (away)", team_verified=goalie_verified['away'])

    matchup = {}
    for f in feats:
        if f.startswith('home_goalie_'):
            # Extract goalie feature name
            goalie_feat = f.replace('home_goalie_', '')
            val = 0.0
            # Map feature names if they differ in process_goalie_metrics vs training
            # (they are same: roll_gsax, roll_hd_gsax, roll_rcr, roll_fatigue_index)
            if not home_goalie_features.empty and goalie_feat in home_goalie_features.columns:
                val = home_goalie_features[goalie_feat].iloc[0]
            # Use fallback from h_row if user didn't specify goalie and h_row has it?
            # Actually h_row comes from get_latest_stats which uses primary starter.
            # But here we might have overridden it. Best to use the fetched features.
            # If fetched features empty, fallback to 0.0 or league avg
            matchup[f] = val
            
        elif f.startswith('away_goalie_'):
            goalie_feat = f.replace('away_goalie_', '')
            val = 0.0
            if not away_goalie_features.empty and goalie_feat in away_goalie_features.columns:
                val = away_goalie_features[goalie_feat].iloc[0]
            matchup[f] = val
            
        # Handle special features BEFORE generic home_/away_ prefix handlers
        elif f == 'home_rest':
            matchup[f] = h_rest
        elif f == 'away_rest':
            matchup[f] = a_rest
        # Generic handlers for all other home_/away_ prefixed features
        elif f.startswith('home_'):
            matchup[f] = h_row.get(f[len('home_'):], 0)
        elif f.startswith('away_'):
            matchup[f] = a_row.get(f[len('away_'):], 0)
        # elif f == 'rest_diff':
        #    matchup[f] = h_rest - a_rest
        elif f == 'league_home_win_pct':
            # Compute current league-wide home win % from recent games
            con2 = _connect(db)
            league_query = """
            SELECT
                COALESCE(h.mp_goals_for, 0) as home_goals,
                COALESCE(a.mp_goals_for, 0) as away_goals
            FROM games g
            LEFT JOIN mp_team_game_stats h ON g.game_id = h.game_id AND g.home_team_id = h.team_id
                AND h.situation_id = (SELECT situation_id FROM situations WHERE LOWER(situation_code) LIKE '%all%' LIMIT 1)
            LEFT JOIN mp_team_game_stats a ON g.game_id = a.game_id AND g.away_team_id = a.team_id
                AND a.situation_id = (SELECT situation_id FROM situations WHERE LOWER(situation_code) LIKE '%all%' LIMIT 1)
            ORDER BY g.game_date DESC
            LIMIT 300
            """
            league_df = pd.read_sql_query(league_query, con2)
            con2.close()
            if not league_df.empty:
                league_home_win = (league_df['home_goals'] > league_df['away_goals']).mean()
                matchup[f] = league_home_win
                print(f"  League home win % (last {len(league_df)} games): {league_home_win:.3f}")
            else:
                matchup[f] = 0.50
        elif f == 'home_prob':
            matchup[f] = american_to_prob(h_odd or -110)
        elif f == 'away_prob':
            matchup[f] = american_to_prob(a_odd or -110)
        else:
            matchup[f] = 0.0

    # Calculate goalie differential features (must be done after matchup is populated)
    GOALIE_BOOST_FACTOR = 2.0  # Must match the factor used in training data

    # Get goalie values from matchup (already populated above)
    home_gsax = matchup.get('home_goalie_roll_gsax', 0.0)
    away_gsax = matchup.get('away_goalie_roll_gsax', 0.0)
    home_hd_gsax = matchup.get('home_goalie_roll_hd_gsax', 0.0)
    away_hd_gsax = matchup.get('away_goalie_roll_hd_gsax', 0.0)
    home_rcr = matchup.get('home_goalie_roll_rcr', 0.92)  # Default to league avg (measured)
    away_rcr = matchup.get('away_goalie_roll_rcr', 0.92)

    # Compute differential features
    if 'goalie_gsax_diff' in feats:
        matchup['goalie_gsax_diff'] = (home_gsax - away_gsax) * GOALIE_BOOST_FACTOR
    if 'goalie_hd_gsax_diff' in feats:
        matchup['goalie_hd_gsax_diff'] = (home_hd_gsax - away_hd_gsax) * GOALIE_BOOST_FACTOR
    if 'home_goalie_quality' in feats:
        matchup['home_goalie_quality'] = (
            home_gsax * 0.4 + home_hd_gsax * 0.4 + (1.0 - home_rcr) * 0.2
        ) * GOALIE_BOOST_FACTOR
    if 'away_goalie_quality' in feats:
        matchup['away_goalie_quality'] = (
            away_gsax * 0.4 + away_hd_gsax * 0.4 + (1.0 - away_rcr) * 0.2
        ) * GOALIE_BOOST_FACTOR

    # ROSTER RAPM matchup features: recompute from each team's latest roster aggregate
    # (home_/away_roster_drapm|orapm are filled by the generic handler from h_row/a_row).
    h_rd = matchup.get('home_roster_drapm', 0.0); a_rd = matchup.get('away_roster_drapm', 0.0)
    h_ro = matchup.get('home_roster_orapm', 0.0); a_ro = matchup.get('away_roster_orapm', 0.0)
    if 'home_roster_off_vs_def' in feats:
        matchup['home_roster_off_vs_def'] = h_ro - a_rd
    if 'away_roster_off_vs_def' in feats:
        matchup['away_roster_off_vs_def'] = a_ro - h_rd
    if 'roster_drapm_diff' in feats:
        matchup['roster_drapm_diff'] = a_rd - h_rd

    # Matchup-specific team inputs, same formulas as training's final section. FIX 2026-10-01: these were
    # copied from each team's LAST game, i.e. computed against LAST game's opponent, not tonight's.
    if 'home_sted' in feats or 'away_sted' in feats:
        hs = ((h_row.get('roll_pp_xg60', 0.0) - a_row.get('roll_pk_xga60', 0.0)) -
              (a_row.get('roll_pp_xg60', 0.0) - h_row.get('roll_pk_xga60', 0.0)))
        if 'home_sted' in feats: matchup['home_sted'] = hs
        if 'away_sted' in feats: matchup['away_sted'] = -hs
    if 'home_osa_xg' in feats:
        matchup['home_osa_xg'] = h_row.get('roll_xgf', 0.0) * a_row.get('opp_xg_suppression', 1.0)
    if 'away_osa_xg' in feats:
        matchup['away_osa_xg'] = a_row.get('roll_xgf', 0.0) * h_row.get('opp_xg_suppression', 1.0)

    print("matchup")
    for (k,v) in matchup.items():
      print(f"  {k}: {v}")
    print("\n")

    rates = []
    for mi, (params, STATS_PATH_i, CALIBRATION_PATH_i, _f) in enumerate(models):
        if len(models) > 1:
            print(f"--- model {mi + 1}/{len(models)}: {ensemble_tags[mi]}")
        X = standardize_data(pd.DataFrame([matchup])[feats], feats, STATS_PATH_i, 'predict')
        lam = forward(params, jnp.array(X.values))[0]
        lh_raw, la_raw = float(lam[0]), float(lam[1])

        # Load and apply calibration factors
        if use_calibration and os.path.exists(CALIBRATION_PATH_i):
            cal_data = np.load(CALIBRATION_PATH_i)
            cal_factor_home = float(cal_data['calibration_factor_home'])
            cal_factor_away = float(cal_data['calibration_factor_away'])
            cal_factor_total = float(cal_data['calibration_factor_total'])

            # Apply calibration
            lh = lh_raw * cal_factor_home
            la = la_raw * cal_factor_away

            print(f"\nRaw Predicted Rates → {h_row['team_abbr']} {lh_raw:.2f} | {a_row['team_abbr']} {la_raw:.2f} (Total: {lh_raw + la_raw:.2f})")
            print(f"Calibration Applied → {h_row['team_abbr']} {cal_factor_home:.4f} | {a_row['team_abbr']} {cal_factor_away:.4f}")
            print(f"Calibrated Rates    → {h_row['team_abbr']} {lh:.2f} | {a_row['team_abbr']} {la:.2f} (Total: {lh + la:.2f})\n")
        elif not use_calibration:
            lh, la = lh_raw, la_raw
            print(f"\n⚠️  Calibration disabled - using raw predictions")
            print(f"Projected Rates → {h_row['team_abbr']} {lh:.2f} | {a_row['team_abbr']} {la:.2f}\n")
        else:
            lh, la = lh_raw, la_raw
            print(f"\n⚠️  No calibration file found - using raw predictions")
            print(f"Projected Rates → {h_row['team_abbr']} {lh:.2f} | {a_row['team_abbr']} {la:.2f}\n")
        rates.append((lh, la))
    lh = float(np.mean([r[0] for r in rates])); la = float(np.mean([r[1] for r in rates]))
    if len(rates) > 1:
        print(f"Ensemble mean rates → {h_row['team_abbr']} {lh:.2f} | {a_row['team_abbr']} {la:.2f} (Total: {lh + la:.2f})  "
              f"[{len(rates)} models; sims pooled = probabilities averaged]\n")

    print(f"Simulating {n_sims:,} games...\n{'=' * 60}")

    # Period scoring: empirical non-EN distribution scaled to 58/60 of the game
    # The model's predicted rate (lambda) includes EN goals from training data.
    # Periods cover 58 min of normal play (96.67% of lambda as base rate).
    # EN covers final ~2 min (3.33% of lambda as base rate) with score-state
    # multipliers modeling the actual goalie-pull effect on top of normal scoring.
    # P2 is historically highest due to short change (bench closer to attacking zone).
    REGULATION_SHARE = 58.0 / 60.0  # 0.9667 - periods' share of the game rate
    EN_BASE_SHARE = 2.0 / 60.0      # 0.0333 - EN phase base (normal 2-min scoring rate)

    con_sim = _connect(db)
    period_query = """
    SELECT period,
           SUM(CASE WHEN shot_on_empty_net = 0 OR shot_on_empty_net IS NULL THEN 1 ELSE 0 END) as non_en_goals
    FROM mp_shots
    WHERE goal = 1 AND period BETWEEN 1 AND 3
    GROUP BY period ORDER BY period
    """
    period_df = pd.read_sql_query(period_query, con_sim)
    con_sim.close()

    if len(period_df) == 3 and period_df['non_en_goals'].sum() > 0:
        total_non_en = float(period_df['non_en_goals'].sum())
        # Distribute REGULATION_SHARE across periods proportional to empirical non-EN goals
        p1_wt = float(period_df.iloc[0]['non_en_goals']) / total_non_en * REGULATION_SHARE
        p2_wt = float(period_df.iloc[1]['non_en_goals']) / total_non_en * REGULATION_SHARE
        p3_wt = float(period_df.iloc[2]['non_en_goals']) / total_non_en * REGULATION_SHARE
    else:
        # Fallback if shot data unavailable
        p1_wt = 0.317 * REGULATION_SHARE
        p2_wt = 0.372 * REGULATION_SHARE
        p3_wt = 0.311 * REGULATION_SHARE

    print(f"Period weights (empirical): P1={p1_wt:.3f}  P2={p2_wt:.3f}  P3={p3_wt:.3f}  EN base={EN_BASE_SHARE:.3f}  (periods={p1_wt+p2_wt+p3_wt:.3f})")

    from scipy.stats import norm as _norm, poisson as _poisson

    # FIX 2026-10-01 (sim conservation). The network's rates (lh, la) are trained on REAL goals, which already
    # include empty-net goals and real OT goals. The old sim ADDED goalie-pull multipliers and an OT +1 on top
    # (~+0.4 goals/game, ~0.3 of it double counting). Now each team's rates are scaled so that its expected REAL
    # goals (regulation + EN + real OT goals) equal lh / la exactly; the EN/OT logic only shapes margins and scores.
    # Reported totals follow the market convention: real goals + 1 for the shootout winner.
    OT_GOAL_SHARE = 0.674   # measured 2026-10-01: share of OT games settled by a real OT goal (6,568 reg-season games)
    cov = [[1.0, HOME_AWAY_RHO_GAUSS], [HOME_AWAY_RHO_GAUSS, 1.0]]

    def _sim(lh, la, p_home_ot, sh, sa, n, seed=None):
        rng = np.random.default_rng(seed)
        # --- Correlated regulation scoring (Gaussian copula): exact Poisson marginals, negative correlation ---
        z = rng.multivariate_normal([0.0, 0.0], cov, n)
        u = _norm.cdf(z)
        c_h = _poisson.ppf(u[:, 0], lh * sh * REGULATION_SHARE).astype(np.int64)
        c_a = _poisson.ppf(u[:, 1], la * sa * REGULATION_SHARE).astype(np.int64)
        d = c_h - c_a
        # --- Empty net phase: final ~2 minutes; multipliers model the goalie-pull effect ---
        r_h = np.full(n, lh * sh * EN_BASE_SHARE)
        r_a = np.full(n, la * sa * EN_BASE_SHARE)
        p_h = (d >= -3) & (d < 0)   # home trailing by 1-3, pulls goalie
        p_a = (d <= 3) & (d > 0)    # away trailing by 1-3, pulls goalie
        r_h[p_a] *= EMPTY_NET_MULTIPLIER_FOR
        r_a[p_a] *= EMPTY_NET_MULTIPLIER_AGAINST
        r_h[p_h] *= EMPTY_NET_MULTIPLIER_AGAINST
        r_a[p_h] *= EMPTY_NET_MULTIPLIER_FOR
        e_h, e_a = rng.poisson(r_h), rng.poisson(r_a)
        g_h, g_a = c_h + e_h, c_a + e_a
        # --- Overtime / shootout: tied games get +1 for the winner (OT goal or the shootout-winner convention) ---
        t = g_h == g_a
        hw = rng.random(n) < p_home_ot
        by_goal = rng.random(n) < OT_GOAL_SHARE
        return dict(cur_h=c_h, cur_a=c_a, diff=d, en_h=e_h, en_a=e_a, reg_h=g_h, reg_a=g_a, tied=t,
                    real_h=g_h + (t & hw & by_goal), real_a=g_a + (t & ~hw & by_goal),
                    final_h=g_h + (t & hw), final_a=g_a + (t & ~hw))

    parts = []
    for mi, (lh_i, la_i) in enumerate(rates):   # one conserved sim per model, equal shares, pooled
        p_ot_i = float(np.clip(OT_HOME_WIN_BASE + OT_STRENGTH_TILT * (lh_i - la_i), 0.30, 0.70))
        sh = sa = 1.0
        for _ in range(3):   # fixed-point: expected REAL goals == the network's rate, per team (seeded 200k pilot)
            pilot = _sim(lh_i, la_i, p_ot_i, sh, sa, 200000, seed=20261001)
            sh *= lh_i / pilot['real_h'].mean()
            sa *= la_i / pilot['real_a'].mean()
        n_i = n_sims // len(rates) + (n_sims % len(rates) if mi == len(rates) - 1 else 0)
        o_i = _sim(lh_i, la_i, p_ot_i, sh, sa, n_i)
        parts.append(o_i)
        print(f"Sim conservation{f' [{ensemble_tags[mi]}]' if len(rates) > 1 else ''}: rate scale home {sh:.3f} away {sa:.3f} | "
              f"real goals {np.mean(o_i['real_h'] + o_i['real_a']):.2f} (network {lh_i + la_i:.2f}) | "
              f"+ shootout-winner goals {np.mean(o_i['final_h'] + o_i['final_a'] - o_i['real_h'] - o_i['real_a']):.2f}")
    _o = {k: np.concatenate([o[k] for o in parts]) for k in parts[0]}
    cur_h, cur_a, diff, en_h, en_a = _o['cur_h'], _o['cur_a'], _o['diff'], _o['en_h'], _o['en_a']
    reg_h, reg_a, tied = _o['reg_h'], _o['reg_a'], _o['tied']
    final_h, final_a = _o['final_h'].copy(), _o['final_a'].copy()
    total = final_h + final_a

    ot_rate = float(np.mean(tied))

    # --- SIMULATION OVERVIEW ---
    print(f"{h_row['team_abbr']} {np.mean(final_h):.2f} – {a_row['team_abbr']} {np.mean(final_a):.2f}  |  Total {np.mean(total):.2f}")
    print(f"Home/away goal corr (sim): {np.corrcoef(final_h, final_a)[0, 1]:+.3f}  (target ≈ -0.118)  |  Reach OT/SO: {100 * ot_rate:.1f}%")

    # Moneyline now sums to 100% (every game has a winner, incl. OT/SO)
    win_h = np.mean(final_h > final_a)
    win_a = np.mean(final_a > final_h)

    print(f"Win Probability → {h_row['team_abbr']}: {100 * win_h:.1f}%   |   {a_row['team_abbr']}: {100 * win_a:.1f}%  (incl. OT/SO)")
    print(f"Puckline (-1.5) → {h_row['team_abbr']}: {100 * np.mean(final_h - final_a >= 2):.1f}%   |   {a_row['team_abbr']}: {100 * np.mean(final_a - final_h >= 2):.1f}%")
    print(f"Over 6.5: {100 * np.mean(total > 6.5):.1f}%   |   Under 6.5: {100 * np.mean(total <= 6.5):.1f}%")

    # 1. Most Likely Scores
    print("\nMost Likely Scores:")
    score_counts = pd.DataFrame({'h': final_h, 'a': final_a}).groupby(['h', 'a']).size().reset_index(name='count')
    score_counts['prob'] = score_counts['count'] / n_sims
    top_scores = score_counts.sort_values('count', ascending=False).head(5)

    for _, row in top_scores.iterrows():
        print(f"  {h_row['team_abbr']} {int(row['h'])} - {int(row['a'])} {a_row['team_abbr']}  ({row['prob']*100:.1f}%)")

    # 2. Projected Period Scoring (regulation mean split by empirical period weights;
    #    P3 adds the EN phase). Periods are a display approximation of the simulated
    #    regulation totals (cur_h/cur_a), which is what the EN/OT logic runs on.
    pw = np.array([p1_wt, p2_wt, p3_wt])
    pw = pw / pw.sum()
    mh, ma = float(np.mean(cur_h)), float(np.mean(cur_a))
    sp1_h, sp1_a = mh * pw[0], ma * pw[0]
    sp2_h, sp2_a = mh * pw[1], ma * pw[1]
    sp3_h, sp3_a = mh * pw[2] + np.mean(en_h), ma * pw[2] + np.mean(en_a)

    print(f"\nProjected Scoring by Period:")
    print(f"  P1: {h_row['team_abbr']} {sp1_h:.2f} - {sp1_a:.2f} {a_row['team_abbr']}")
    print(f"  P2: {h_row['team_abbr']} {sp2_h:.2f} - {sp2_a:.2f} {a_row['team_abbr']}")
    print(f"  P3: {h_row['team_abbr']} {sp3_h:.2f} - {sp3_a:.2f} {a_row['team_abbr']}  (includes EN)")

    # 3. Win Margin Distribution (ASCII)
    print("\nWin Margin Distribution:")
    margins = final_h - final_a
    # Bins: <-2, -2, -1, 0, 1, 2, >2
    dist = {
        f"{a_row['team_abbr']} by 3+": np.mean(margins <= -3),
        f"{a_row['team_abbr']} by 2 ": np.mean(margins == -2),
        f"{a_row['team_abbr']} by 1 ": np.mean(margins == -1),
        "Tie      ": np.mean(margins == 0),
        f"{h_row['team_abbr']} by 1 ": np.mean(margins == 1),
        f"{h_row['team_abbr']} by 2 ": np.mean(margins == 2),
        f"{h_row['team_abbr']} by 3+": np.mean(margins >= 3),
    }
    
    for label, prob in dist.items():
        bar_len = int(prob * 50) # Scale: 50 chars = 100%
        bar = '#' * bar_len
        print(f"  {label}: {bar} ({prob*100:.1f}%)")

    print('=' * 60)


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="NHL Model Training and Prediction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Train with filtered data (recommended)
  python3 model_test.py --db ./nhl_analytics.db --mode train --epochs 400

  # Train with ALL data (old behavior, expect missing features)
  python3 model_test.py --db ./nhl_analytics.db --mode train --no-filter --epochs 400

  # Make a prediction
  python3 model_test.py --mode manual --home TOR --away BOS --date 2025-01-15
        """
    )
    p.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to database")
    p.add_argument("--mode", required=True, choices=['train', 'manual'], help="Mode: train or manual forecast")
    p.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS, help=f"Training epochs (default: {DEFAULT_EPOCHS})")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH, help=f"Batch size (default: {DEFAULT_BATCH})")
    p.add_argument("--lr", type=float, default=DEFAULT_LR, help=f"Learning rate (default: {DEFAULT_LR})")
    p.add_argument("--hidden", type=int, default=DEFAULT_HIDDEN, help=f"Hidden layer size (default: {DEFAULT_HIDDEN})")
    p.add_argument("--no-filter", dest='use_filter', action='store_false',
                   help="Disable complete games filter (use ALL games, expect missing data)")
    p.add_argument("--home", type=str, help="Home team abbreviation (manual mode)")
    p.add_argument("--away", type=str, help="Away team abbreviation (manual mode)")
    p.add_argument("--h_odds", type=str, default="-110", help="Home team odds (default: -110)")
    p.add_argument("--a_odds", type=str, default="-110", help="Away team odds (default: -110)")
    p.add_argument("--n_sims", type=int, default=DEFAULT_N_SIMS, help=f"Monte Carlo simulations (default: {DEFAULT_N_SIMS})")
    
    # NEW: Goalie override arguments
    p.add_argument("--home-goalie", type=str, help="Home team starting goalie (manual mode)")
    p.add_argument("--away-goalie", type=str, help="Away team starting goalie (manual mode)")

    # Calibration toggle
    p.add_argument("--no-calibration", dest='use_calibration', action='store_false',
                   help="Disable calibration and use raw model predictions (manual mode)")

    # Feature set toggle
    p.add_argument("--pruned", dest='use_pruned', action='store_true',
                   help="Use pruned high-signal feature set instead of the full set")

    # Optimizer toggle
    p.add_argument("--val-start", dest='val_start', default=None,
                   help="Walk-forward: train on games before this date (YYYY-MM-DD), validate from it")
    p.add_argument("--val-end", dest='val_end', default=None,
                   help="Walk-forward: end of validation window (YYYY-MM-DD, exclusive); games after are dropped")
    p.add_argument("--dump-preds", dest='dump_preds', default=None,
                   help="Write val-set predictions (game_id, pred/actual goals) to this CSV for external backtests")
    p.add_argument("--optimizer", choices=['sgd', 'adam'], default='sgd',
                   help="Training optimizer (default: sgd)")

    p.add_argument("--override", action='store_true',
                   help="manual mode: price with the lineup-override model (seeds ovr_s101/202/303 averaged; "
                        "shrunk form + lineup projections + override; priors override_roll_priors.json)")
    p.add_argument("--offline", action='store_true',
                   help="manual mode: skip the pre-game check (no lineup feed / freshness check) — prices off the DB only")
    p.add_argument("--allow-stale", dest='allow_stale', action='store_true',
                   help="manual mode: price even if the DB is missing these teams' completed games")
    rest_days_args = p.add_mutually_exclusive_group()
    rest_days_args.add_argument("--date", type=str, help="calculate rest-days from Game date YYYY-MM-DD (manual mode)")
    rest_days_args.add_argument("--today", dest="date", action="store_const", const=str(datetime.datetime.now().date()), help="use today's date for rest-diff calculations")
    rest_days_args.add_argument("--rest", type=int, nargs=2, default=[2,2], help="number of rest days - home/away (default: 2/2)")

    p.set_defaults(use_filter=True, use_calibration=True, use_pruned=False)
    a = p.parse_args()
    
    home_goalie_id = None
    away_goalie_id = None
    db_conn = _connect(a.db)
    if (a.home_goalie is not None): home_goalie_id = lookup_player_id(a.home_goalie, db_conn);
    if (a.away_goalie is not None): away_goalie_id = lookup_player_id(a.away_goalie, db_conn);
    db_conn.close()
    
    if a.mode == 'train':
        print("\n" + "=" * 70)
        print("NHL MODEL TRAINING")
        print("=" * 70)
        print(f"Database: {a.db}")
        print(f"Complete games filter: {'ENABLED' if a.use_filter else 'DISABLED'}")
        print(f"Feature set: {'PRUNED' if a.use_pruned else 'FULL'} (exact feature count printed at training start)")
        print(f"Epochs: {a.epochs} | Batch: {a.batch} | LR: {a.lr} | Hidden: {a.hidden} | Optimizer: {a.optimizer.upper()}")
        print("=" * 70 + "\n")

        train(a.db, a.epochs, a.batch, a.lr, a.hidden, RANDOM_SEED, use_complete_games_filter=a.use_filter, use_pruned_features=a.use_pruned, use_adam=(a.optimizer == 'adam'), val_start=a.val_start, val_end=a.val_end, dump_preds=a.dump_preds)
    elif a.mode == 'manual':
        tags = None
        if a.override:   # same feature modes the override models were trained with
            import json as _json
            ROLL_MODE = 'shrink'
            _ROLL_PRIORS = _json.load(open('override_roll_priors.json'))
            PLAYER_PROJ = True
            LINEUP_OVERRIDE = True
            tags = ['ovr_s101', 'ovr_s202', 'ovr_s303']
            print("OVERRIDE MODEL: shrunk form + lineup projections + lineup override | 3-seed ensemble")
        ctx = None
        if not a.offline:
            import pregame_query
            ctx = pregame_query.build_context(a.db, a.date or str(datetime.date.today()), a.away, a.home,
                                        allow_stale=a.allow_stale)
            if ctx is None:
                raise SystemExit(1)
        manual_forecast(a.db, a.home, a.away, a.date, a.rest[0], a.rest[1],
                       a.h_odds, a.a_odds, a.n_sims,
                       home_goalie_id=home_goalie_id,
                       away_goalie_id=away_goalie_id,
                       use_calibration=a.use_calibration,
                       pregame=ctx, ensemble_tags=tags)
