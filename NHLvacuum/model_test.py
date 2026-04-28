import pandas as pd
import numpy as np
import argparse
import sqlite3
import os
import random
import datetime

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
RANDOM_SEED = 4237426529  # Best performer from random seed testing (val loss -0.4517)

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

    # Calculate Rates
    pp['pp_xg60'] = pp['xg_for'] / (pp['toi'] / 60 + 0.2)
    pp['pp_efficiency'] = pp['goals_for'] / (pp['toi'] / 60 + 0.2) # PP Goals per 60
    
    pk['pk_xga60'] = pk['xg_against'] / (pk['toi'] / 60 + 0.2)
    pk['pk_xgf60'] = pk['xg_for'] / (pk['toi'] / 60 + 0.2)
    pk['pk_g60'] = pk['goals_against'] / (pk['toi'] / 60 + 0.2) # PK Goals Against per 60

    pp = pp.sort_values(['team_id', 'game_id'])
    pk = pk.sort_values(['team_id', 'game_id'])

    # Rolling
    pp['roll_pp_xg60'] = pp.groupby('team_id')['pp_xg60'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())
    pp['roll_pp_efficiency'] = pp.groupby('team_id')['pp_efficiency'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())
    
    pk['roll_pk_xga60'] = pk.groupby('team_id')['pk_xga60'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())
    pk['roll_pk_xgf60'] = pk.groupby('team_id')['pk_xgf60'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())
    pk['roll_pk_g60'] = pk.groupby('team_id')['pk_g60'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())

    # Merge
    out = pd.merge(pp[['game_id', 'team_id', 'roll_pp_xg60', 'roll_pp_efficiency']],
                   pk[['game_id', 'team_id', 'roll_pk_xga60', 'roll_pk_xgf60', 'roll_pk_g60']],
                   on=['game_id', 'team_id'], how='outer').fillna(0.0)
                   
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

    # 6. Rolling averages (PER GOALIE)
    df['roll_gsax'] = grp_goalie['gsax'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )
    df['roll_hd_gsax'] = grp_goalie['hd_gsax'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )
    df['roll_rcr'] = grp_goalie['rcr'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )
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
    
    # Default values for new goalies
    LEAGUE_AVG_RCR = 0.82
    LEAGUE_AVG_FATIGUE = 150.0
    
    df['roll_gsax'] = df['roll_gsax'].fillna(0.0)
    df['roll_hd_gsax'] = df['roll_hd_gsax'].fillna(0.0)
    df['roll_rcr'] = df['roll_rcr'].fillna(LEAGUE_AVG_RCR)
    df['roll_fatigue_index'] = df['roll_fatigue_index'].fillna(LEAGUE_AVG_FATIGUE)
    
    # Enforce defaults for first 5 games (unstable)
    mask_new = df['games_played_cum'] < 5
    df.loc[mask_new, 'roll_hd_gsax'] = 0.0
    df.loc[mask_new, 'roll_rcr'] = LEAGUE_AVG_RCR
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

    # Sort and calculate rolling average
    df = df.sort_values(['team_id', 'game_id'])
    grp = df.groupby('team_id')

    # Standard 10-game rolling
    df['roll_hdcf_share'] = grp['hdcf_share'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )

    # HDSM (High-Danger Shot Momentum): 3-game vs 10-game differential
    df['roll3_hdcf_share'] = grp['hdcf_share'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean()
    )
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

    df = df.sort_values(['team_id', 'game_id'])
    grp = df.groupby('team_id')

    # Rolling averages
    df['roll_hd_shot_pct'] = grp['hd_shot_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_sh_pct'] = grp['sh_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_rebound_xgf'] = grp['rebound_xgf'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_fo_pct'] = grp['fo_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_sa_corsi_pct'] = grp['sa_corsi_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    
    # New features rolling
    df['roll_freeze_ag'] = grp['freeze_ag'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_pen_diff'] = grp['pen_diff'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    
    # Added advanced features rolling
    df['roll_flurry_delta'] = grp['flurry_delta'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_hd_finish_pct'] = grp['hd_finish_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_hd_save_pct'] = grp['hd_save_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_md_finish_pct'] = grp['md_finish_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_md_save_pct'] = grp['md_save_pct'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_block_rate'] = grp['block_rate'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    
    # Pressure Rolling
    df['roll_pressure_rate'] = grp['pressure_rate'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    df['roll_dzone_clearance_rate'] = grp['dzone_clearance_rate'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    # --- Trend Features (Short & Long Term) ---
    # Short term (Last 3) - Hot/Cold streaks
    df['roll3_sh_pct'] = grp['sh_pct'].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    df['roll3_sa_corsi_pct'] = grp['sa_corsi_pct'].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    df['roll3_hd_save_pct'] = grp['hd_save_pct'].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    
    # Long term (Last 20) - Structural strength
    df['roll20_sh_pct'] = grp['sh_pct'].transform(lambda x: x.shift(1).rolling(20, min_periods=1).mean())
    df['roll20_sa_corsi_pct'] = grp['sa_corsi_pct'].transform(lambda x: x.shift(1).rolling(20, min_periods=1).mean())
    df['roll20_hd_save_pct'] = grp['hd_save_pct'].transform(lambda x: x.shift(1).rolling(20, min_periods=1).mean())

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
        out[f'roll_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
        
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
    
    out['roll_suppression_factor'] = grp['suppression_factor'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    out['roll_matchup_rate'] = grp['matchup_rate'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    
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
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
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
    teams_map_query = "SELECT NHL_TEAM_ID, team_id FROM teams WHERE NHL_TEAM_ID IS NOT NULL"
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
        print(f"Feature count: {len(feature_cols)}")

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
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    )
    team_games['roll10_win_rate'] = grp['win'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
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


def get_base_team_stats(db_path, use_complete_games_filter=True):
    con = sqlite3.connect(db_path)
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

    for func in [process_special_teams, process_nst_metrics,
                  process_shot_metrics, process_advanced_metrics, process_edge_metrics,
                  process_opposition_adjusted_xg, process_linemate_synergy, process_rush_metrics,
                  process_skater_chemistry, process_matchup_metrics, process_win_rate_features,
                  process_home_ice_features]:
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
    LEAGUE_AVG_RCR = 0.82
    LEAGUE_AVG_FATIGUE = 150.0
    
    for c in ['goalie_roll_gsax', 'goalie_roll_hd_gsax', 'goalie_ghsf']:
        if c in df.columns: df[c] = df[c].fillna(0.0)
        else: df[c] = 0.0
            
    if 'goalie_roll_rcr' in df.columns: df['goalie_roll_rcr'] = df['goalie_roll_rcr'].fillna(LEAGUE_AVG_RCR)
    else: df['goalie_roll_rcr'] = LEAGUE_AVG_RCR
        
    if 'goalie_roll_fatigue_index' in df.columns: df['goalie_roll_fatigue_index'] = df['goalie_roll_fatigue_index'].fillna(LEAGUE_AVG_FATIGUE)
    else: df['goalie_roll_fatigue_index'] = LEAGUE_AVG_FATIGUE

    con.close()
    df = df.fillna(0);
    assert(df is not None), "dataframe got nuked";
    df = df.sort_values(['team_id', 'mp_game_date'])

    grp = df.groupby('team_id')
    for c in ['xgf', 'xga', 'pens', 'goals_for', 'rush_attempts_for', 'avg_dist', 'avg_angle']:
        # Standard 10-game rolling
        df[f'roll_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())

        # Trend rolling (3 and 20)
        df[f'roll3_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
        df[f'roll20_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(20, min_periods=1).mean())

        # Fill NaNs
        league_avg = df[c].mean()
        for prefix in ['roll_', 'roll3_', 'roll20_']:
            df[f'{prefix}{c}'] = df[f'{prefix}{c}'].fillna(league_avg if not np.isnan(league_avg) else 0.0)

    # Roll LSS: 10-game shifted window so it's always pre-game information
    if 'lss' in df.columns:
        df['roll_lss'] = grp['lss'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
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
        df[f'roll3_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())

        # 10-game rolling average
        df[f'roll10_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())

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

    df['prev_date'] = grp['mp_game_date'].shift(1)
    df['rest_days'] = (df['mp_game_date'] - df['prev_date']).dt.days.fillna(2).clip(0, 10)

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


def update_step_adam(params, adam_state, x, y, lr, rng_key, dropout_rate, beta1=0.9, beta2=0.999):
    """Single training step with Adam optimizer."""
    # Compute loss and gradients with dropout enabled
    loss_and_grad = jax.value_and_grad(
        lambda p: loss_fn(p, x, y, training=True, rng_key=rng_key, dropout_rate=dropout_rate)
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

        if base_c in ['rest', 'osa_xg', 'hdsm', 'opp_xg_suppression', 'sted',
                      'goalie_gsax_diff', 'goalie_hd_gsax_diff', 'goalie_quality']:
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
# Train & Forecast
# ---------------------------
def train(db, epochs, batch, lr, hidden, seed, use_complete_games_filter=True, use_pruned_features=False):
    print("preparing training data...")
    print(f"game filtering: {'ENABLED' if use_complete_games_filter else 'DISABLED'}")
    print(f"feature set: {'PRUNED (~40)' if use_pruned_features else 'FULL (~124)'}\n")
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
    val_size = int(len(df) * 0.15)
    train_size = len(df) - val_size

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
        perm = jax.random.permutation(subkey, len(X))
        X, Y = X[perm], Y[perm]
        W_shuffled = W_train[perm]
        loss_sum = 0.0

        # Train Loop
        for i in range(steps):
            xb = X[i * batch:(i + 1) * batch]
            yb = Y[i * batch:(i + 1) * batch]
            wb = W_shuffled[i * batch:(i + 1) * batch]

            # Generate unique random key for dropout in this batch
            key, dropout_key = jax.random.split(key)
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

    np.savez("feature_list.npz", features=np.array(feats))

    # Calculate calibration factor on validation set
    print("\nCalculating calibration factor on validation set...")
    final_params = best_params if best_params is not None else params
    # Forward pass WITHOUT dropout for calibration
    val_predictions = forward(final_params, X_val, training=False, rng_key=None, dropout_rate=0.0)

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
            return f"{player_id} [G]" if is_goalie else player_id

        # Multiple matches - need disambiguation
        print(f"ERROR: Multiple {'goalies' if is_goalie else 'players'} match '{player_name}':")
        for _, row in result.iterrows():
            print(f"    - {row['player_name']} (ID: {row['player_id']})")
        print(f"  Please specify full name, e.g.: --home-goalie \"{result['player_name'].iloc[0]}\"")
        raise ValueError(f"Ambiguous: multiple matches for '{player_name}'")

    player_id = result['player_id'].iloc[0]
    print(f"  Found: {result['player_name'].iloc[0]} ({player_id})")
    return f"{player_id} [G]" if is_goalie else player_id

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

    teams = pd.read_sql_query("SELECT team_id, team_abbr FROM teams", sqlite3.connect(db_path))
    teams['normalized_abbr'] = teams['team_abbr'].apply(lambda x: norm(x))
    latest = pd.merge(teams, latest, on='team_id', how='left').fillna(0)
    latest['mp_game_date'] = pd.to_datetime(latest['mp_game_date'])

    # NEW: Identify primary starter for each team
    # (Goalie who has started the most games in the last 10 games)

    con = sqlite3.connect(db_path)
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
    
    return latest


def manual_forecast(db, home_abbr, away_abbr, date_str, h_rest, a_rest, h_odd, a_odd, n_sims, home_goalie_id=None, away_goalie_id=None, use_calibration=True):
    if not os.path.exists(MODEL_PARAMS_PATH):
        print("No model – train first.")
        return

    norm = lambda s: s.replace('.', '').upper()
    h_norm = norm(home_abbr)
    a_norm = norm(away_abbr)

    params = {k: jnp.array(v) for k, v in np.load(MODEL_PARAMS_PATH).items()}
    feats = np.load("feature_list.npz")['features'].tolist()

    latest = get_latest_stats_for_manual(db)

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

    # Calculate rest days from game date
    if (date_str is not None):
      print(f"\ncalculating rest days from date: {date_str}")
      target = pd.to_datetime(date_str)
      h_rest = min(max((target - h_row['mp_game_date']).days, 0), 10)
      a_rest = min(max((target - a_row['mp_game_date']).days, 0), 10)
    else:
      # Clip manual rest inputs to match training distribution [0, 10]
      h_rest = min(max(h_rest, 0), 10)
      a_rest = min(max(a_rest, 0), 10)
    
    print(f"rest-days (home): {h_rest}")
    print(f"rest-days (away): {a_rest}")
    print(f"rest-days (diff): {h_rest - a_rest}")
    
    # NEW: Determine which goalies to use
    if home_goalie_id is None:
        home_goalie_id = h_row.get('primary_goalie_id', None)
        print(f"Using primary starter for {h_norm}: {home_goalie_id}")
    else:
        print(f"User-specified goalie for {h_norm}: {home_goalie_id}")

    if away_goalie_id is None:
        away_goalie_id = a_row.get('primary_goalie_id', None)
        print(f"Using primary starter for {a_norm}: {away_goalie_id}")
    else:
        print(f"User-specified goalie for {a_norm}: {away_goalie_id}")
        
    # Clean IDs for lookup
    if home_goalie_id: home_goalie_id = str(home_goalie_id).replace(' [G]', '').strip()
    if away_goalie_id: away_goalie_id = str(away_goalie_id).replace(' [G]', '').strip()
        
    print("\n")

    # NEW: Get goalie features for specified goalies
    con = sqlite3.connect(db)
    goalie_features_df = process_goalie_metrics(con)
    con.close()

    # Get latest features for home goalie
    home_goalie_features = pd.DataFrame()
    if home_goalie_id:
        hg_data = goalie_features_df[
            (goalie_features_df['player_id'] == home_goalie_id) &
            (goalie_features_df['team_id'] == h_row['team_id'])
        ].sort_values('game_id')
        if not hg_data.empty:
            home_goalie_features = hg_data.tail(1)
        else:
            print(f"Warning: No data found for home goalie {home_goalie_id}")

    # Get latest features for away goalie
    away_goalie_features = pd.DataFrame()
    if away_goalie_id:
        ag_data = goalie_features_df[
            (goalie_features_df['player_id'] == away_goalie_id) &
            (goalie_features_df['team_id'] == a_row['team_id'])
        ].sort_values('game_id')
        if not ag_data.empty:
            away_goalie_features = ag_data.tail(1)
        else:
            print(f"Warning: No data found for away goalie {away_goalie_id}")

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
            con2 = sqlite3.connect(db)
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
    home_rcr = matchup.get('home_goalie_roll_rcr', 0.82)  # Default to league avg
    away_rcr = matchup.get('away_goalie_roll_rcr', 0.82)

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

    print("matchup")
    for (k,v) in matchup.items():
      print(f"  {k}: {v}")
    print("\n")

    X = standardize_data(pd.DataFrame([matchup])[feats], feats, STATS_PATH, 'predict')
    lam = forward(params, jnp.array(X.values))[0]
    lh_raw, la_raw = float(lam[0]), float(lam[1])

    # Load and apply calibration factors
    if use_calibration and os.path.exists(CALIBRATION_PATH):
        cal_data = np.load(CALIBRATION_PATH)
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

    print(f"Simulating {n_sims:,} games...\n{'=' * 60}")

    # Period scoring: empirical non-EN distribution scaled to 58/60 of the game
    # The model's predicted rate (lambda) includes EN goals from training data.
    # Periods cover 58 min of normal play (96.67% of lambda as base rate).
    # EN covers final ~2 min (3.33% of lambda as base rate) with score-state
    # multipliers modeling the actual goalie-pull effect on top of normal scoring.
    # P2 is historically highest due to short change (bench closer to attacking zone).
    REGULATION_SHARE = 58.0 / 60.0  # 0.9667 - periods' share of the game rate
    EN_BASE_SHARE = 2.0 / 60.0      # 0.0333 - EN phase base (normal 2-min scoring rate)

    con_sim = sqlite3.connect(db)
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

    # Simulate each period independently
    h_p1 = np.random.poisson(lh * p1_wt, n_sims)
    a_p1 = np.random.poisson(la * p1_wt, n_sims)
    h_p2 = np.random.poisson(lh * p2_wt, n_sims)
    a_p2 = np.random.poisson(la * p2_wt, n_sims)
    h_p3 = np.random.poisson(lh * p3_wt, n_sims)
    a_p3 = np.random.poisson(la * p3_wt, n_sims)

    # Score entering EN window (final ~2 min of P3)
    cur_h = h_p1 + h_p2 + h_p3
    cur_a = a_p1 + a_p2 + a_p3
    diff = cur_h - cur_a

    # Empty net phase: final ~2 minutes of regulation
    # Base rate = normal 2-min scoring rate; multipliers model goalie-pull effect
    rh = np.full(n_sims, lh * EN_BASE_SHARE)
    ra = np.full(n_sims, la * EN_BASE_SHARE)

    pull_h = (diff >= -3) & (diff < 0)  # home trailing by 1-3, pulls goalie
    pull_a = (diff <= 3) & (diff > 0)   # away trailing by 1-3, pulls goalie

    rh[pull_a] *= EMPTY_NET_MULTIPLIER_FOR
    ra[pull_a] *= EMPTY_NET_MULTIPLIER_AGAINST
    rh[pull_h] *= EMPTY_NET_MULTIPLIER_AGAINST
    ra[pull_h] *= EMPTY_NET_MULTIPLIER_FOR

    en_h = np.random.poisson(rh)
    en_a = np.random.poisson(ra)

    final_h = cur_h + en_h
    final_a = cur_a + en_a
    total = final_h + final_a

    # --- SIMULATION OVERVIEW ---
    print(f"{h_row['team_abbr']} {np.mean(final_h):.2f} – {a_row['team_abbr']} {np.mean(final_a):.2f}  |  Total {np.mean(total):.2f}")

    win_h = np.mean(final_h > final_a)
    win_a = np.mean(final_a > final_h)
    ties = np.mean(final_h == final_a)

    print(f"Win Probability → {h_row['team_abbr']}: {100 * win_h:.1f}%   |   {a_row['team_abbr']}: {100 * win_a:.1f}%  |  ties: {100 * ties:.1f}%")
    print(f"Puckline (-1.5) → {h_row['team_abbr']}: {100 * np.mean(final_h - final_a >= 2):.1f}%   |   {a_row['team_abbr']}: {100 * np.mean(final_a - final_h >= 2):.1f}%")
    print(f"Over 6.5: {100 * np.mean(total > 6.5):.1f}%   |   Under 6.5: {100 * np.mean(total <= 6.5):.1f}%")

    # 1. Most Likely Scores
    print("\nMost Likely Scores:")
    score_counts = pd.DataFrame({'h': final_h, 'a': final_a}).groupby(['h', 'a']).size().reset_index(name='count')
    score_counts['prob'] = score_counts['count'] / n_sims
    top_scores = score_counts.sort_values('count', ascending=False).head(5)

    for _, row in top_scores.iterrows():
        print(f"  {h_row['team_abbr']} {int(row['h'])} - {int(row['a'])} {a_row['team_abbr']}  ({row['prob']*100:.1f}%)")

    # 2. Projected Period Scoring (from actual simulation arrays)
    sp1_h, sp1_a = np.mean(h_p1), np.mean(a_p1)
    sp2_h, sp2_a = np.mean(h_p2), np.mean(a_p2)
    sp3_h, sp3_a = np.mean(h_p3) + np.mean(en_h), np.mean(a_p3) + np.mean(en_a)

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
                   help="Use pruned feature set (~40 high-signal features instead of ~124)")

    rest_days_args = p.add_mutually_exclusive_group()
    rest_days_args.add_argument("--date", type=str, help="calculate rest-days from Game date YYYY-MM-DD (manual mode)")
    rest_days_args.add_argument("--today", dest="date", action="store_const", const=str(datetime.datetime.now().date()), help="use today's date for rest-diff calculations")
    rest_days_args.add_argument("--rest", type=int, nargs=2, default=[2,2], help="number of rest days - home/away (default: 2/2)")

    p.set_defaults(use_filter=True, use_calibration=True, use_pruned=False)
    a = p.parse_args()
    
    home_goalie_id = None
    away_goalie_id = None
    db_conn = sqlite3.connect(a.db)
    if (a.home_goalie is not None): home_goalie_id = lookup_player_id(a.home_goalie, db_conn);
    if (a.away_goalie is not None): away_goalie_id = lookup_player_id(a.away_goalie, db_conn);
    db_conn.close()
    
    if a.mode == 'train':
        print("\n" + "=" * 70)
        print("NHL MODEL TRAINING")
        print("=" * 70)
        print(f"Database: {a.db}")
        print(f"Complete games filter: {'ENABLED' if a.use_filter else 'DISABLED'}")
        print(f"Feature set: {'PRUNED (~40)' if a.use_pruned else 'FULL (~124)'}")
        print(f"Epochs: {a.epochs} | Batch: {a.batch} | LR: {a.lr} | Hidden: {a.hidden}")
        print("=" * 70 + "\n")

        train(a.db, a.epochs, a.batch, a.lr, a.hidden, RANDOM_SEED, use_complete_games_filter=a.use_filter, use_pruned_features=a.use_pruned)
    elif a.mode == 'manual':
        manual_forecast(a.db, a.home, a.away, a.date, a.rest[0], a.rest[1],
                       a.h_odds, a.a_odds, a.n_sims,
                       home_goalie_id=home_goalie_id,
                       away_goalie_id=away_goalie_id,
                       use_calibration=a.use_calibration)
