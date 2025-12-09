import pandas as pd
import numpy as np
import argparse
import sqlite3
import os
import random
import datetime

import jax
import jax.numpy as jnp

# Everything in this file sucks ass
# TODO: make it happen

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


# ---------------------------
# Config & Constants
# ---------------------------
DEFAULT_DB_PATH = "./nhl_analytics.db"
MODEL_PARAMS_PATH = "advanced_model_params_v6.npz"
STATS_PATH = "advanced_standardize_stats_v6.npz"
RANDOM_SEED = None

DEFAULT_EPOCHS = 600
DEFAULT_BATCH = 64
DEFAULT_LR = 0.0005
DEFAULT_HIDDEN = 128
DEFAULT_N_SIMS = 5000

EMPTY_NET_MULTIPLIER_FOR = 5.0
EMPTY_NET_MULTIPLIER_AGAINST = 2.5

# ---------------------------
# Global Helpers
# ---------------------------
def norm(s):
    return s.replace('.', '').upper()

# ---------------------------
# Situation Helper
# ---------------------------
def get_situation_id(con):
    res = pd.read_sql_query("SELECT situation_id FROM situations WHERE LOWER(situation_code) LIKE '%all%' LIMIT 1", con)
    return int(res.iloc[0]['situation_id']) if not res.empty else 2


# ---------------------------
# Sketchy at best
# ---------------------------
def process_special_teams(con) -> pd.DataFrame:
    query = """
    SELECT game_id, team_id, situation_code,
           SUM(COALESCE(mp_score_venue_adjusted_xgoals_for, mp_xgoals_for, 0)) as xg_for,
           SUM(COALESCE(mp_score_venue_adjusted_xgoals_against, mp_xgoals_against, 0)) as xg_against,
           SUM(mp_ice_time) as toi
    FROM mp_team_game_stats mp
    JOIN situations s ON mp.situation_id = s.situation_id
    WHERE s.situation_code IN ('PP', 'PK')
    GROUP BY game_id, team_id, situation_code
    """
    df = pd.read_sql_query(query, con)
    if df.empty:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_pp_xg60', 'roll_pk_xga60', 'roll_pk_xgf60'])

    pp = df[df['situation_code'] == 'PP'].copy()
    pk = df[df['situation_code'] == 'PK'].copy()

    # Calculate Rates
    pp['pp_xg60'] = pp['xg_for'] / (pp['toi'] / 60 + 0.2)
    pk['pk_xga60'] = pk['xg_against'] / (pk['toi'] / 60 + 0.2)
    pk['pk_xgf60'] = pk['xg_for'] / (pk['toi'] / 60 + 0.2)

    pp = pp.sort_values(['team_id', 'game_id'])
    pk = pk.sort_values(['team_id', 'game_id'])

    # Rolling
    pp['roll_pp_xg60'] = pp.groupby('team_id')['pp_xg60'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())
    pk['roll_pk_xga60'] = pk.groupby('team_id')['pk_xga60'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())
    pk['roll_pk_xgf60'] = pk.groupby('team_id')['pk_xgf60'].transform(lambda x: x.shift(1).rolling(8, min_periods=1).mean())

    # Merge
    out = pd.merge(pp[['game_id', 'team_id', 'roll_pp_xg60']],
                   pk[['game_id', 'team_id', 'roll_pk_xga60', 'roll_pk_xgf60']],
                   on=['game_id', 'team_id'], how='outer').fillna(0.0)
                   
    # Fill defaults if missing (league avg approx)
    if out['roll_pp_xg60'].mean() == 0: out['roll_pp_xg60'] = 7.0
    
    return out

# Use NST goalie data (goalie_game_stats) with MoneyPuck fallback
# NST has better coverage overall, but MP fills gaps for some games
def process_goalie_metrics(con) -> pd.DataFrame:
    all_id = get_situation_id(con)

    # Get per-goalie data (needed for GHSF calculation)
    nst_query = f"""
    SELECT game_id, team_id, player_id,
           COALESCE(expected_goals_against - goals_against, 0) as gsax
    FROM goalie_game_stats WHERE situation_id = {all_id}
    """
    nst_df = pd.read_sql_query(nst_query, con)

    # Get MP goalie data as fallback
    mp_query = f"""
    SELECT game_id, team_id, player_id,
           COALESCE(mp_xgoals_against - mp_goals_against, 0) as gsax
    FROM mp_goalie_game_stats WHERE situation_id = {all_id}
    """
    mp_df = pd.read_sql_query(mp_query, con)

    # Combine: use NST where available, fall back to MP
    if not nst_df.empty and not mp_df.empty:
        nst_df['source'] = 'NST'
        mp_df['source'] = 'MP'
        df = pd.concat([nst_df, mp_df]).drop_duplicates(subset=['game_id', 'team_id', 'player_id'], keep='first')
    elif not nst_df.empty:
        df = nst_df
    elif not mp_df.empty:
        df = mp_df
    else:
        return pd.DataFrame(columns=['game_id', 'team_id', 'roll_gsax', 'ghsf'])

    # Sort by player and game for per-goalie rolling calculations
    df = df.sort_values(['player_id', 'game_id'])
    grp_goalie = df.groupby('player_id')

    # GHSF (Goalie Hot Streak Factor): Trend / Volatility
    # Calculate recent 3-game vs prior 3-game trend
    df['gsax_recent3'] = grp_goalie['gsax'].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    df['gsax_prior3'] = grp_goalie['gsax'].transform(lambda x: x.shift(4).rolling(3, min_periods=1).mean())
    df['gsax_trend'] = df['gsax_recent3'] - df['gsax_prior3']

    # Calculate volatility (std dev over last 5 games)
    df['gsax_volatility'] = grp_goalie['gsax'].transform(lambda x: x.shift(1).rolling(5, min_periods=2).std())

    # GHSF = Trend / (Volatility + epsilon)
    df['ghsf'] = df['gsax_trend'] / (df['gsax_volatility'] + 0.1)
    df['ghsf'] = df['ghsf'].fillna(0.0).clip(-5, 5)  # Cap extreme values

    # Aggregate to team level (primary goalie's metrics)
    # Use the goalie with most recent action or highest TOI if available
    team_df = df.sort_values(['team_id', 'game_id']).groupby(['game_id', 'team_id']).first().reset_index()

    # Standard team-level rolling GSAx
    team_df = team_df.sort_values(['team_id', 'game_id'])
    team_df['roll_gsax'] = team_df.groupby('team_id')['gsax'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()
    )
    team_df['roll_gsax'] = team_df['roll_gsax'].fillna(0.0)

    return team_df[['game_id', 'team_id', 'roll_gsax', 'ghsf']]



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
           COALESCE(mp_shot_attempts_against, 0) as corsi_against_raw
    FROM mp_team_game_stats
    WHERE situation_id = {all_id}
    """
    df = pd.read_sql_query(query, con)
    
    new_features = ['roll_hd_shot_pct', 'roll_sh_pct', 'roll_rebound_xgf', 'roll_fo_pct', 'roll_sa_corsi_pct',
                    'roll_freeze_ag', 'roll_pen_diff',
                    'roll_flurry_delta', 'roll_hd_finish_pct', 'roll_hd_save_pct', 
                    'roll_md_finish_pct', 'roll_md_save_pct', 'roll_block_rate']

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
            'roll_block_rate': 'block_rate'
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
    # 1. Fetch raw shot data (All situations or 5v5? Let's use All for volume)
    # Using period <= 4 (Regulation + OT)
    shots_query = """
    SELECT game_id, team_code,
           shot_distance,
           shot_angle,
           shot_type,
           shot_generated_rebound,
           shot_rush,
           off_wing,
           shot_was_on_goal,
           goal
    FROM mp_shots
    WHERE period <= 4
    """
    df_shots = pd.read_sql_query(shots_query, con)

    if df_shots.empty:
        return pd.DataFrame(columns=['game_id', 'team_id'])

    # 2. Map Teams
    team_map = get_team_map(con)
    df_shots['team_code_clean'] = df_shots['team_code'].astype(str).str.upper().str.strip()
    df_shots['team_id'] = df_shots['team_code_clean'].map(team_map)
    df_shots = df_shots.dropna(subset=['team_id'])
    df_shots['team_id'] = df_shots['team_id'].astype(int)

    # 3. Create Indicators
    df_shots['is_wrist'] = (df_shots['shot_type'] == 'WRIST').astype(int)
    df_shots['is_slap'] = (df_shots['shot_type'] == 'SLAP').astype(int)
    df_shots['is_snap'] = (df_shots['shot_type'] == 'SNAP').astype(int)
    df_shots['is_backhand'] = (df_shots['shot_type'] == 'BACK').astype(int)
    df_shots['is_tip'] = (df_shots['shot_type'] == 'TIP').astype(int) # 'DEFL' or 'TIP'? MP uses 'TIP' usually. 
    # Check distinct shot_type if unsure, but this is a good start.

    # 4. Aggregate
    df_agg = df_shots.groupby(['game_id', 'team_id']).agg(
        shots_total=('shot_distance', 'count'),
        avg_dist=('shot_distance', 'mean'),
        avg_angle=('shot_angle', lambda x: x.abs().mean()),
        cnt_wrist=('is_wrist', 'sum'),
        cnt_slap=('is_slap', 'sum'),
        cnt_snap=('is_snap', 'sum'),
        cnt_backhand=('is_backhand', 'sum'),
        cnt_tip=('is_tip', 'sum'),
        cnt_rebound=('shot_generated_rebound', 'sum'),
        cnt_rush=('shot_rush', 'sum'),
        cnt_off_wing=('off_wing', 'sum')
    ).reset_index()

    # We do NOT return rolling averages here anymore. We return RAW stats.
    # The central manager will handle rolling.
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
        lambda x: (x['mp_i_f_goals'] - x['mp_i_f_xgoals']).sum()
    ).reset_index(name='top3_f_finish')
    
    # Merge all
    out = top3f
    out = pd.merge(out, top2d, on=['game_id', 'team_id'], how='outer')
    out = pd.merge(out, bot6f, on=['game_id', 'team_id'], how='outer')
    out = pd.merge(out, top3f_finish, on=['game_id', 'team_id'], how='outer')
    
    return out.fillna(0)


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


# --- New: Define all expected edge columns for consistency ---
EVENT_TYPES = ['hit', 'giveaway', 'takeaway', 'blocked_shot', 'missed_shot']
ZONES = ['d', 'n', 'o', 'u'] # d=defensive, n=neutral, o=offensive, u=unknown

ALL_EXPECTED_EDGE_COLUMNS = []
for event_type in EVENT_TYPES:
    for zone in ZONES:
        ALL_EXPECTED_EDGE_COLUMNS.append(f"edge_{event_type}_{zone}")
# --- End new section ---

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


# ---------------------------
# Data Prep
# ---------------------------
def get_complete_games(con):
    """
    Return only game_ids with complete data in all required tables.
    This ensures training data has no missing features.
    """
    ALL_ID = get_situation_id(con)

    query = f"""
    SELECT DISTINCT g.game_id
    FROM games g
    -- Require MoneyPuck team data for All situation (both teams)
    WHERE EXISTS (
        SELECT 1 FROM mp_team_game_stats mp_all
        WHERE mp_all.game_id = g.game_id
        AND mp_all.team_id = g.home_team_id
        AND mp_all.situation_id = {ALL_ID}
    )
    AND EXISTS (
        SELECT 1 FROM mp_team_game_stats mp_all
        WHERE mp_all.game_id = g.game_id
        AND mp_all.team_id = g.away_team_id
        AND mp_all.situation_id = {ALL_ID}
    )
    -- Require MoneyPuck PP data (at least one team)
    AND EXISTS (
        SELECT 1 FROM mp_team_game_stats mp_pp
        JOIN situations s ON mp_pp.situation_id = s.situation_id
        WHERE mp_pp.game_id = g.game_id
        AND s.situation_code = 'PP'
    )
    -- Require MoneyPuck PK data (at least one team)
    AND EXISTS (
        SELECT 1 FROM mp_team_game_stats mp_pk
        JOIN situations s ON mp_pk.situation_id = s.situation_id
        WHERE mp_pk.game_id = g.game_id
        AND s.situation_code = 'PK'
    )
    -- Require NST team_game_overview data
    AND EXISTS (
        SELECT 1 FROM team_game_overview nst
        WHERE nst.game_id = g.game_id
        AND nst.situation_id = {ALL_ID}
    )
    -- Require shot data (minimum 20 shots total)
    AND EXISTS (
        SELECT 1 FROM (
            SELECT game_id, COUNT(*) as shot_count
            FROM mp_shots
            WHERE game_id = g.game_id
            GROUP BY game_id
            HAVING COUNT(*) >= 20
        ) shots
        WHERE shots.game_id = g.game_id
    )
    -- Require goalie data for both teams (NST preferred, MP fallback)
    AND EXISTS (
        SELECT 1 FROM (
            SELECT game_id, COUNT(DISTINCT team_id) as team_count
            FROM (
                -- NST goalie data
                SELECT game_id, team_id FROM goalie_game_stats
                WHERE game_id = g.game_id AND situation_id = {ALL_ID}
                UNION
                -- MP goalie data as fallback
                SELECT game_id, team_id FROM mp_goalie_game_stats
                WHERE game_id = g.game_id AND situation_id = {ALL_ID}
            ) combined_goalies
            GROUP BY game_id
            HAVING COUNT(DISTINCT team_id) = 2
        ) goalies
        WHERE goalies.game_id = g.game_id
    )
    """

    result = pd.read_sql_query(query, con)
    return result['game_id'].tolist()


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
            game_filter_clause = ""
        else:
            # Build SQL IN clause
            game_ids_str = "', '".join(complete_games)
            game_filter_clause = f"WHERE game_id IN ('{game_ids_str}')"
    else:
        print("⚠️  Running WITHOUT complete games filter (expect missing data)")
        game_filter_clause = ""

    base = f"""
    WITH teamsplit AS (
        SELECT game_id, home_team_id AS team_id, 'HOME' AS side, game_date
        FROM games
        {game_filter_clause}
        UNION ALL
        SELECT game_id, away_team_id AS team_id, 'AWAY' AS side, game_date
        FROM games
        {game_filter_clause}
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

    for func in [process_special_teams, process_goalie_metrics, process_nst_metrics,
                  process_shot_metrics, process_advanced_metrics, process_edge_metrics,
                  process_opposition_adjusted_xg, process_linemate_synergy]:
        extra = func(con)
        if not extra.empty:
            # if func.__name__ == 'process_edge_metrics':
            #     pass
            
            df = pd.merge(df, extra, on=['game_id', 'team_id'], how='left')

    con.close()
    df = df.fillna(0);
    assert(df is not None), "dataframe got nuked";
    df = df.sort_values(['team_id', 'mp_game_date'])

    grp = df.groupby('team_id')
    for c in ['xgf', 'xga', 'pens', 'goals_for']:
        # Standard 10-game rolling
        df[f'roll_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
        
        # Trend rolling (3 and 20)
        df[f'roll3_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(3, min_periods=1).mean())
        df[f'roll20_{c}'] = grp[c].transform(lambda x: x.shift(1).rolling(20, min_periods=1).mean())
        
        # Fill NaNs
        league_avg = df[c].mean()
        for prefix in ['roll_', 'roll3_', 'roll20_']:
            df[f'{prefix}{c}'] = df[f'{prefix}{c}'].fillna(league_avg if not np.isnan(league_avg) else 0.0)

    df['prev_date'] = grp['mp_game_date'].shift(1)
    df['rest_days'] = (df['mp_game_date'] - df['prev_date']).dt.days.fillna(2).clip(0, 10)

    home = df[df['side'] == 'HOME'].rename(columns=lambda c: f"home_{c}" if c not in ['game_id'] else c)
    away = df[df['side'] == 'AWAY'].rename(columns=lambda c: f"away_{c}" if c not in ['game_id'] else c)

    home = home.rename(columns={'home_goals_for': 'goals_home', 'home_rest_days': 'home_rest'})
    away = away.rename(columns={'away_goals_for': 'goals_away', 'away_rest_days': 'away_rest'})

    final = pd.merge(home, away, on='game_id')
    # final['rest_diff'] = final['home_rest'] - final['away_rest']

    # STED (Special Teams Efficiency Differential): Matchup-specific ST advantage
    # Only calculate if special teams columns exist
    if 'home_roll_pp_xg60' in final.columns and 'away_roll_pk_xga60' in final.columns:
        final['home_sted'] = (
            (final['home_roll_pp_xg60'] - final['away_roll_pk_xga60']) +
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

    # Keep home_mp_game_date for forecasting (renamed from mp_game_date during home_ prefix)
    drop = [c for c in final.columns if any(x in c for x in ['side', 'prev_date', 'team_id_x', 'team_id_y', 'away_mp_game_date'])]
    final = final.drop(columns=drop, errors='ignore')

    # Rename home_mp_game_date back to mp_game_date for consistency
    if 'home_mp_game_date' in final.columns:
        final = final.rename(columns={'home_mp_game_date': 'mp_game_date'})

    # DEBUG: Check edge_giveaway_d for team 12
    if 'away_edge_giveaway_d' in final.columns:
        edm_debug = final[final['away_team_id'] == 12]
        if not edm_debug.empty:
            print(f"DEBUG: get_base_team_stats away_edge_giveaway_d for team 12:\n{edm_debug[['game_id', 'mp_game_date', 'away_edge_giveaway_d']].tail()}")

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
    b3 = jnp.full(2, 3.0)
    return {'W1': W1, 'b1': b1, 'W2': W2, 'b2': b2, 'W3': W3, 'b3': b3}


@jax.jit
def forward(p, x):
    h = jax.nn.elu(x @ p['W1'] + p['b1'])
    h = jax.nn.elu(h @ p['W2'] + p['b2'])
    return jax.nn.softplus(h @ p['W3'] + p['b3']) + 1e-6


@jax.jit
def loss_fn(p, x, y):
    lam = forward(p, x)
    lam = jnp.clip(lam, 0.2, 30.0)
    return jnp.mean(lam - y * jnp.log(lam)) + 2e-5 * jnp.sum(p['W3'] ** 2)


update = jax.jit(lambda p, x, y, lr: ({k: p[k] - lr * jax.grad(loss_fn)(p, x, y)[k] for k in p}, loss_fn(p, x, y)))


def get_features(df):
    exclude = ['game_id', 'goals_home', 'goals_away', 'h_odd', 'a_odd', 'mp_game_date',
               'home_ghsf', 'away_ghsf', 'home_lss', 'away_lss', 'home_sted', 'away_sted']
    return [c for c in df.columns if c not in exclude]


# ---------------------------
# Train & Forecast
# ---------------------------
def train(db, epochs, batch, lr, hidden, seed, use_complete_games_filter=True):
    print("preparing training data...")
    print(f"game filtering: {'ENABLED' if use_complete_games_filter else 'DISABLED'}\n")
    df = prepare_training_data(db, use_complete_games_filter=use_complete_games_filter)
    if df.empty:
        print("No data!")
        return
    
    if seed is None: seed = random.randint(0, 2**32);
    print(f"[RANDOM_SEED: {seed}]")
    print("beginning training...\n\n")
    
    feats = get_features(df)
    print("using features: "); print(feats);
    X_df = standardize_data(df, feats, STATS_PATH, 'train')
    X = jnp.array(X_df.values)
    Y = jnp.array(df[['goals_home', 'goals_away']].values)

    steps = max(1, len(X) // batch)
    print(f"\nTraining on {len(X)} games | {len(feats)} features")

    key = jax.random.PRNGKey(seed)
    params = init_params(key, len(feats), hidden)

    for e in range(epochs):
        perm = jax.random.permutation(key, len(X))
        X, Y = X[perm], Y[perm]
        loss_sum = 0.0
        for i in range(steps):
            xb = X[i * batch:(i + 1) * batch]
            yb = Y[i * batch:(i + 1) * batch]
            params, l = update(params, xb, yb, lr)
            loss_sum += l
        if e % 100 == 0 or e == epochs - 1:
            print(f"Epoch {e:3d} | Loss {loss_sum / steps:.4f}")

    print("saving...")
    np.savez(MODEL_PARAMS_PATH, **{k: np.array(v) for k, v in params.items()})
    np.savez("feature_list.npz", features=np.array(feats))
    print("Model saved.")


def american_to_prob(o):
    o = float(o)
    if o > 0:
        return 100 / (o + 100)
    else:
        return abs(o) / (abs(o) + 100)


def get_latest_stats_for_manual(db_path):
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
    return latest


def manual_forecast(db, home_abbr, away_abbr, date_str, h_rest, a_rest, h_odd, a_odd, n_sims):
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
    print("\n")

    matchup = {}
    for f in feats:
        if f.startswith('home_'):
            matchup[f] = h_row.get(f[len('home_'):], 0)
        elif f.startswith('away_'):
            matchup[f] = a_row.get(f[len('away_'):], 0)
        elif f == 'home_rest':
            matchup[f] = h_rest
        elif f == 'away_rest':
            matchup[f] = a_rest
        # elif f == 'rest_diff':
        #    matchup[f] = h_rest - a_rest
        elif f == 'home_prob':
            matchup[f] = american_to_prob(h_odd or -110)
        elif f == 'away_prob':
            matchup[f] = american_to_prob(a_odd or -110)
        else:
            matchup[f] = 0.0
    
    print("matchup")
    for (k,v) in matchup.items():
      print(f"  {k}: {v}")
    print("\n")

    X = standardize_data(pd.DataFrame([matchup])[feats], feats, STATS_PATH, 'predict')
    lam = forward(params, jnp.array(X.values))[0]
    lh, la = float(lam[0]), float(lam[1])

    print(f"\nProjected Rates → {h_row['team_abbr']} {lh:.2f} | {a_row['team_abbr']} {la:.2f}\n")
    print(f"Simulating {n_sims:,} games...\n{'=' * 60}")

    # according to Gemini:
    # [ha]60 represents the first 40 minutes of the game
    # [ha]18 represents the next 18 minutes
    h60 = np.random.poisson(lh * 0.6667, n_sims)
    a60 = np.random.poisson(la * 0.6667, n_sims)
    h18 = np.random.poisson(lh * 0.3333, n_sims)
    a18 = np.random.poisson(la * 0.3333, n_sims)

    cur_h = h60 + h18
    cur_a = a60 + a18
    diff = cur_h - cur_a

    # final two minutes of the game, where goalies may be pulled
    rh = np.full(n_sims, lh * 0.0333)
    ra = np.full(n_sims, la * 0.0333)

    pull_h = (diff >= -3) & (diff < 0)
    pull_a = (diff <= 3) & (diff > 0)

    rh[pull_a] *= EMPTY_NET_MULTIPLIER_FOR
    ra[pull_a] *= EMPTY_NET_MULTIPLIER_AGAINST
    rh[pull_h] *= EMPTY_NET_MULTIPLIER_AGAINST
    ra[pull_h] *= EMPTY_NET_MULTIPLIER_FOR

    final_h = cur_h + np.random.poisson(rh)
    final_a = cur_a + np.random.poisson(ra)
    total = final_h + final_a

    print(f"{h_row['team_abbr']} {np.mean(final_h):.2f} – {a_row['team_abbr']} {np.mean(final_a):.2f}  |  Total {np.mean(total):.2f}")
    print(f"Win Probability → {h_row['team_abbr']}: {100 * np.mean(final_h > final_a):.1f}%   |   {a_row['team_abbr']}: {100 * np.mean(final_a > final_h):.1f}%  |  ties: {100 * np.mean(final_a == final_h):.1f}%")
    print(f"Puckline (-1.5) → {h_row['team_abbr']}: {100 * np.mean(final_h - final_a >= 2):.1f}%   |   {a_row['team_abbr']}: {100 * np.mean(final_a - final_h >= 2):.1f}%")
    print(f"Over 6.5: {100 * np.mean(total > 6.5):.1f}%   |   Under 6.5: {100 * np.mean(total <= 6.5):.1f}%")
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
    
    rest_days_args = p.add_mutually_exclusive_group()
    rest_days_args.add_argument("--date", type=str, help="calculate rest-days from Game date YYYY-MM-DD (manual mode)")
    rest_days_args.add_argument("--today", dest="date", action="store_const", const=str(datetime.datetime.now().date()), help="use today's date for rest-diff calculations")
    rest_days_args.add_argument("--rest", type=int, nargs=2, default=[2,2], help="number of rest days - home/away (default: 2/2)")

    p.set_defaults(use_filter=True)
    a = p.parse_args()

    if a.mode == 'train':
        print("\n" + "=" * 70)
        print("NHL MODEL TRAINING")
        print("=" * 70)
        print(f"Database: {a.db}")
        print(f"Complete games filter: {'ENABLED' if a.use_filter else 'DISABLED'}")
        print(f"Epochs: {a.epochs} | Batch: {a.batch} | LR: {a.lr} | Hidden: {a.hidden}")
        print("=" * 70 + "\n")

        train(a.db, a.epochs, a.batch, a.lr, a.hidden, RANDOM_SEED, use_complete_games_filter=a.use_filter)
    elif a.mode == 'manual':
        manual_forecast(a.db, a.home, a.away, a.date, a.rest[0], a.rest[1], a.h_odds, a.a_odds, a.n_sims)
