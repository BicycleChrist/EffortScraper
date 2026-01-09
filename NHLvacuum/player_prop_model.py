"""
NHL Player Prop Projection Model - Unified System

Follows model_test.py architecture: Direct SQLite access with on-the-fly feature engineering.
No parquet files needed - everything computed from the database.

Props Supported: TOI, Goals, Assists, Points, SOG, Hits, Blocks

Usage:
    # Train TOI model
    python player_prop_model.py --train --prop toi --epochs 300

    # Train goals model (both binary and rate)
    python player_prop_model.py --train --prop goals --epochs 200

    # Predict for upcoming games
    python player_prop_model.py --predict --date 2025-01-15

    # Validate model performance
    python player_prop_model.py --validate --prop goals

Docker (ROCm/JAX):
    docker exec --interactive --tty --user 1000 --workdir /NHLvacuum wooptyROCM \
        python player_prop_model.py --train --prop toi --epochs 300

Author: NHL Analytics Project
Date: 2025-01
"""

import pandas as pd
import numpy as np
import sqlite3
import argparse
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

import jax
import jax.numpy as jnp

print("JAX Devices:", jax.devices())
print("JAX Backend:", jax.default_backend())

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_DB_PATH = "./nhl_analytics.db"
MODEL_DIR = "./"  # Where to save model files

# Training defaults
DEFAULT_EPOCHS_TOI = 300
DEFAULT_EPOCHS_PROP = 200
DEFAULT_BATCH_TOI = 128
DEFAULT_BATCH_PROP = 256
DEFAULT_LR = 0.01
DEFAULT_HIDDEN_TOI = 128
DEFAULT_HIDDEN_PROP = 96

# Minimum TOI filter (seconds) - exclude scratches/limited players
MIN_TOI_SECONDS = 300  # 5 minutes

# Rolling window sizes
ROLL_SHORT = 3
ROLL_MEDIUM = 5
ROLL_STANDARD = 10
ROLL_LONG = 20

PROPS = ['toi', 'goals', 'assists', 'points', 'sog', 'hits', 'blocks']

# =============================================================================
# DATABASE HELPERS
# =============================================================================

def get_situation_id(con, situation_code='All') -> int:
    """Get situation_id for a given situation code."""
    query = f"SELECT situation_id FROM situations WHERE LOWER(situation_code) LIKE '%{situation_code.lower()}%' LIMIT 1"
    res = pd.read_sql_query(query, con)
    return int(res.iloc[0]['situation_id']) if not res.empty else 2


def get_team_map(con) -> Dict[str, int]:
    """Map team abbreviations to team_id."""
    teams_df = pd.read_sql_query("SELECT team_id, team_abbr FROM teams", con)
    mapping = {abbr.upper(): tid for tid, abbr in zip(teams_df['team_id'], teams_df['team_abbr'])}

    # Add common variations
    overrides = {
        'L.A': mapping.get('LA'), 'LAK': mapping.get('LA'),
        'N.J': mapping.get('NJ'), 'NJD': mapping.get('NJ'),
        'S.J': mapping.get('SJ'), 'SJS': mapping.get('SJ'),
        'T.B': mapping.get('TB'), 'TBL': mapping.get('TB'),
        'VGK': mapping.get('VGK'), 'SEA': mapping.get('SEA'),
        'UTA': mapping.get('UTA'), 'ARI': mapping.get('ARI'),
    }
    for k, v in overrides.items():
        if v is not None:
            mapping[k] = v
    return mapping


# =============================================================================
# PLAYER DATA LOADERS
# =============================================================================

def load_player_game_base(con, min_toi: int = MIN_TOI_SECONDS) -> pd.DataFrame:
    """
    Load base player-game data combining MoneyPuck and NST sources.

    Returns DataFrame with one row per player-game with core stats.
    """
    all_id = get_situation_id(con, 'All')

    # MoneyPuck skater data (primary source for most stats)
    mp_query = f"""
    SELECT
        mp.game_id,
        mp.player_id,
        mp.team_id,
        mp.mp_game_date as game_date,
        mp.mp_position as position,
        mp.mp_ice_time as toi_seconds,

        -- Scoring
        COALESCE(mp.mp_i_f_goals, 0) as goals,
        COALESCE(mp.mp_i_f_total_assists, 0) as assists,
        COALESCE(mp.mp_i_f_first_assists, 0) as first_assists,
        COALESCE(mp.mp_i_f_points, 0) as points,

        -- Shooting
        COALESCE(mp.mp_i_f_shots_on_goal, 0) as sog,
        COALESCE(mp.mp_i_f_shots, 0) as shots,
        COALESCE(mp.mp_i_f_xgoals, 0) as ixg,
        COALESCE(mp.mp_i_f_high_danger_shots, 0) as hd_shots,
        COALESCE(mp.mp_i_f_high_danger_xgoals, 0) as hd_xg,
        COALESCE(mp.mp_i_f_rebounds_created, 0) as rebounds_created,

        -- Physical
        COALESCE(mp.mp_i_f_hits, 0) as hits,
        COALESCE(mp.mp_i_f_blocked_shot_attempts, 0) as blocks,
        COALESCE(mp.mp_i_f_giveaways, 0) as giveaways,
        COALESCE(mp.mp_i_f_takeaways, 0) as takeaways,

        -- Faceoffs
        COALESCE(mp.mp_i_f_faceoffs_won, 0) as fo_won,
        COALESCE(mp.mp_i_f_faceoffs_lost, 0) as fo_lost,

        -- On-ice
        COALESCE(mp.mp_onice_f_xgoals, 0) as onice_xgf,
        COALESCE(mp.mp_onice_a_xgoals, 0) as onice_xga

    FROM mp_skater_game_stats mp
    WHERE mp.situation_id = {all_id}
      AND mp.mp_ice_time >= {min_toi}
    """

    df = pd.read_sql_query(mp_query, con)

    if df.empty:
        print("WARNING: No MoneyPuck skater data found!")
        return pd.DataFrame()

    # Parse date
    df['game_date'] = pd.to_datetime(df['game_date'])

    # Clean player_id (remove any suffixes like " [G]")
    df['player_id'] = df['player_id'].astype(str).str.replace(r' \[.*\]', '', regex=True).str.strip()

    # Standardize position to F/D
    df['pos_group'] = df['position'].map({'C': 'F', 'L': 'F', 'R': 'F', 'D': 'D'}).fillna('F')

    print(f"Loaded {len(df):,} player-game records from MoneyPuck")
    print(f"  Players: {df['player_id'].nunique():,}")
    print(f"  Games: {df['game_id'].nunique():,}")
    print(f"  Date range: {df['game_date'].min()} to {df['game_date'].max()}")

    return df


def load_nst_player_stats(con, min_toi: int = MIN_TOI_SECONDS) -> pd.DataFrame:
    """
    Load NST player stats to supplement MoneyPuck data.
    Provides additional metrics like icf, iscf, ihdcf.
    """
    all_id = get_situation_id(con, 'All')

    query = f"""
    SELECT
        game_id,
        player_id,
        team_id,
        toi_seconds,
        COALESCE(ixg, 0) as nst_ixg,
        COALESCE(icf, 0) as icf,
        COALESCE(iscf, 0) as iscf,
        COALESCE(ihdcf, 0) as ihdcf,
        COALESCE(rush_attempts, 0) as rush_attempts,
        COALESCE(rebound_attempts, 0) as rebound_attempts,
        COALESCE(shots_blocked, 0) as shots_blocked
    FROM player_game_stats
    WHERE situation_id = {all_id}
      AND toi_seconds >= {min_toi}
    """

    df = pd.read_sql_query(query, con)
    df['player_id'] = df['player_id'].astype(str).str.replace(r' \[.*\]', '', regex=True).str.strip()

    print(f"Loaded {len(df):,} NST player-game records")
    return df


def load_player_bio(con) -> pd.DataFrame:
    """Load player biographical data."""
    query = """
    SELECT
        player_id,
        birth_date,
        height_inches,
        weight_lbs,
        shoots_catches,
        debut_date,
        draft_year,
        draft_overall
    FROM player_bio
    """

    df = pd.read_sql_query(query, con)
    df['player_id'] = df['player_id'].astype(str).str.strip()

    # Calculate age features (will be joined with game date later)
    df['birth_date'] = pd.to_datetime(df['birth_date'], errors='coerce')
    df['debut_date'] = pd.to_datetime(df['debut_date'], errors='coerce')

    print(f"Loaded bio data for {len(df):,} players")
    return df


def load_game_context(con) -> pd.DataFrame:
    """Load game context (home/away, opponent, date)."""
    query = """
    SELECT
        game_id,
        game_date,
        home_team_id,
        away_team_id
    FROM games
    """

    df = pd.read_sql_query(query, con)
    df['game_date'] = pd.to_datetime(df['game_date'])

    print(f"Loaded {len(df):,} games")
    return df


def load_opponent_goalie_stats(con) -> pd.DataFrame:
    """
    Load opponent goalie stats for shooting prop context.
    Returns per-goalie rolling averages.
    """
    all_id = get_situation_id(con, 'All')

    # Get goalie game stats
    # Note: HD saves calculated as hd_shots_against - hd_goals_against
    query = f"""
    SELECT
        game_id,
        team_id,
        player_id as goalie_id,
        COALESCE(mp_saves, 0) as saves,
        COALESCE(mp_goals_against, 0) as goals_against,
        COALESCE(mp_xgoals_against, 0) as xga,
        COALESCE(mp_high_danger_shots_against, 0) - COALESCE(mp_high_danger_goals_against, 0) as hd_saves,
        COALESCE(mp_high_danger_goals_against, 0) as hd_ga,
        COALESCE(mp_high_danger_xgoals_against, 0) as hd_xga,
        COALESCE(mp_ice_time, 0) as toi
    FROM mp_goalie_game_stats
    WHERE situation_id = {all_id}
      AND mp_ice_time > 1800
    """

    df = pd.read_sql_query(query, con)

    if df.empty:
        return pd.DataFrame()

    df['goalie_id'] = df['goalie_id'].astype(str).str.strip()

    # Calculate per-game metrics
    df['sv_pct'] = df['saves'] / (df['saves'] + df['goals_against'] + 0.1)
    df['gsax'] = df['xga'] - df['goals_against']
    df['hd_sv_pct'] = df['hd_saves'] / (df['hd_saves'] + df['hd_ga'] + 0.1)

    # Sort and calculate rolling averages per goalie
    df = df.sort_values(['goalie_id', 'game_id'])
    grp = df.groupby('goalie_id')

    df['goalie_roll_sv_pct'] = grp['sv_pct'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )
    df['goalie_roll_gsax'] = grp['gsax'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )
    df['goalie_roll_hd_sv_pct'] = grp['hd_sv_pct'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )

    # Fill with league averages
    df['goalie_roll_sv_pct'] = df['goalie_roll_sv_pct'].fillna(0.910)
    df['goalie_roll_gsax'] = df['goalie_roll_gsax'].fillna(0.0)
    df['goalie_roll_hd_sv_pct'] = df['goalie_roll_hd_sv_pct'].fillna(0.820)

    print(f"Loaded goalie stats for {df['goalie_id'].nunique():,} goalies")

    return df[['game_id', 'team_id', 'goalie_id',
               'goalie_roll_sv_pct', 'goalie_roll_gsax', 'goalie_roll_hd_sv_pct']]


def load_team_defensive_stats(con) -> pd.DataFrame:
    """Load team defensive quality for opponent context."""
    all_id = get_situation_id(con, 'All')

    query = f"""
    SELECT
        game_id,
        team_id,
        COALESCE(mp_xgoals_against, 0) as xga,
        COALESCE(mp_shots_on_goal_against, 0) as sa,
        COALESCE(mp_high_danger_shots_against, 0) as hd_sa,
        COALESCE(mp_ice_time, 1) as toi
    FROM mp_team_game_stats
    WHERE situation_id = {all_id}
    """

    df = pd.read_sql_query(query, con)

    if df.empty:
        return pd.DataFrame()

    # Calculate per-60 rates
    df['xga_per60'] = (df['xga'] / df['toi']) * 3600
    df['sa_per60'] = (df['sa'] / df['toi']) * 3600
    df['hd_sa_per60'] = (df['hd_sa'] / df['toi']) * 3600

    # Rolling averages
    df = df.sort_values(['team_id', 'game_id'])
    grp = df.groupby('team_id')

    df['opp_xga_per60_l10'] = grp['xga_per60'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )
    df['opp_sa_per60_l10'] = grp['sa_per60'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )
    df['opp_hd_sa_per60_l10'] = grp['hd_sa_per60'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )

    # Fill with league averages
    league_avg_xga = df['xga_per60'].mean()
    league_avg_sa = df['sa_per60'].mean()

    df['opp_xga_per60_l10'] = df['opp_xga_per60_l10'].fillna(league_avg_xga if not np.isnan(league_avg_xga) else 2.5)
    df['opp_sa_per60_l10'] = df['opp_sa_per60_l10'].fillna(league_avg_sa if not np.isnan(league_avg_sa) else 30.0)
    df['opp_hd_sa_per60_l10'] = df['opp_hd_sa_per60_l10'].fillna(10.0)

    return df[['game_id', 'team_id', 'opp_xga_per60_l10', 'opp_sa_per60_l10', 'opp_hd_sa_per60_l10']]


# =============================================================================
# FEATURE ENGINEERING
# =============================================================================

def calculate_per60_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate per-60 minute rates for all counting stats."""
    df = df.copy()

    # Avoid division by zero
    toi_min = df['toi_seconds'] / 60
    toi_min = toi_min.replace(0, np.nan)

    # Per-60 rates
    for col in ['goals', 'assists', 'points', 'sog', 'shots', 'hits', 'blocks',
                'ixg', 'hd_shots', 'hd_xg', 'rebounds_created', 'giveaways', 'takeaways']:
        if col in df.columns:
            df[f'{col}_per60'] = (df[col] / toi_min) * 60
            df[f'{col}_per60'] = df[f'{col}_per60'].fillna(0)

    return df


def calculate_rolling_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate rolling statistics per player.
    Must be sorted by player_id and game_date first.
    """
    df = df.copy()
    df = df.sort_values(['player_id', 'game_date'])

    grp = df.groupby('player_id')

    # TOI rolling
    df['roll_toi_short'] = grp['toi_seconds'].transform(
        lambda x: x.shift(1).rolling(ROLL_SHORT, min_periods=1).mean()
    )
    df['roll_toi_medium'] = grp['toi_seconds'].transform(
        lambda x: x.shift(1).rolling(ROLL_MEDIUM, min_periods=1).mean()
    )
    df['roll_toi_standard'] = grp['toi_seconds'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )

    # Per-60 rolling for each prop
    rate_cols = ['goals_per60', 'assists_per60', 'points_per60', 'sog_per60',
                 'hits_per60', 'blocks_per60', 'ixg_per60', 'hd_xg_per60']

    for col in rate_cols:
        if col not in df.columns:
            continue

        base_name = col.replace('_per60', '')

        # Short (3 game)
        df[f'roll_{base_name}_per60_short'] = grp[col].transform(
            lambda x: x.shift(1).rolling(ROLL_SHORT, min_periods=1).mean()
        )

        # Medium (5 game)
        df[f'roll_{base_name}_per60_medium'] = grp[col].transform(
            lambda x: x.shift(1).rolling(ROLL_MEDIUM, min_periods=1).mean()
        )

        # Standard (10 game)
        df[f'roll_{base_name}_per60_standard'] = grp[col].transform(
            lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
        )

    # Shooting percentage
    df['sh_pct'] = df['goals'] / (df['sog'] + 0.1)
    df['roll_sh_pct_standard'] = grp['sh_pct'].transform(
        lambda x: x.shift(1).rolling(ROLL_STANDARD, min_periods=1).mean()
    )

    # Fill NaNs with 0 for rolling stats (new players)
    roll_cols = [c for c in df.columns if c.startswith('roll_')]
    for col in roll_cols:
        df[col] = df[col].fillna(0)

    return df


def add_game_context_features(df: pd.DataFrame, games_df: pd.DataFrame) -> pd.DataFrame:
    """Add game context: home/away, back-to-back, rest days, opponent."""
    df = df.copy()

    # Merge game info
    df = df.merge(
        games_df[['game_id', 'home_team_id', 'away_team_id']],
        on='game_id',
        how='left'
    )

    # Is home?
    df['is_home'] = (df['team_id'] == df['home_team_id']).astype(int)

    # Opponent team
    df['opponent_team_id'] = np.where(
        df['team_id'] == df['home_team_id'],
        df['away_team_id'],
        df['home_team_id']
    )

    # Back-to-back and rest days
    df = df.sort_values(['player_id', 'game_date'])
    grp = df.groupby('player_id')

    df['prev_game_date'] = grp['game_date'].shift(1)
    df['days_rest'] = (df['game_date'] - df['prev_game_date']).dt.days
    df['days_rest'] = df['days_rest'].fillna(3).clip(0, 10)

    df['is_b2b'] = (df['days_rest'] <= 1).astype(int)

    # Workload last 7 days (games played) - simplified calculation
    # Count games in last 7 days using cumcount within rolling window
    df['workload_l7'] = grp['days_rest'].transform(
        lambda x: x.rolling(7, min_periods=1).apply(lambda w: (w <= 7).sum(), raw=True)
    ).fillna(1).clip(1, 7)

    # Clean up
    df = df.drop(columns=['home_team_id', 'away_team_id', 'prev_game_date'], errors='ignore')

    return df


def add_player_bio_features(df: pd.DataFrame, bio_df: pd.DataFrame) -> pd.DataFrame:
    """Add player biographical features: age, experience, size."""
    df = df.copy()

    # Merge bio
    df = df.merge(bio_df, on='player_id', how='left')

    # Calculate age at game time
    df['age_years'] = (df['game_date'] - df['birth_date']).dt.days / 365.25
    df['age_years'] = df['age_years'].fillna(27)  # League average

    # Years pro
    df['years_pro'] = (df['game_date'] - df['debut_date']).dt.days / 365.25
    df['years_pro'] = df['years_pro'].clip(0, 25).fillna(5)

    # Size
    df['height_inches'] = df['height_inches'].fillna(73)  # ~6'1"
    df['weight_lbs'] = df['weight_lbs'].fillna(200)

    # Handedness (for goalie matchup)
    df['is_left_shot'] = (df['shoots_catches'] == 'L').astype(int)

    # Clean up
    df = df.drop(columns=['birth_date', 'debut_date', 'shoots_catches',
                          'draft_year', 'draft_overall'], errors='ignore')

    return df


def add_opponent_features(df: pd.DataFrame, team_def_df: pd.DataFrame,
                          goalie_df: pd.DataFrame) -> pd.DataFrame:
    """Add opponent defensive quality and goalie features."""
    df = df.copy()

    # Team defensive stats (opponent's defensive quality)
    if not team_def_df.empty:
        # Rename to indicate these are opponent stats
        opp_cols = team_def_df.rename(columns={'team_id': 'opponent_team_id'})
        df = df.merge(opp_cols, on=['game_id', 'opponent_team_id'], how='left')

    # Opponent goalie stats
    if not goalie_df.empty:
        opp_goalie = goalie_df.rename(columns={'team_id': 'opponent_team_id'})
        df = df.merge(opp_goalie, on=['game_id', 'opponent_team_id'], how='left')

    # Fill missing opponent stats with league averages
    df['opp_xga_per60_l10'] = df.get('opp_xga_per60_l10', pd.Series([2.5]*len(df))).fillna(2.5)
    df['opp_sa_per60_l10'] = df.get('opp_sa_per60_l10', pd.Series([30.0]*len(df))).fillna(30.0)
    df['goalie_roll_sv_pct'] = df.get('goalie_roll_sv_pct', pd.Series([0.910]*len(df))).fillna(0.910)
    df['goalie_roll_hd_sv_pct'] = df.get('goalie_roll_hd_sv_pct', pd.Series([0.820]*len(df))).fillna(0.820)

    return df


# =============================================================================
# MAIN DATA PIPELINE
# =============================================================================

def prepare_player_features(db_path: str, min_toi: int = MIN_TOI_SECONDS) -> pd.DataFrame:
    """
    Main pipeline: Load all data and engineer features.

    Returns DataFrame ready for model training.
    """
    print("=" * 80)
    print("PREPARING PLAYER FEATURES")
    print("=" * 80)

    con = sqlite3.connect(db_path)

    # Step 1: Load base player-game data
    print("\nStep 1: Loading base player-game data...")
    df = load_player_game_base(con, min_toi)

    if df.empty:
        con.close()
        raise ValueError("No player data found!")

    # Step 2: Load supplementary data
    print("\nStep 2: Loading supplementary data...")
    games_df = load_game_context(con)
    bio_df = load_player_bio(con)
    team_def_df = load_team_defensive_stats(con)
    goalie_df = load_opponent_goalie_stats(con)

    con.close()

    # Step 3: Calculate per-60 rates
    print("\nStep 3: Calculating per-60 rates...")
    df = calculate_per60_rates(df)

    # Step 4: Calculate rolling statistics
    print("\nStep 4: Calculating rolling statistics...")
    df = calculate_rolling_stats(df)

    # Step 5: Add game context
    print("\nStep 5: Adding game context features...")
    df = add_game_context_features(df, games_df)

    # Step 6: Add player bio
    print("\nStep 6: Adding player bio features...")
    df = add_player_bio_features(df, bio_df)

    # Step 7: Add opponent features
    print("\nStep 7: Adding opponent features...")
    df = add_opponent_features(df, team_def_df, goalie_df)

    # Final cleanup
    df = df.fillna(0)

    print("\n" + "=" * 80)
    print("FEATURE PREPARATION COMPLETE")
    print("=" * 80)
    print(f"Final dataset: {len(df):,} player-games")
    print(f"Features: {len(df.columns)}")
    print(f"Date range: {df['game_date'].min()} to {df['game_date'].max()}")

    return df


# =============================================================================
# FEATURE SELECTION
# =============================================================================

def get_toi_features() -> List[str]:
    """Features for TOI prediction."""
    return [
        # Recent TOI trends (most important)
        'roll_toi_short', 'roll_toi_medium', 'roll_toi_standard',

        # Game context
        'is_home', 'is_b2b', 'days_rest',

        # Player attributes
        'age_years', 'years_pro',

        # Position (will be one-hot encoded)
        'pos_group',

        # Recent performance (coaches reward production)
        'roll_points_per60_short', 'roll_sog_per60_short',

        # Opponent strength
        'opp_xga_per60_l10'
    ]


def get_prop_features(prop: str) -> List[str]:
    """Features for prop prediction."""
    base = [
        'roll_toi_standard',
        f'roll_{prop}_per60_short',
        f'roll_{prop}_per60_medium',
        f'roll_{prop}_per60_standard',
        'is_home', 'is_b2b', 'days_rest',
        'age_years',
        'pos_group',
        'opp_xga_per60_l10', 'opp_sa_per60_l10'
    ]

    if prop in ['goals', 'assists', 'points']:
        specific = [
            'roll_ixg_per60_standard',
            'roll_sh_pct_standard',
            'goalie_roll_sv_pct',
            'goalie_roll_hd_sv_pct'
        ]
    elif prop == 'sog':
        specific = [
            'roll_ixg_per60_standard',
            'opp_sa_per60_l10'
        ]
    elif prop == 'hits':
        specific = ['weight_lbs']
    elif prop == 'blocks':
        specific = ['opp_sa_per60_l10']
    else:
        specific = []

    return base + specific


# =============================================================================
# DATA PREPARATION FOR TRAINING
# =============================================================================

def prepare_training_data(df: pd.DataFrame, prop: str, mode: str = 'toi'
                          ) -> Tuple[jnp.ndarray, jnp.ndarray, List[str]]:
    """
    Prepare data for model training.

    Args:
        df: Full feature dataframe
        prop: 'toi' or prop name
        mode: 'toi', 'binary', or 'rate'

    Returns:
        X, y, feature_names
    """
    df = df.copy()

    # Filter valid records
    if prop == 'toi':
        df = df[df['toi_seconds'].notna()].copy()
        df = df[df['roll_toi_standard'].notna()].copy()
        feature_list = get_toi_features()
        target_col = 'toi_seconds'
    else:
        df = df[df[prop].notna()].copy()
        df = df[df['toi_seconds'] > 0].copy()

        rate_col = f'roll_{prop}_per60_standard'
        if rate_col in df.columns:
            df = df[df[rate_col].notna()].copy()

        feature_list = get_prop_features(prop)
        target_col = prop

    # One-hot encode categoricals
    cat_cols = [c for c in ['pos_group'] if c in df.columns and c in feature_list]
    if cat_cols:
        df = pd.get_dummies(df, columns=cat_cols, drop_first=True, dtype=float)

    # Find available features (including one-hot encoded)
    available_features = []
    for feat in feature_list:
        if feat in df.columns:
            available_features.append(feat)
        else:
            # Check for one-hot encoded variants
            encoded = [c for c in df.columns if c.startswith(f'{feat}_')]
            available_features.extend(encoded)

    available_features = list(set(available_features))
    available_features = [f for f in available_features if f in df.columns]

    print(f"Using {len(available_features)} features")

    # Prepare X
    X = df[available_features].fillna(0).values.astype(np.float32)

    # Prepare y
    if mode == 'binary':
        y = (df[target_col] > 0.5).astype(float).values
    elif mode == 'rate':
        df['_rate'] = (df[target_col] / df['toi_seconds']) * 3600
        q99 = df['_rate'].quantile(0.99)
        mask = df['_rate'] <= q99
        X = X[mask]
        y = df.loc[mask, '_rate'].values.astype(np.float32)
    else:  # toi
        y = df[target_col].values.astype(np.float32)

    X = jnp.array(X, dtype=jnp.float32)
    y = jnp.array(y, dtype=jnp.float32)

    return X, y, available_features


def standardize_features(X: jnp.ndarray, stats_path: str, mode: str = 'train'
                         ) -> jnp.ndarray:
    """Z-score standardization."""
    if mode == 'train':
        mean = jnp.mean(X, axis=0)
        std = jnp.std(X, axis=0) + 1e-8
        np.savez(stats_path, mean=np.array(mean), std=np.array(std))
    else:
        stats = np.load(stats_path)
        mean = jnp.array(stats['mean'])
        std = jnp.array(stats['std'])

    return (X - mean) / std


# =============================================================================
# NEURAL NETWORK (JAX)
# =============================================================================

def init_params(key, n_features: int, hidden_size: int):
    """Initialize network parameters."""
    keys = jax.random.split(key, 3)

    scale1 = jnp.sqrt(2.0 / n_features)
    scale2 = jnp.sqrt(2.0 / hidden_size)
    scale3 = jnp.sqrt(2.0 / hidden_size)

    params = {
        'W1': jax.random.normal(keys[0], (n_features, hidden_size)) * scale1,
        'b1': jnp.zeros(hidden_size),
        'W2': jax.random.normal(keys[1], (hidden_size, hidden_size)) * scale2,
        'b2': jnp.zeros(hidden_size),
        'W3': jax.random.normal(keys[2], (hidden_size, 1)) * scale3,
        'b3': jnp.zeros(1),
    }

    adam_state = {
        'm': {k: jnp.zeros_like(v) for k, v in params.items()},
        'v': {k: jnp.zeros_like(v) for k, v in params.items()},
        't': 0
    }

    return params, adam_state


def forward(params, x, training=False, rng_key=None, dropout_rate=0.2, output_mode='toi'):
    """Forward pass with ELU activation and dropout."""
    h = jax.nn.elu(x @ params['W1'] + params['b1'])

    if training and dropout_rate > 0.0 and rng_key is not None:
        key1, key2 = jax.random.split(rng_key)
        keep_prob = 1.0 - dropout_rate
        mask1 = jax.random.bernoulli(key1, keep_prob, h.shape)
        h = jnp.where(mask1, h / keep_prob, 0.0)
    else:
        key2 = rng_key

    h = jax.nn.elu(h @ params['W2'] + params['b2'])

    if training and dropout_rate > 0.0 and key2 is not None:
        keep_prob = 1.0 - dropout_rate
        mask2 = jax.random.bernoulli(key2, keep_prob, h.shape)
        h = jnp.where(mask2, h / keep_prob, 0.0)

    output = h @ params['W3'] + params['b3']

    if output_mode == 'toi':
        return (jax.nn.softplus(output) + 60.0).squeeze()  # Min 60 seconds
    elif output_mode == 'binary':
        return jax.nn.sigmoid(output).squeeze()
    else:  # rate
        return jax.nn.softplus(output).squeeze()


def loss_fn_toi(params, x, y, training=False, rng_key=None, dropout_rate=0.2):
    """MSE loss for TOI."""
    pred = forward(params, x, training=training, rng_key=rng_key,
                   dropout_rate=dropout_rate, output_mode='toi')
    mse = jnp.mean((pred - y) ** 2)
    l2_reg = 1e-5 * jnp.sum(params['W3'] ** 2)
    return mse + l2_reg


def loss_fn_binary(params, x, y, training=False, rng_key=None, dropout_rate=0.2):
    """Binary cross-entropy loss with numerical stability."""
    pred = forward(params, x, training=training, rng_key=rng_key,
                   dropout_rate=dropout_rate, output_mode='binary')
    # Clip predictions for numerical stability
    pred = jnp.clip(pred, 1e-7, 1 - 1e-7)
    bce = -jnp.mean(y * jnp.log(pred) + (1 - y) * jnp.log(1 - pred))
    l2_reg = 1e-5 * jnp.sum(params['W3'] ** 2)
    return bce + l2_reg


def loss_fn_rate(params, x, y, training=False, rng_key=None, dropout_rate=0.2):
    """MSE loss for rate."""
    pred = forward(params, x, training=training, rng_key=rng_key,
                   dropout_rate=dropout_rate, output_mode='rate')
    mse = jnp.mean((pred - y) ** 2)
    l2_reg = 1e-5 * jnp.sum(params['W3'] ** 2)
    return mse + l2_reg


def adam_update(params, grads, adam_state, lr, beta1=0.9, beta2=0.999, eps=1e-8):
    """Adam optimizer step."""
    t = adam_state['t'] + 1
    m = adam_state['m']
    v = adam_state['v']

    new_m = {}
    new_v = {}
    new_params = {}

    for key in params:
        new_m[key] = beta1 * m[key] + (1 - beta1) * grads[key]
        new_v[key] = beta2 * v[key] + (1 - beta2) * (grads[key] ** 2)

        m_hat = new_m[key] / (1 - beta1 ** t)
        v_hat = new_v[key] / (1 - beta2 ** t)

        new_params[key] = params[key] - lr * m_hat / (jnp.sqrt(v_hat) + eps)

    return new_params, {'m': new_m, 'v': new_v, 't': t}


def cosine_decay_schedule(epoch, total_epochs, lr_max, lr_min=1e-6, warmup_epochs=10):
    """Cosine annealing with warmup."""
    if epoch < warmup_epochs:
        return lr_min + (lr_max - lr_min) * (epoch / warmup_epochs)
    else:
        progress = (epoch - warmup_epochs) / (total_epochs - warmup_epochs)
        return lr_min + 0.5 * (lr_max - lr_min) * (1 + jnp.cos(jnp.pi * progress))


@jax.jit
def update_step_toi(params, adam_state, x, y, lr, rng_key):
    """JIT-compiled TOI training step (dropout hardcoded to 0.2)."""
    loss_and_grad = jax.value_and_grad(
        lambda p: loss_fn_toi(p, x, y, training=True, rng_key=rng_key, dropout_rate=0.2)
    )
    loss, grads = loss_and_grad(params)
    new_params, new_adam_state = adam_update(params, grads, adam_state, lr)
    return new_params, new_adam_state, loss


@jax.jit
def update_step_binary(params, adam_state, x, y, lr, rng_key):
    """JIT-compiled binary training step (dropout hardcoded to 0.2)."""
    loss_and_grad = jax.value_and_grad(
        lambda p: loss_fn_binary(p, x, y, training=True, rng_key=rng_key, dropout_rate=0.2)
    )
    loss, grads = loss_and_grad(params)
    new_params, new_adam_state = adam_update(params, grads, adam_state, lr)
    return new_params, new_adam_state, loss


@jax.jit
def update_step_rate(params, adam_state, x, y, lr, rng_key):
    """JIT-compiled rate training step (dropout hardcoded to 0.2)."""
    loss_and_grad = jax.value_and_grad(
        lambda p: loss_fn_rate(p, x, y, training=True, rng_key=rng_key, dropout_rate=0.2)
    )
    loss, grads = loss_and_grad(params)
    new_params, new_adam_state = adam_update(params, grads, adam_state, lr)
    return new_params, new_adam_state, loss


# =============================================================================
# TRAINING
# =============================================================================

def train_model(df: pd.DataFrame, prop: str, mode: str = 'toi',
                epochs: int = None, batch_size: int = None,
                lr: float = DEFAULT_LR, hidden_size: int = None,
                test_size: float = 0.2, seed: int = 42):
    """
    Train model for TOI or prop.

    Args:
        df: Feature dataframe
        prop: 'toi' or prop name
        mode: 'toi', 'binary', or 'rate'
    """
    # Set defaults
    if prop == 'toi':
        epochs = epochs or DEFAULT_EPOCHS_TOI
        batch_size = batch_size or DEFAULT_BATCH_TOI
        hidden_size = hidden_size or DEFAULT_HIDDEN_TOI
        model_mode = 'toi'
        stats_path = f"{MODEL_DIR}toi_standardize_stats.npz"
        model_path = f"{MODEL_DIR}toi_model_params.npz"
        loss_fn = loss_fn_toi
        update_fn = update_step_toi
    else:
        epochs = epochs or DEFAULT_EPOCHS_PROP
        batch_size = batch_size or DEFAULT_BATCH_PROP
        hidden_size = hidden_size or DEFAULT_HIDDEN_PROP
        model_mode = mode
        stats_path = f"{MODEL_DIR}{prop}_{mode}_standardize_stats.npz"
        model_path = f"{MODEL_DIR}{prop}_{mode}_model_params.npz"

        if mode == 'binary':
            loss_fn = loss_fn_binary
            update_fn = update_step_binary
        else:
            loss_fn = loss_fn_rate
            update_fn = update_step_rate

    print("=" * 80)
    if prop == 'toi':
        print("TRAINING TOI PREDICTION MODEL")
    else:
        print(f"TRAINING {prop.upper()} {mode.upper()} MODEL")
    print("=" * 80)
    print(f"Epochs: {epochs} | Batch: {batch_size} | LR: {lr} | Hidden: {hidden_size}")
    print()

    # Prepare data
    X, y, feature_cols = prepare_training_data(df, prop, mode=model_mode)

    print(f"Samples: {len(X):,}")
    print(f"Features: {len(feature_cols)}")

    if model_mode == 'binary':
        print(f"Positive rate: {float(jnp.mean(y)):.1%}")
    elif model_mode == 'toi':
        print(f"Mean TOI: {float(jnp.mean(y))/60:.1f} min")
    else:
        print(f"Mean rate: {float(jnp.mean(y)):.3f} per-60")
    print()

    # Standardize
    X = standardize_features(X, stats_path, mode='train')

    # Train/test split
    split_idx = int(len(X) * (1 - test_size))
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    print(f"Train: {len(X_train):,} | Test: {len(X_test):,}")
    print()

    # Initialize
    key = jax.random.PRNGKey(seed)
    params, adam_state = init_params(key, X.shape[1], hidden_size)

    steps = max(1, len(X_train) // batch_size)
    best_val_loss = float('inf')
    best_params = None
    patience_counter = 0
    dropout_rate = 0.2

    print("Training...")
    print()

    for e in range(epochs):
        current_lr = cosine_decay_schedule(e, epochs, lr_max=lr, lr_min=1e-6, warmup_epochs=10)

        # Shuffle
        key, subkey = jax.random.split(key)
        perm = jax.random.permutation(subkey, len(X_train))
        X_train_shuffled = X_train[perm]
        y_train_shuffled = y_train[perm]

        loss_sum = 0.0

        for i in range(steps):
            xb = X_train_shuffled[i * batch_size:(i + 1) * batch_size]
            yb = y_train_shuffled[i * batch_size:(i + 1) * batch_size]

            key, dropout_key = jax.random.split(key)
            params, adam_state, l = update_fn(params, adam_state, xb, yb,
                                              current_lr, dropout_key)
            loss_sum += l

        # Validation every 10 epochs
        if e % 10 == 0 or e == epochs - 1:
            val_loss = loss_fn(params, X_test, y_test, training=False, rng_key=None, dropout_rate=0.0)
            train_loss = loss_sum / steps

            val_pred = forward(params, X_test, training=False, rng_key=None,
                               dropout_rate=0.0, output_mode=model_mode)

            if model_mode == 'binary':
                acc = jnp.mean((val_pred > 0.5) == y_test)
                metric_str = f"Acc {acc:.3f}"
            elif model_mode == 'toi':
                mae = jnp.mean(jnp.abs(val_pred - y_test))
                metric_str = f"MAE {mae/60:.2f}min"
            else:
                mae = jnp.mean(jnp.abs(val_pred - y_test))
                metric_str = f"MAE {mae:.3f}"

            improved = ""
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                best_params = params
                improved = "✓"
                patience_counter = 0
            else:
                patience_counter += 1

            print(f"Epoch {e:3d} | LR {current_lr:.6f} | "
                  f"Train {train_loss:.4f} | Val {val_loss:.4f} | {metric_str} {improved}")

            # Early stopping
            patience_limit = 50 if prop == 'toi' else 30
            if patience_counter > patience_limit:
                print(f"\nEarly stopping at epoch {e}")
                break

    print()
    print("=" * 80)
    print("TRAINING COMPLETE")
    print("=" * 80)

    # Save model
    final_params = best_params if best_params is not None else params
    print(f"\nSaving model to {model_path}...")
    np.savez(model_path,
             **{k: np.array(v) for k, v in final_params.items()},
             features=np.array(feature_cols, dtype=object))
    print("Model saved!")

    return final_params, feature_cols


# =============================================================================
# PREDICTION
# =============================================================================

def predict_prop(df: pd.DataFrame, prop: str, mode: str = 'binary') -> np.ndarray:
    """
    Make predictions for a prop.

    Args:
        df: Feature dataframe (must have required features)
        prop: 'toi' or prop name
        mode: 'binary' or 'rate'

    Returns:
        Array of predictions
    """
    if prop == 'toi':
        stats_path = f"{MODEL_DIR}toi_standardize_stats.npz"
        model_path = f"{MODEL_DIR}toi_model_params.npz"
        output_mode = 'toi'
    else:
        stats_path = f"{MODEL_DIR}{prop}_{mode}_standardize_stats.npz"
        model_path = f"{MODEL_DIR}{prop}_{mode}_model_params.npz"
        output_mode = mode

    # Load model
    model_data = np.load(model_path, allow_pickle=True)
    params = {k: jnp.array(model_data[k]) for k in ['W1', 'b1', 'W2', 'b2', 'W3', 'b3']}
    feature_cols = list(model_data['features'])

    # Prepare features
    X, _, _ = prepare_training_data(df, prop, mode=output_mode if prop != 'toi' else 'toi')
    X = standardize_features(X, stats_path, mode='predict')

    # Predict
    predictions = forward(params, X, training=False, output_mode=output_mode)

    return np.array(predictions)


# =============================================================================
# VALIDATION
# =============================================================================

def validate_model(df: pd.DataFrame, prop: str, mode: str = 'binary'):
    """Run validation on held-out data."""
    print("=" * 80)
    print(f"VALIDATING {prop.upper()} {mode.upper()} MODEL")
    print("=" * 80)

    # Use last 20% as validation
    df = df.sort_values('game_date')
    val_df = df.tail(int(len(df) * 0.2)).copy()

    print(f"Validation set: {len(val_df):,} samples")
    print(f"Date range: {val_df['game_date'].min()} to {val_df['game_date'].max()}")

    # Get predictions
    predictions = predict_prop(val_df, prop, mode)

    # Get actuals
    if prop == 'toi':
        actuals = val_df['toi_seconds'].values
        mae = np.mean(np.abs(predictions - actuals))
        print(f"\nMAE: {mae:.1f} seconds ({mae/60:.2f} minutes)")
    elif mode == 'binary':
        actuals = (val_df[prop] > 0.5).astype(int).values
        accuracy = np.mean((predictions > 0.5) == actuals)
        print(f"\nAccuracy: {accuracy:.3f}")
        print(f"Positive rate (actual): {actuals.mean():.3f}")
        print(f"Positive rate (predicted): {(predictions > 0.5).mean():.3f}")
    else:
        actuals = (val_df[prop] / val_df['toi_seconds']) * 3600
        actuals = actuals.values
        mae = np.mean(np.abs(predictions - actuals))
        print(f"\nMAE: {mae:.3f} per-60")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NHL Player Prop Model")
    parser.add_argument("--train", action="store_true", help="Train model")
    parser.add_argument("--predict", action="store_true", help="Make predictions")
    parser.add_argument("--validate", action="store_true", help="Validate model")
    parser.add_argument("--prop", type=str, default="toi", choices=PROPS,
                        help="Prop to train/predict")
    parser.add_argument("--mode", type=str, default="both",
                        choices=['binary', 'rate', 'both', 'toi'],
                        help="Model mode (for props)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH,
                        help="Database path")
    parser.add_argument("--epochs", type=int, default=None,
                        help="Training epochs")
    parser.add_argument("--batch", type=int, default=None,
                        help="Batch size")
    parser.add_argument("--lr", type=float, default=DEFAULT_LR,
                        help="Learning rate")
    parser.add_argument("--hidden", type=int, default=None,
                        help="Hidden layer size")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed")
    parser.add_argument("--date", type=str, default=None,
                        help="Date for predictions (YYYY-MM-DD)")

    args = parser.parse_args()

    # Load data
    print(f"\nLoading data from {args.db}...")
    df = prepare_player_features(args.db)

    if args.train:
        if args.prop == 'toi':
            train_model(df, prop='toi', mode='toi',
                        epochs=args.epochs, batch_size=args.batch,
                        lr=args.lr, hidden_size=args.hidden, seed=args.seed)
        else:
            if args.mode == 'both':
                print("\nTraining BINARY model...")
                train_model(df, prop=args.prop, mode='binary',
                            epochs=args.epochs, batch_size=args.batch,
                            lr=args.lr, hidden_size=args.hidden, seed=args.seed)

                print("\n\n")

                print("Training RATE model...")
                train_model(df, prop=args.prop, mode='rate',
                            epochs=args.epochs, batch_size=args.batch,
                            lr=args.lr, hidden_size=args.hidden, seed=args.seed)
            else:
                train_model(df, prop=args.prop, mode=args.mode,
                            epochs=args.epochs, batch_size=args.batch,
                            lr=args.lr, hidden_size=args.hidden, seed=args.seed)

    elif args.validate:
        if args.prop == 'toi':
            validate_model(df, 'toi', 'toi')
        else:
            validate_model(df, args.prop, 'binary')
            validate_model(df, args.prop, 'rate')

    elif args.predict:
        # Filter to recent/upcoming games if date specified
        if args.date:
            target_date = pd.to_datetime(args.date)
            df = df[df['game_date'] >= target_date - timedelta(days=7)]

        print(f"\nPredicting {args.prop} for {len(df):,} player-games...")

        if args.prop == 'toi':
            preds = predict_prop(df, 'toi')
            df['pred_toi_min'] = preds / 60
            print(df[['player_id', 'game_date', 'toi_seconds', 'pred_toi_min']].tail(20))
        else:
            preds_binary = predict_prop(df, args.prop, 'binary')
            preds_rate = predict_prop(df, args.prop, 'rate')
            df[f'p_{args.prop}_any'] = preds_binary
            df[f'{args.prop}_rate_per60'] = preds_rate
            print(df[['player_id', 'game_date', args.prop,
                      f'p_{args.prop}_any', f'{args.prop}_rate_per60']].tail(20))

    else:
        parser.print_help()
