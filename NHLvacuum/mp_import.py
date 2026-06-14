#!/usr/bin/env python3
"""
MoneyPuck Data Importer

Import MoneyPuck team and player data into the NHL analytics database.
Contains separate classes for team and player imports.
"""

import sqlite3
import csv
from pathlib import Path
from typing import Dict, Optional, Tuple, Set
# Goalie data not being imported


def season_data_dir(base_dir: str, season_type: str) -> str:
    """Resolve a season-type-specific data directory.

    Regular season keeps the historical location; playoff data lives in a parallel
    'playoffs' subtree written by moneypuck_downloader.season_subdir():
        'moneypuck_data/teams' + 'playoffs' -> 'moneypuck_data/playoffs/teams'
    """
    if season_type == 'regular':
        return base_dir
    p = Path(base_dir)
    return str(p.parent / season_type / p.name)

class MoneyPuckTeamImporter:
    """Imports MoneyPuck team game-by-game data"""

    DB_PATH = 'nhl_analytics.db'
    MP_TEAM_DATA_DIR = 'moneypuck_data/teams'
    SCHEMA_PATH = 'moneypuck_schema.sql'

    # MoneyPuck situation codes → our situation IDs
    SITUATION_MAP = {
        '5on5': '5v5',
        'all': 'All',
        '4on5': 'PK',
        '5on4': 'PP',
        'other': 'other'
    }

    # Team abbreviation mapping (MoneyPuck → Our DB)
    TEAM_ABBR_MAP = {
        'L.A': 'LA',
        'N.J': 'NJ',
        'S.J': 'SJ',
        'T.B': 'TB',
        'LAK': 'LA',
        'SJS': 'SJ',
        'TBL': 'TB',
        'NJD': 'NJ',
    }

    # CSV column → Database column mapping
    COLUMN_MAP = {
        # Metadata
        'season': 'mp_season',
        'home_or_away': 'mp_home_or_away',
        'gameDate': 'mp_game_date',

        # Percentages and rates
        'xGoalsPercentage': 'mp_xgoals_percentage',
        'corsiPercentage': 'mp_corsi_percentage',
        'fenwickPercentage': 'mp_fenwick_percentage',
        'iceTime': 'mp_ice_time',

        # Expected goals (FOR)
        'xOnGoalFor': 'mp_xon_goal_for',
        'xGoalsFor': 'mp_xgoals_for',
        'xReboundsFor': 'mp_xrebounds_for',
        'xFreezeFor': 'mp_xfreeze_for',
        'xPlayStoppedFor': 'mp_xplay_stopped_for',
        'xPlayContinuedInZoneFor': 'mp_xplay_continued_in_zone_for',
        'xPlayContinuedOutsideZoneFor': 'mp_xplay_continued_outside_zone_for',
        'flurryAdjustedxGoalsFor': 'mp_flurry_adjusted_xgoals_for',
        'scoreVenueAdjustedxGoalsFor': 'mp_score_venue_adjusted_xgoals_for',
        'flurryScoreVenueAdjustedxGoalsFor': 'mp_flurry_score_venue_adjusted_xgoals_for',

        # Shots and attempts (FOR)
        'shotsOnGoalFor': 'mp_shots_on_goal_for',
        'missedShotsFor': 'mp_missed_shots_for',
        'blockedShotAttemptsFor': 'mp_blocked_shot_attempts_for',
        'shotAttemptsFor': 'mp_shot_attempts_for',
        'goalsFor': 'mp_goals_for',
        'reboundsFor': 'mp_rebounds_for',
        'reboundGoalsFor': 'mp_rebound_goals_for',
        'freezeFor': 'mp_freeze_for',
        'playStoppedFor': 'mp_play_stopped_for',
        'playContinuedInZoneFor': 'mp_play_continued_in_zone_for',
        'playContinuedOutsideZoneFor': 'mp_play_continued_outside_zone_for',
        'savedShotsOnGoalFor': 'mp_saved_shots_on_goal_for',
        'savedUnblockedShotAttemptsFor': 'mp_saved_unblocked_shot_attempts_for',

        # Other events (FOR)
        'penaltiesFor': 'mp_penalties_for',
        'penalityMinutesFor': 'mp_penality_minutes_for',
        'faceOffsWonFor': 'mp_faceoffs_won_for',
        'hitsFor': 'mp_hits_for',
        'takeawaysFor': 'mp_takeaways_for',
        'giveawaysFor': 'mp_giveaways_for',

        # Shot danger breakdown (FOR)
        'lowDangerShotsFor': 'mp_low_danger_shots_for',
        'mediumDangerShotsFor': 'mp_medium_danger_shots_for',
        'highDangerShotsFor': 'mp_high_danger_shots_for',
        'lowDangerxGoalsFor': 'mp_low_danger_xgoals_for',
        'mediumDangerxGoalsFor': 'mp_medium_danger_xgoals_for',
        'highDangerxGoalsFor': 'mp_high_danger_xgoals_for',
        'lowDangerGoalsFor': 'mp_low_danger_goals_for',
        'mediumDangerGoalsFor': 'mp_medium_danger_goals_for',
        'highDangerGoalsFor': 'mp_high_danger_goals_for',

        # Advanced metrics (FOR)
        'scoreAdjustedShotsAttemptsFor': 'mp_score_adjusted_shots_attempts_for',
        'unblockedShotAttemptsFor': 'mp_unblocked_shot_attempts_for',
        'scoreAdjustedUnblockedShotAttemptsFor': 'mp_score_adjusted_unblocked_shot_attempts_for',
        'dZoneGiveawaysFor': 'mp_dzone_giveaways_for',
        'xGoalsFromxReboundsOfShotsFor': 'mp_xgoals_from_xrebounds_of_shots_for',
        'xGoalsFromActualReboundsOfShotsFor': 'mp_xgoals_from_actual_rebounds_of_shots_for',
        'reboundxGoalsFor': 'mp_rebound_xgoals_for',
        'totalShotCreditFor': 'mp_total_shot_credit_for',
        'scoreAdjustedTotalShotCreditFor': 'mp_score_adjusted_total_shot_credit_for',
        'scoreFlurryAdjustedTotalShotCreditFor': 'mp_score_flurry_adjusted_total_shot_credit_for',

        # Expected goals (AGAINST)
        'xOnGoalAgainst': 'mp_xon_goal_against',
        'xGoalsAgainst': 'mp_xgoals_against',
        'xReboundsAgainst': 'mp_xrebounds_against',
        'xFreezeAgainst': 'mp_xfreeze_against',
        'xPlayStoppedAgainst': 'mp_xplay_stopped_against',
        'xPlayContinuedInZoneAgainst': 'mp_xplay_continued_in_zone_against',
        'xPlayContinuedOutsideZoneAgainst': 'mp_xplay_continued_outside_zone_against',
        'flurryAdjustedxGoalsAgainst': 'mp_flurry_adjusted_xgoals_against',
        'scoreVenueAdjustedxGoalsAgainst': 'mp_score_venue_adjusted_xgoals_against',
        'flurryScoreVenueAdjustedxGoalsAgainst': 'mp_flurry_score_venue_adjusted_xgoals_against',

        # Shots and attempts (AGAINST)
        'shotsOnGoalAgainst': 'mp_shots_on_goal_against',
        'missedShotsAgainst': 'mp_missed_shots_against',
        'blockedShotAttemptsAgainst': 'mp_blocked_shot_attempts_against',
        'shotAttemptsAgainst': 'mp_shot_attempts_against',
        'goalsAgainst': 'mp_goals_against',
        'reboundsAgainst': 'mp_rebounds_against',
        'reboundGoalsAgainst': 'mp_rebound_goals_against',
        'freezeAgainst': 'mp_freeze_against',
        'playStoppedAgainst': 'mp_play_stopped_against',
        'playContinuedInZoneAgainst': 'mp_play_continued_in_zone_against',
        'playContinuedOutsideZoneAgainst': 'mp_play_continued_outside_zone_against',
        'savedShotsOnGoalAgainst': 'mp_saved_shots_on_goal_against',
        'savedUnblockedShotAttemptsAgainst': 'mp_saved_unblocked_shot_attempts_against',

        # Other events (AGAINST)
        'penaltiesAgainst': 'mp_penalties_against',
        'penalityMinutesAgainst': 'mp_penality_minutes_against',
        'faceOffsWonAgainst': 'mp_faceoffs_won_against',
        'hitsAgainst': 'mp_hits_against',
        'takeawaysAgainst': 'mp_takeaways_against',
        'giveawaysAgainst': 'mp_giveaways_against',

        # Shot danger breakdown (AGAINST)
        'lowDangerShotsAgainst': 'mp_low_danger_shots_against',
        'mediumDangerShotsAgainst': 'mp_medium_danger_shots_against',
        'highDangerShotsAgainst': 'mp_high_danger_shots_against',
        'lowDangerxGoalsAgainst': 'mp_low_danger_xgoals_against',
        'mediumDangerxGoalsAgainst': 'mp_medium_danger_xgoals_against',
        'highDangerxGoalsAgainst': 'mp_high_danger_xgoals_against',
        'lowDangerGoalsAgainst': 'mp_low_danger_goals_against',
        'mediumDangerGoalsAgainst': 'mp_medium_danger_goals_against',
        'highDangerGoalsAgainst': 'mp_high_danger_goals_against',

        # Advanced metrics (AGAINST)
        'scoreAdjustedShotsAttemptsAgainst': 'mp_score_adjusted_shots_attempts_against',
        'unblockedShotAttemptsAgainst': 'mp_unblocked_shot_attempts_against',
        'scoreAdjustedUnblockedShotAttemptsAgainst': 'mp_score_adjusted_unblocked_shot_attempts_against',
        'dZoneGiveawaysAgainst': 'mp_dzone_giveaways_against',
        'xGoalsFromxReboundsOfShotsAgainst': 'mp_xgoals_from_xrebounds_of_shots_against',
        'xGoalsFromActualReboundsOfShotsAgainst': 'mp_xgoals_from_actual_rebounds_of_shots_against',
        'reboundxGoalsAgainst': 'mp_rebound_xgoals_against',
        'totalShotCreditAgainst': 'mp_total_shot_credit_against',
        'scoreAdjustedTotalShotCreditAgainst': 'mp_score_adjusted_total_shot_credit_against',
        'scoreFlurryAdjustedTotalShotCreditAgainst': 'mp_score_flurry_adjusted_total_shot_credit_against',
    }

    @staticmethod
    def normalize_team_abbr(team_abbr: str) -> str:
        """Normalize team abbreviation to match our database"""
        return MoneyPuckTeamImporter.TEAM_ABBR_MAP.get(team_abbr, team_abbr)

    @staticmethod
    def get_team_id(cursor: sqlite3.Cursor, team_abbr: str) -> Optional[int]:
        """Get team_id from database"""
        normalized = MoneyPuckTeamImporter.normalize_team_abbr(team_abbr)
        cursor.execute("SELECT team_id FROM teams WHERE team_abbr = ?", (normalized,))
        result = cursor.fetchone()
        return result[0] if result else None

    @staticmethod
    def get_situation_id(cursor: sqlite3.Cursor, mp_situation: str) -> Optional[int]:
        """Get situation_id from database"""
        our_situation = MoneyPuckTeamImporter.SITUATION_MAP.get(mp_situation)
        if not our_situation:
            return None
        cursor.execute("SELECT situation_id FROM situations WHERE situation_code = ?", (our_situation,))
        result = cursor.fetchone()
        return result[0] if result else None

    @staticmethod
    def ensure_tables(conn: sqlite3.Connection):
        """Ensure MoneyPuck tables exist by running schema file"""
        schema_path = Path(MoneyPuckTeamImporter.SCHEMA_PATH)

        if not schema_path.exists():
            print(f"Warning: Schema file not found at {schema_path}")
            return

        cursor = conn.cursor()
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_sql = f.read()
            cursor.executescript(schema_sql)
        conn.commit()

    @staticmethod
    def ensure_other_situation(cursor: sqlite3.Cursor) -> int:
        """Ensure 'other' situation exists in situations table"""
        cursor.execute("SELECT situation_id FROM situations WHERE situation_code = 'other'")
        result = cursor.fetchone()

        if result:
            return result[0]

        cursor.execute("""
            INSERT INTO situations (situation_code, situation_name, description)
            VALUES ('other', 'Other', 'Other game situations not covered by standard categories')
        """)
        return cursor.lastrowid

    @staticmethod
    def clear_table(conn: sqlite3.Connection):
        """Clear team MoneyPuck table"""
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mp_team_game_stats")
        conn.commit()

    @staticmethod
    def import_csv(csv_path: str, conn: sqlite3.Connection, game_ids: Optional[Set[str]] = None) -> Tuple[int, int]:
        """Import a single team CSV file. Returns: (rows_imported, rows_skipped)"""
        cursor = conn.cursor()

        team_abbr = Path(csv_path).stem
        team_id = MoneyPuckTeamImporter.get_team_id(cursor, team_abbr)

        if not team_id:
            return 0, 0

        rows_imported = 0
        rows_skipped = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
                game_id = row.get('gameId')
                
                # Filter by game_id if provided
                if game_ids is not None and game_id not in game_ids:
                    continue

                mp_situation = row.get('situation')

                if not game_id or not mp_situation:
                    rows_skipped += 1
                    continue

                situation_id = MoneyPuckTeamImporter.get_situation_id(cursor, mp_situation)
                if not situation_id:
                    rows_skipped += 1
                    continue

                db_values = {
                    'game_id': game_id,
                    'team_id': team_id,
                    'situation_id': situation_id,
                }

                # Map CSV columns to database columns
                for csv_col, db_col in MoneyPuckTeamImporter.COLUMN_MAP.items():
                    if csv_col in row and row[csv_col]:
                        value = row[csv_col]
                        try:
                            db_values[db_col] = float(value) if '.' in value else int(value)
                        except ValueError:
                            db_values[db_col] = value

                # Build SQL
                columns = list(db_values.keys())
                placeholders = ['?' for _ in columns]
                values = [db_values[col] for col in columns]

                sql = f"""
                    INSERT OR REPLACE INTO mp_team_game_stats ({', '.join(columns)})
                    VALUES ({', '.join(placeholders)})
                """

                try:
                    cursor.execute(sql, values)
                    rows_imported += 1
                except sqlite3.Error:
                    rows_skipped += 1

        conn.commit()
        return rows_imported, rows_skipped

    @staticmethod
    def import_all(game_ids: Optional[Set[str]] = None, season_type: str = 'regular'):
        """Import all team CSV files"""
        conn = sqlite3.connect(MoneyPuckTeamImporter.DB_PATH)
        cursor = conn.cursor()

        # Ensure tables exist
        MoneyPuckTeamImporter.ensure_tables(conn)

        # Ensure 'other' situation exists
        MoneyPuckTeamImporter.ensure_other_situation(cursor)
        conn.commit()

        # Clear existing data ONLY on a full regular-season rebuild.
        # Never clear on a playoff pass: playoff rows append onto regular ones
        # (game_ids are disjoint), so clearing would wipe the regular data.
        if game_ids is None and season_type == 'regular':
            MoneyPuckTeamImporter.clear_table(conn)

        data_dir = season_data_dir(MoneyPuckTeamImporter.MP_TEAM_DATA_DIR, season_type)

        print("\n" + "="*80)
        print(f"IMPORTING MONEYPUCK TEAM DATA [{season_type}] from {data_dir}")
        if game_ids:
            print(f"Filtering for {len(game_ids)} specific games")
        print("="*80 + "\n")

        team_files = sorted(Path(data_dir).glob('*.csv'))

        total_imported = 0
        total_skipped = 0

        for csv_file in team_files:
            team_abbr = csv_file.stem
            # print(f"Importing {team_abbr}...", end=' ')

            imported, skipped = MoneyPuckTeamImporter.import_csv(str(csv_file), conn, game_ids)
            total_imported += imported
            total_skipped += skipped

            # print(f"✓ {imported} rows imported, {skipped} skipped")

        conn.close()

        # Summary
        print("\n" + "="*80)
        print("IMPORT SUMMARY - TEAMS")
        print("="*80)
        print(f"Total teams processed: {len(team_files)}")
        print(f"Total rows imported: {total_imported}")
        print(f"Total rows skipped: {total_skipped}")
        print("="*80)


class MoneyPuckPlayerImporter:
    """Imports MoneyPuck skater and goalie game-by-game data"""

    DB_PATH = 'nhl_analytics.db'
    MP_SKATER_DATA_DIR = 'moneypuck_data/skaters'
    MP_GOALIE_DATA_DIR = 'moneypuck_data/goalies'
    SCHEMA_PATH = 'moneypuck_schema.sql'

    # MoneyPuck situation codes → our situation IDs
    SITUATION_MAP = {
        '5on5': '5v5',
        'all': 'All',
        '4on5': 'PK',
        '5on4': 'PP',
        'other': 'other'
    }

    # Team abbreviation mapping
    TEAM_ABBR_MAP = {
        'L.A': 'LA',
        'N.J': 'NJ',
        'S.J': 'SJ',
        'T.B': 'TB',
        'LAK': 'LA',
        'SJS': 'SJ',
        'TBL': 'TB',
        'NJD': 'NJ',
    }

    # SKATER Column mapping
    SKATER_COLUMN_MAP = {
        # Metadata
        'season': 'mp_season',
        'home_or_away': 'mp_home_or_away',
        'gameDate': 'mp_game_date',
        'position': 'mp_position',

        # Ice time and percentages
        'icetime': 'mp_ice_time',
        'onIce_xGoalsPercentage': 'mp_xgoals_percentage',
        'onIce_corsiPercentage': 'mp_corsi_percentage',
        'onIce_fenwickPercentage': 'mp_fenwick_percentage',

        # Individual stats (I_F prefix)
        'I_F_xGoals': 'mp_i_f_xgoals',
        'I_F_goals': 'mp_i_f_goals',
        'I_F_primaryAssists': 'mp_i_f_first_assists',
        'I_F_secondaryAssists': 'mp_i_f_second_assists',
        'I_F_points': 'mp_i_f_points',
        'I_F_shotsOnGoal': 'mp_i_f_shots_on_goal',
        'I_F_missedShots': 'mp_i_f_missed_shots',
        'I_F_shotAttempts': 'mp_i_f_shots',
        'I_F_blockedShotAttempts': 'mp_i_f_blocked_shot_attempts',
        'I_F_rebounds': 'mp_i_f_rebounds_created',
        'penaltiesDrawn': 'mp_i_f_penalties_drawn',
        'I_F_takeaways': 'mp_i_f_takeaways',
        'I_F_giveaways': 'mp_i_f_giveaways',
        'I_F_hits': 'mp_i_f_hits',
        'I_F_faceOffsWon': 'mp_i_f_faceoffs_won',
        'faceoffsLost': 'mp_i_f_faceoffs_lost',

        # Individual shot danger
        'I_F_lowDangerShots': 'mp_i_f_low_danger_shots',
        'I_F_mediumDangerShots': 'mp_i_f_medium_danger_shots',
        'I_F_highDangerShots': 'mp_i_f_high_danger_shots',
        'I_F_lowDangerxGoals': 'mp_i_f_low_danger_xgoals',
        'I_F_mediumDangerxGoals': 'mp_i_f_medium_danger_xgoals',
        'I_F_highDangerxGoals': 'mp_i_f_high_danger_xgoals',
        'I_F_lowDangerGoals': 'mp_i_f_low_danger_goals',
        'I_F_mediumDangerGoals': 'mp_i_f_medium_danger_goals',
        'I_F_highDangerGoals': 'mp_i_f_high_danger_goals',

        # On-ice stats (FOR)
        'OnIce_F_xGoals': 'mp_onice_f_xgoals',
        'OnIce_F_goals': 'mp_onice_f_goals',
        'OnIce_F_shotsOnGoal': 'mp_onice_f_shots_on_goal',
        'OnIce_F_missedShots': 'mp_onice_f_missed_shots',
        'OnIce_F_blockedShotAttempts': 'mp_onice_f_blocked_shot_attempts',
        'OnIce_F_shotAttempts': 'mp_onice_f_shot_attempts',
        'OnIce_F_rebounds': 'mp_onice_f_rebounds',
        'OnIce_F_reboundGoals': 'mp_onice_f_rebound_goals',
        'OnIce_F_faceOffsWon': 'mp_onice_f_faceoffs_won',
        'OnIce_F_hits': 'mp_onice_f_hits',
        'OnIce_F_takeaways': 'mp_onice_f_takeaways',
        'OnIce_F_giveaways': 'mp_onice_f_giveaways',

        # On-ice shot danger (FOR)
        'OnIce_F_lowDangerShots': 'mp_onice_f_low_danger_shots',
        'OnIce_F_mediumDangerShots': 'mp_onice_f_medium_danger_shots',
        'OnIce_F_highDangerShots': 'mp_onice_f_high_danger_shots',
        'OnIce_F_lowDangerxGoals': 'mp_onice_f_low_danger_xgoals',
        'OnIce_F_mediumDangerxGoals': 'mp_onice_f_medium_danger_xgoals',
        'OnIce_F_highDangerxGoals': 'mp_onice_f_high_danger_xgoals',
        'OnIce_F_lowDangerGoals': 'mp_onice_f_low_danger_goals',
        'OnIce_F_mediumDangerGoals': 'mp_onice_f_medium_danger_goals',
        'OnIce_F_highDangerGoals': 'mp_onice_f_high_danger_goals',

        # On-ice stats (AGAINST)
        'OnIce_A_xGoals': 'mp_onice_a_xgoals',
        'OnIce_A_goals': 'mp_onice_a_goals',
        'OnIce_A_shotsOnGoal': 'mp_onice_a_shots_on_goal',
        'OnIce_A_missedShots': 'mp_onice_a_missed_shots',
        'OnIce_A_blockedShotAttempts': 'mp_onice_a_blocked_shot_attempts',
        'OnIce_A_shotAttempts': 'mp_onice_a_shot_attempts',
        'OnIce_A_rebounds': 'mp_onice_a_rebounds',
        'OnIce_A_reboundGoals': 'mp_onice_a_rebound_goals',
        'OnIce_A_hits': 'mp_onice_a_hits',
        'OnIce_A_takeaways': 'mp_onice_a_takeaways',
        'OnIce_A_giveaways': 'mp_onice_a_giveaways',

        # On-ice shot danger (AGAINST)
        'OnIce_A_lowDangerShots': 'mp_onice_a_low_danger_shots',
        'OnIce_A_mediumDangerShots': 'mp_onice_a_medium_danger_shots',
        'OnIce_A_highDangerShots': 'mp_onice_a_high_danger_shots',
        'OnIce_A_lowDangerxGoals': 'mp_onice_a_low_danger_xgoals',
        'OnIce_A_mediumDangerxGoals': 'mp_onice_a_medium_danger_xgoals',
        'OnIce_A_highDangerxGoals': 'mp_onice_a_high_danger_xgoals',
        'OnIce_A_lowDangerGoals': 'mp_onice_a_low_danger_goals',
        'OnIce_A_mediumDangerGoals': 'mp_onice_a_medium_danger_goals',
        'OnIce_A_highDangerGoals': 'mp_onice_a_high_danger_goals',

        # Off-ice stats
        'OffIce_F_xGoals': 'mp_office_f_xgoals',
        'OffIce_A_xGoals': 'mp_office_a_xgoals',
    }

    # GOALIE Column mapping
    GOALIE_COLUMN_MAP = {
        # Metadata
        'season': 'mp_season',
        'home_or_away': 'mp_home_or_away',
        'gameDate': 'mp_game_date',

        # Ice time
        'icetime': 'mp_ice_time',

        # Goals and xG against
        'xGoals': 'mp_xgoals_against',
        'goals': 'mp_goals_against',

        # Shots against
        'ongoal': 'mp_shots_on_goal_against',

        # Shot danger breakdown
        'lowDangerShots': 'mp_low_danger_shots_against',
        'mediumDangerShots': 'mp_medium_danger_shots_against',
        'highDangerShots': 'mp_high_danger_shots_against',
        'lowDangerxGoals': 'mp_low_danger_xgoals_against',
        'mediumDangerxGoals': 'mp_medium_danger_xgoals_against',
        'highDangerxGoals': 'mp_high_danger_xgoals_against',
        'lowDangerGoals': 'mp_low_danger_goals_against',
        'mediumDangerGoals': 'mp_medium_danger_goals_against',
        'highDangerGoals': 'mp_high_danger_goals_against',

        # Rebounds
        'rebounds': 'mp_rebounds_against',
        'xRebounds': 'mp_rebound_xgoals_against',

        # Adjusted metrics
        'flurryAdjustedxGoals': 'mp_flurry_adjusted_xgoals_against',
    }

    @staticmethod
    def normalize_team_abbr(team_abbr: str) -> str:
        """Normalize team abbreviation"""
        return MoneyPuckPlayerImporter.TEAM_ABBR_MAP.get(team_abbr, team_abbr)

    @staticmethod
    def get_team_id(cursor: sqlite3.Cursor, team_abbr: str) -> Optional[int]:
        """Get team_id from database"""
        normalized = MoneyPuckPlayerImporter.normalize_team_abbr(team_abbr)
        cursor.execute("SELECT team_id FROM teams WHERE team_abbr = ?", (normalized,))
        result = cursor.fetchone()
        return result[0] if result else None

    @staticmethod
    def get_situation_id(cursor: sqlite3.Cursor, mp_situation: str) -> Optional[int]:
        """Get situation_id from database"""
        our_situation = MoneyPuckPlayerImporter.SITUATION_MAP.get(mp_situation)
        if not our_situation:
            return None
        cursor.execute("SELECT situation_id FROM situations WHERE situation_code = ?", (our_situation,))
        result = cursor.fetchone()
        return result[0] if result else None

    @staticmethod
    def ensure_tables(conn: sqlite3.Connection):
        """Ensure MoneyPuck tables exist by running schema file"""
        schema_path = Path(MoneyPuckPlayerImporter.SCHEMA_PATH)

        if not schema_path.exists():
            print(f"Warning: Schema file not found at {schema_path}")
            return

        cursor = conn.cursor()
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_sql = f.read()
            cursor.executescript(schema_sql)
        conn.commit()

    @staticmethod
    def clear_tables(conn: sqlite3.Connection):
        """Clear player MoneyPuck tables"""
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mp_skater_game_stats")
        cursor.execute("DELETE FROM mp_goalie_game_stats")
        conn.commit()

    @staticmethod
    def get_or_create_player(cursor: sqlite3.Cursor, player_id: str, name: str, position: str):
        """Ensure player exists in database, create if not"""
        cursor.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
        if cursor.fetchone():
            return

        # Insert new player
        try:
            cursor.execute(
                "INSERT INTO players (player_id, player_name, position) VALUES (?, ?, ?)",
                (player_id, name, position)
            )
        except sqlite3.Error as e:
            print(f"Error creating player {name} ({player_id}): {e}")

    @staticmethod
    def import_skater_csv(csv_path: str, conn: sqlite3.Connection, game_ids: Optional[Set[str]] = None) -> Tuple[int, int]:
        """Import a single skater CSV file"""
        cursor = conn.cursor()
        player_id = Path(csv_path).stem

        rows_imported = 0
        rows_skipped = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
                game_id = row.get('gameId')
                
                # Filter by game_id if provided
                if game_ids is not None and game_id not in game_ids:
                    continue

                # Ensure player exists using data from first row
                name = row.get('name')
                position = row.get('position')
                if name and position:
                    MoneyPuckPlayerImporter.get_or_create_player(cursor, player_id, name, position)

                game_id = row.get('gameId')
                mp_situation = row.get('situation')
                team_abbr = row.get('playerTeam')

                if not all([game_id, mp_situation, team_abbr]):
                    rows_skipped += 1
                    continue

                team_id = MoneyPuckPlayerImporter.get_team_id(cursor, team_abbr)
                situation_id = MoneyPuckPlayerImporter.get_situation_id(cursor, mp_situation)

                if not team_id or not situation_id:
                    rows_skipped += 1
                    continue

                db_values = {
                    'game_id': game_id,
                    'player_id': player_id,
                    'team_id': team_id,
                    'situation_id': situation_id,
                }

                # Map columns
                for csv_col, db_col in MoneyPuckPlayerImporter.SKATER_COLUMN_MAP.items():
                    if csv_col in row and row[csv_col]:
                        value = row[csv_col]
                        try:
                            db_values[db_col] = float(value) if '.' in value else int(value)
                        except (ValueError, TypeError):
                            db_values[db_col] = value

                # Build SQL
                columns = list(db_values.keys())
                placeholders = ['?' for _ in columns]
                values = [db_values[col] for col in columns]

                sql = f"""
                    INSERT OR REPLACE INTO mp_skater_game_stats ({', '.join(columns)})
                    VALUES ({', '.join(placeholders)})
                """

                try:
                    cursor.execute(sql, values)
                    rows_imported += 1
                except sqlite3.Error:
                    rows_skipped += 1

        return rows_imported, rows_skipped

    @staticmethod
    def import_goalie_csv(csv_path: str, conn: sqlite3.Connection, game_ids: Optional[Set[str]] = None) -> Tuple[int, int]:
        """Import a single goalie CSV file"""
        cursor = conn.cursor()
        player_id = Path(csv_path).stem

        rows_imported = 0
        rows_skipped = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
                game_id = row.get('gameId')
                
                # Filter by game_id if provided
                if game_ids is not None and game_id not in game_ids:
                    continue

                # Ensure player exists using data from first row
                name = row.get('name')
                position = row.get('position')
                if name and position:
                    MoneyPuckPlayerImporter.get_or_create_player(cursor, player_id, name, position)

                game_id = row.get('gameId')
                mp_situation = row.get('situation')
                team_abbr = row.get('playerTeam')

                if not all([game_id, mp_situation, team_abbr]):
                    rows_skipped += 1
                    continue

                team_id = MoneyPuckPlayerImporter.get_team_id(cursor, team_abbr)
                situation_id = MoneyPuckPlayerImporter.get_situation_id(cursor, mp_situation)

                if not team_id or not situation_id:
                    rows_skipped += 1
                    continue

                db_values = {
                    'game_id': game_id,
                    'player_id': player_id,
                    'team_id': team_id,
                    'situation_id': situation_id,
                }

                # Map columns
                for csv_col, db_col in MoneyPuckPlayerImporter.GOALIE_COLUMN_MAP.items():
                    if csv_col in row and row[csv_col]:
                        value = row[csv_col]
                        try:
                            db_values[db_col] = float(value) if '.' in value else int(value)
                        except (ValueError, TypeError):
                            db_values[db_col] = value

                # Calculate saves and save percentage
                if 'mp_shots_on_goal_against' in db_values and 'mp_goals_against' in db_values:
                    shots = db_values['mp_shots_on_goal_against']
                    goals = db_values['mp_goals_against']
                    if shots > 0:
                        db_values['mp_saves'] = shots - goals
                        db_values['mp_save_percentage'] = (shots - goals) / shots

                # Build SQL
                columns = list(db_values.keys())
                placeholders = ['?' for _ in columns]
                values = [db_values[col] for col in columns]

                sql = f"""
                    INSERT OR REPLACE INTO mp_goalie_game_stats ({', '.join(columns)})
                    VALUES ({', '.join(placeholders)})
                """

                try:
                    cursor.execute(sql, values)
                    rows_imported += 1
                except sqlite3.Error:
                    rows_skipped += 1

        return rows_imported, rows_skipped

    @staticmethod
    def import_all(game_ids: Optional[Set[str]] = None, season_type: str = 'regular'):
        """Import all skater and goalie data"""
        conn = sqlite3.connect(MoneyPuckPlayerImporter.DB_PATH)

        # Ensure tables exist
        MoneyPuckPlayerImporter.ensure_tables(conn)

        # Clear existing data ONLY on a full regular-season rebuild.
        # Never clear on a playoff pass (rows append onto regular ones).
        if game_ids is None and season_type == 'regular':
            MoneyPuckPlayerImporter.clear_tables(conn)

        skater_dir = season_data_dir(MoneyPuckPlayerImporter.MP_SKATER_DATA_DIR, season_type)
        goalie_dir = season_data_dir(MoneyPuckPlayerImporter.MP_GOALIE_DATA_DIR, season_type)

        print("\n" + "="*80)
        print(f"IMPORTING MONEYPUCK SKATER DATA [{season_type}] from {skater_dir}")
        if game_ids:
            print(f"Filtering for {len(game_ids)} specific games")
        print("="*80 + "\n")

        skater_files = sorted(Path(skater_dir).glob('*.csv'))
        total_skater_imported = 0
        total_skater_skipped = 0
        skaters_processed = 0

        for i, csv_file in enumerate(skater_files, 1):
            imported, skipped = MoneyPuckPlayerImporter.import_skater_csv(str(csv_file), conn, game_ids)
            if imported > 0 or skipped > 0:
                skaters_processed += 1
                total_skater_imported += imported
                total_skater_skipped += skipped

                if i % 50 == 0:
                    conn.commit()
                    print(f"Processed {i}/{len(skater_files)} skaters... ({total_skater_imported} records)")

        conn.commit()
        print(f"\n✓ Skaters: {skaters_processed} players, {total_skater_imported} records, {total_skater_skipped} skipped\n")

        print("="*80)
        print("IMPORTING MONEYPUCK GOALIE DATA")
        print("="*80 + "\n")

        goalie_files = sorted(Path(goalie_dir).glob('*.csv'))
        total_goalie_imported = 0
        total_goalie_skipped = 0
        goalies_processed = 0

        for csv_file in goalie_files:
            imported, skipped = MoneyPuckPlayerImporter.import_goalie_csv(str(csv_file), conn, game_ids)
            if imported > 0 or skipped > 0:
                goalies_processed += 1
                total_goalie_imported += imported
                total_goalie_skipped += skipped

        conn.commit()
        print(f"✓ Goalies: {goalies_processed} players, {total_goalie_imported} records, {total_goalie_skipped} skipped\n")

        conn.close()

        # Summary
        print("="*80)
        print("IMPORT SUMMARY - PLAYERS")
        print("="*80)
        print(f"Skaters processed: {skaters_processed}")
        print(f"Skater records imported: {total_skater_imported}")
        print(f"Skater records skipped: {total_skater_skipped}")
        print()
        print(f"Goalies processed: {goalies_processed}")
        print(f"Goalie records imported: {total_goalie_imported}")
        print(f"Goalie records skipped: {total_goalie_skipped}")
        print("="*80)


class MoneyPuckOddsImporter:
    """Imports MoneyPuck betting odds data"""

    DB_PATH = 'nhl_analytics.db'
    SCHEMA_PATH = 'moneypuck_schema.sql'

    # Sportsbooks to extract from wide-format CSV
    SPORTSBOOKS = ['betano', 'betmgm', 'bovada', 'draftkings', 'fanduel', 'pinnacle', 'sia']

    @staticmethod
    def parse_percentage(pct_str: str) -> Optional[float]:
        """Convert percentage string (e.g., '58.6%') to decimal (0.586)"""
        if not pct_str or pct_str == '':
            return None
        try:
            # Remove '%' and convert to decimal
            return float(pct_str.strip().replace('%', '')) / 100.0
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def parse_american_odds(odds_str: str) -> Optional[int]:
        """Parse American odds string (e.g., '-150', '+125') to integer"""
        if not odds_str or odds_str == '':
            return None
        try:
            # Remove leading '+' if present, keep '-'
            return int(odds_str.strip().replace('+', ''))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def ensure_tables(conn: sqlite3.Connection):
        """Ensure odds table exists - wide format with one row per game"""
        cursor = conn.cursor()

        # Create single wide-format game_odds table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_odds (
                game_id TEXT PRIMARY KEY,

                -- MoneyPuck win probabilities
                mp_away_win_prob REAL,
                mp_home_win_prob REAL,

                -- Scrape metadata
                scrape_status TEXT,
                error_message TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                -- Betano
                betano_opening_timestamp TEXT,
                betano_opening_away_odds INTEGER,
                betano_opening_home_odds INTEGER,
                betano_closing_timestamp TEXT,
                betano_closing_away_odds INTEGER,
                betano_closing_home_odds INTEGER,

                -- BetMGM
                betmgm_opening_timestamp TEXT,
                betmgm_opening_away_odds INTEGER,
                betmgm_opening_home_odds INTEGER,
                betmgm_closing_timestamp TEXT,
                betmgm_closing_away_odds INTEGER,
                betmgm_closing_home_odds INTEGER,

                -- Bovada
                bovada_opening_timestamp TEXT,
                bovada_opening_away_odds INTEGER,
                bovada_opening_home_odds INTEGER,
                bovada_closing_timestamp TEXT,
                bovada_closing_away_odds INTEGER,
                bovada_closing_home_odds INTEGER,

                -- DraftKings
                draftkings_opening_timestamp TEXT,
                draftkings_opening_away_odds INTEGER,
                draftkings_opening_home_odds INTEGER,
                draftkings_closing_timestamp TEXT,
                draftkings_closing_away_odds INTEGER,
                draftkings_closing_home_odds INTEGER,

                -- FanDuel
                fanduel_opening_timestamp TEXT,
                fanduel_opening_away_odds INTEGER,
                fanduel_opening_home_odds INTEGER,
                fanduel_closing_timestamp TEXT,
                fanduel_closing_away_odds INTEGER,
                fanduel_closing_home_odds INTEGER,

                -- Pinnacle
                pinnacle_opening_timestamp TEXT,
                pinnacle_opening_away_odds INTEGER,
                pinnacle_opening_home_odds INTEGER,
                pinnacle_closing_timestamp TEXT,
                pinnacle_closing_away_odds INTEGER,
                pinnacle_closing_home_odds INTEGER,

                -- SIA
                sia_opening_timestamp TEXT,
                sia_opening_away_odds INTEGER,
                sia_opening_home_odds INTEGER,
                sia_closing_timestamp TEXT,
                sia_closing_away_odds INTEGER,
                sia_closing_home_odds INTEGER,

                FOREIGN KEY (game_id) REFERENCES games(game_id)
            )
        """)

        # Create index
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_game_odds_game ON game_odds(game_id)")

        conn.commit()

    @staticmethod
    def clear_tables(conn: sqlite3.Connection):
        """Clear odds table"""
        cursor = conn.cursor()
        cursor.execute("DELETE FROM game_odds")
        conn.commit()

    @staticmethod
    def import_csv(csv_path: str, conn: sqlite3.Connection, game_ids: Optional[Set[str]] = None) -> Tuple[int, int]:
        """
        Import a single odds CSV file (wide format).
        Returns: (games_imported, games_skipped)
        """
        cursor = conn.cursor()

        games_imported = 0
        games_skipped = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
                traditional_game_id = row.get('traditional_game_id', '').strip()

                if not traditional_game_id:
                    games_skipped += 1
                    continue
                    
                # Filter by game_id if provided
                if game_ids is not None and traditional_game_id not in game_ids:
                    continue

                # Parse MoneyPuck win probabilities
                mp_away_win_prob = MoneyPuckOddsImporter.parse_percentage(row.get('mp_away_win_prob'))
                mp_home_win_prob = MoneyPuckOddsImporter.parse_percentage(row.get('mp_home_win_prob'))
                scrape_status = row.get('scrape_status', '').strip()
                error_message = row.get('error_message', '').strip()

                # Build the values dict for all columns
                values_dict = {
                    'game_id': traditional_game_id,
                    'mp_away_win_prob': mp_away_win_prob,
                    'mp_home_win_prob': mp_home_win_prob,
                    'scrape_status': scrape_status if scrape_status else None,
                    'error_message': error_message if error_message else None
                }

                # Parse all sportsbook odds
                for sportsbook in MoneyPuckOddsImporter.SPORTSBOOKS:
                    opening_ts = row.get(f'{sportsbook}_opening_timestamp', '').strip()
                    opening_away = MoneyPuckOddsImporter.parse_american_odds(
                        row.get(f'{sportsbook}_opening_away_odds', ''))
                    opening_home = MoneyPuckOddsImporter.parse_american_odds(
                        row.get(f'{sportsbook}_opening_home_odds', ''))
                    closing_ts = row.get(f'{sportsbook}_closing_timestamp', '').strip()
                    closing_away = MoneyPuckOddsImporter.parse_american_odds(
                        row.get(f'{sportsbook}_closing_away_odds', ''))
                    closing_home = MoneyPuckOddsImporter.parse_american_odds(
                        row.get(f'{sportsbook}_closing_home_odds', ''))

                    values_dict[f'{sportsbook}_opening_timestamp'] = opening_ts if opening_ts else None
                    values_dict[f'{sportsbook}_opening_away_odds'] = opening_away
                    values_dict[f'{sportsbook}_opening_home_odds'] = opening_home
                    values_dict[f'{sportsbook}_closing_timestamp'] = closing_ts if closing_ts else None
                    values_dict[f'{sportsbook}_closing_away_odds'] = closing_away
                    values_dict[f'{sportsbook}_closing_home_odds'] = closing_home

                # Build INSERT statement
                columns = list(values_dict.keys())
                placeholders = ['?' for _ in columns]
                values = [values_dict[col] for col in columns]

                sql = f"""
                    INSERT OR REPLACE INTO game_odds ({', '.join(columns)})
                    VALUES ({', '.join(placeholders)})
                """

                try:
                    cursor.execute(sql, values)
                    games_imported += 1
                except sqlite3.Error as e:
                    games_skipped += 1
                    print(f"Error importing game {traditional_game_id}: {e}")

        conn.commit()
        return games_imported, games_skipped

    @staticmethod
    def import_all(game_ids: Optional[Set[str]] = None):
        """Import all odds CSV files"""
        conn = sqlite3.connect(MoneyPuckOddsImporter.DB_PATH)

        # Ensure tables exist
        MoneyPuckOddsImporter.ensure_tables(conn)

        # Clear existing data ONLY if not filtering
        if game_ids is None:
            print("\nClearing existing odds data...")
            MoneyPuckOddsImporter.clear_tables(conn)

        print("\n" + "="*80)
        print("IMPORTING MONEYPUCK ODDS DATA")
        if game_ids:
            print(f"Filtering for {len(game_ids)} specific games")
        print("="*80 + "\n")

        # Find all odds files (moneypuck_odds_*.csv in current directory)
        odds_files = sorted(Path('.').glob('moneypuck_odds_*.csv'))

        if not odds_files:
            print("No moneypuck_odds CSV files found in current directory")
            conn.close()
            return

        total_imported = 0
        total_skipped = 0

        for csv_file in odds_files:
            season = csv_file.stem.replace('moneypuck_odds_', '')
            print(f"Importing {season} season odds...", end=' ', flush=True)

            imported, skipped = MoneyPuckOddsImporter.import_csv(str(csv_file), conn, game_ids)
            total_imported += imported
            total_skipped += skipped

            print(f"✓ {imported:,} games imported, {skipped} skipped")

        conn.close()

        # Summary
        print("\n" + "="*80)
        print("IMPORT SUMMARY - ODDS")
        print("="*80)
        print(f"Total files processed: {len(odds_files)}")
        print(f"Total games imported: {total_imported:,}")
        print(f"Total games skipped: {total_skipped:,}")
        print("="*80)


class MoneyPuckShotsImporter:
    """Imports MoneyPuck shot-level data"""

    DB_PATH = 'nhl_analytics.db'
    MP_SHOTS_DATA_DIR = 'moneypuck_data'
    SCHEMA_PATH = 'moneypuck_schema.sql'

    # CSV column → Database column mapping
    COLUMN_MAP = {
        # Core identifiers
        'shotID': 'shot_id',
        'game_id': 'game_id',

        # Event details
        'event': 'event',
        'shotType': 'shot_type',
        'period': 'period',
        'time': 'time',

        # Teams and players
        'teamCode': 'team_code',
        'shooterPlayerId': 'shooter_player_id',
        'shooterName': 'shooter_name',
        'goalieIdForShot': 'goalie_player_id',
        'goalieNameForShot': 'goalie_name',

        # Shot location
        'xCordAdjusted': 'x_cord_adjusted',
        'yCordAdjusted': 'y_cord_adjusted',
        'shotDistance': 'shot_distance',
        'shotAngle': 'shot_angle',
        'shotAngleAdjusted': 'shot_angle_adjusted',

        # Shot outcome
        'goal': 'goal',
        'shotWasOnGoal': 'shot_was_on_goal',
        'shotGeneratedRebound': 'shot_generated_rebound',
        'shotGoalieFroze': 'shot_goalie_froze',
        'shotOnEmptyNet': 'shot_on_empty_net',
        'shotPlayContinuedInZone': 'shot_play_continued_in_zone',
        'shotPlayContinuedOutsideZone': 'shot_play_continued_outside_zone',
        'shotPlayStopped': 'shot_play_stopped',
        'shotRebound': 'shot_rebound',
        'shotRush': 'shot_rush',

        # Expected values
        'xGoal': 'x_goal',
        'xFroze': 'x_froze',
        'xRebound': 'x_rebound',
        'xPlayContinuedInZone': 'x_play_continued_in_zone',
        'xPlayContinuedOutsideZone': 'x_play_continued_outside_zone',
        'xPlayStopped': 'x_play_stopped',
        'xShotWasOnGoal': 'x_shot_was_on_goal',

        # Game state
        'homeTeamCode': 'home_team_code',
        'awayTeamCode': 'away_team_code',
        'homeTeamGoals': 'home_team_goals',
        'awayTeamGoals': 'away_team_goals',
        'homeSkatersOnIce': 'home_skaters_on_ice',
        'awaySkatersOnIce': 'away_skaters_on_ice',
        'homeEmptyNet': 'home_empty_net',
        'awayEmptyNet': 'away_empty_net',
        'isHomeTeam': 'is_home_team',

        # Penalty situation
        'homePenalty1Length': 'home_penalty_1_length',
        'homePenalty1TimeLeft': 'home_penalty_1_time_left',
        'awayPenalty1Length': 'away_penalty_1_length',
        'awayPenalty1TimeLeft': 'away_penalty_1_time_left',

        # Shot context
        'location': 'location',
        'offWing': 'off_wing',
        'shotAnglePlusRebound': 'shot_angle_plus_rebound',
        'shotAnglePlusReboundSpeed': 'shot_angle_plus_rebound_speed',
        'shotAngleReboundRoyalRoad': 'shot_angle_rebound_royal_road',

        # Shooter context
        'shooterLeftRight': 'shooter_left_right',
        'playerPositionThatDidEvent': 'shooter_position',
        'shooterTimeOnIce': 'shooter_time_on_ice',
        'shooterTimeOnIceSinceFaceoff': 'shooter_time_on_ice_since_faceoff',

        # Last event context
        'lastEventCategory': 'last_event_category',
        'lastEventTeam': 'last_event_team',
        'lastEventShotAngle': 'last_event_shot_angle',
        'lastEventShotDistance': 'last_event_shot_distance',
        'lastEventxCord_adjusted': 'last_event_x_cord_adjusted',
        'lastEventyCord_adjusted': 'last_event_y_cord_adjusted',
        'distanceFromLastEvent': 'distance_from_last_event',
        'timeSinceLastEvent': 'time_since_last_event',
        'speedFromLastEvent': 'speed_from_last_event',

        # Shift/change context
        'timeSinceFaceoff': 'time_since_faceoff',
        'timeDifferenceSinceChange': 'time_difference_since_change',
        'timeUntilNextEvent': 'time_until_next_event',
        'averageRestDifference': 'average_rest_difference',

        # Shooting team TOI metrics
        'shootingTeamAverageTimeOnIce': 'shooting_team_average_time_on_ice',
        'shootingTeamAverageTimeOnIceOfDefencemen': 'shooting_team_average_time_on_ice_of_defencemen',
        'shootingTeamAverageTimeOnIceOfForwards': 'shooting_team_average_time_on_ice_of_forwards',
        'shootingTeamAverageTimeOnIceSinceFaceoff': 'shooting_team_average_time_on_ice_since_faceoff',
        'shootingTeamMaxTimeOnIce': 'shooting_team_max_time_on_ice',
        'shootingTeamMinTimeOnIce': 'shooting_team_min_time_on_ice',
        'shootingTeamDefencemenOnIce': 'shooting_team_defencemen_on_ice',
        'shootingTeamForwardsOnIce': 'shooting_team_forwards_on_ice',

        # Defending team TOI metrics
        'defendingTeamAverageTimeOnIce': 'defending_team_average_time_on_ice',
        'defendingTeamAverageTimeOnIceOfDefencemen': 'defending_team_average_time_on_ice_of_defencemen',
        'defendingTeamAverageTimeOnIceOfForwards': 'defending_team_average_time_on_ice_of_forwards',
        'defendingTeamAverageTimeOnIceSinceFaceoff': 'defending_team_average_time_on_ice_since_faceoff',
        'defendingTeamMaxTimeOnIce': 'defending_team_max_time_on_ice',
        'defendingTeamMinTimeOnIce': 'defending_team_min_time_on_ice',
        'defendingTeamDefencemenOnIce': 'defending_team_defencemen_on_ice',
        'defendingTeamForwardsOnIce': 'defending_team_forwards_on_ice',

        # Game metadata
        'season': 'season',
        'homeTeamWon': 'home_team_won',
        'isPlayoffGame': 'is_playoff_game',
    }

    @staticmethod
    def ensure_tables(conn: sqlite3.Connection):
        """Ensure MoneyPuck tables exist by running schema file"""
        schema_path = Path(MoneyPuckShotsImporter.SCHEMA_PATH)

        if not schema_path.exists():
            print(f"Warning: Schema file not found at {schema_path}")
            return

        cursor = conn.cursor()
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_sql = f.read()
            cursor.executescript(schema_sql)
        conn.commit()

    @staticmethod
    def clear_table(conn: sqlite3.Connection):
        """Clear shots table"""
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mp_shots")
        conn.commit()

    @staticmethod
    def import_csv(csv_path: str, conn: sqlite3.Connection) -> Tuple[int, int]:
        """Import a single shots CSV file. Returns: (rows_imported, rows_skipped)"""
        cursor = conn.cursor()

        rows_imported = 0
        rows_skipped = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            batch = []
            batch_size = 1000

            for row in reader:
                shot_id = row.get('shotID')
                game_id = row.get('game_id')  # Short format (e.g., "20001")
                season = row.get('season')    # Year (e.g., "2024")

                if not shot_id or not game_id or not season:
                    rows_skipped += 1
                    continue

                # Convert short game_id to traditional format
                # Regular season: season + "02" + game_id (e.g., "2024" + "02" + "20001" = "2024020001")
                # Playoffs: season + "03" + game_id (e.g., "2024" + "03" + "30111" = "2024030111")
                game_id_str = str(game_id)
                if game_id_str.startswith('3'):
                    # Playoff game
                    traditional_game_id = f"{season}03{game_id_str[1:]}"
                else:
                    # Regular season game (starts with 2)
                    traditional_game_id = f"{season}02{game_id_str[1:]}"

                db_values = {}

                # Map CSV columns to database columns
                for csv_col, db_col in MoneyPuckShotsImporter.COLUMN_MAP.items():
                    if csv_col in row and row[csv_col] not in ('', 'NA', 'NaN'):
                        value = row[csv_col]
                        # Override game_id with traditional format
                        if db_col == 'game_id':
                            db_values[db_col] = traditional_game_id
                            continue
                        try:
                            # Attempt numeric conversion
                            if '.' in str(value):
                                db_values[db_col] = float(value)
                            else:
                                db_values[db_col] = int(value)
                        except (ValueError, TypeError):
                            # Keep as string
                            db_values[db_col] = value

                batch.append(db_values)

                # Insert in batches
                if len(batch) >= batch_size:
                    rows_imported += MoneyPuckShotsImporter._insert_batch(cursor, batch)
                    batch = []

            # Insert remaining rows
            if batch:
                rows_imported += MoneyPuckShotsImporter._insert_batch(cursor, batch)

        conn.commit()
        return rows_imported, rows_skipped

    @staticmethod
    def _insert_batch(cursor: sqlite3.Cursor, batch: list) -> int:
        """Insert a batch of rows"""
        if not batch:
            return 0

        inserted = 0
        for db_values in batch:
            columns = list(db_values.keys())
            placeholders = ['?' for _ in columns]
            values = [db_values[col] for col in columns]

            sql = f"""
                INSERT OR REPLACE INTO mp_shots ({', '.join(columns)})
                VALUES ({', '.join(placeholders)})
            """

            try:
                cursor.execute(sql, values)
                inserted += 1
            except sqlite3.Error as e:
                # Skip this row on error
                pass

        return inserted

    @staticmethod
    def import_all(season: Optional[int] = None):
        """
        Import shots CSV files.

        Args:
            season: Optional season year to import (e.g., 2025 for shots_2025.csv).
                   If None, imports all seasons and clears the table first.
                   If specified, only imports that season (updates existing data).
        """
        conn = sqlite3.connect(MoneyPuckShotsImporter.DB_PATH)

        # Ensure tables exist
        MoneyPuckShotsImporter.ensure_tables(conn)

        # Only clear table if doing a full import (no season specified)
        if season is None:
            print("\nClearing existing shots data...")
            MoneyPuckShotsImporter.clear_table(conn)

        print("\n" + "="*80)
        if season:
            print(f"IMPORTING MONEYPUCK SHOT DATA - {season-1}-{season} SEASON")
        else:
            print("IMPORTING MONEYPUCK SHOT DATA - ALL SEASONS")
        print("="*80 + "\n")

        # Find shots files
        if season:
            # Import only the specified season
            shots_files = list(Path(MoneyPuckShotsImporter.MP_SHOTS_DATA_DIR).glob(f'shots_{season}.csv'))
        else:
            # Import all seasons
            shots_files = sorted(Path(MoneyPuckShotsImporter.MP_SHOTS_DATA_DIR).glob('shots_*.csv'))

        if not shots_files:
            if season:
                print(f"No shots file found for season {season}")
            else:
                print(f"No shots files found in {MoneyPuckShotsImporter.MP_SHOTS_DATA_DIR}/")
            conn.close()
            return

        total_imported = 0
        total_skipped = 0

        for csv_file in shots_files:
            season_str = csv_file.stem.replace('shots_', '')
            print(f"Importing {season_str} season shots...", end=' ', flush=True)

            imported, skipped = MoneyPuckShotsImporter.import_csv(str(csv_file), conn)
            total_imported += imported
            total_skipped += skipped

            print(f"✓ {imported:,} shots imported")

        conn.close()

        # Summary
        print("\n" + "="*80)
        print("IMPORT SUMMARY - SHOTS")
        print("="*80)
        print(f"Total files processed: {len(shots_files)}")
        print(f"Total shots imported: {total_imported:,}")
        print(f"Total shots skipped: {total_skipped:,}")
        print("="*80)


if __name__ == '__main__':
    import sys

    # Optional season type as a trailing arg: regular (default) | playoffs | both
    # e.g. `python mp_import.py teams playoffs`  or  `python mp_import.py all both`
    season_arg = 'regular'
    argv = sys.argv[1:]
    if argv and argv[-1].lower() in ('regular', 'playoffs', 'both'):
        season_arg = argv.pop().lower()
    season_types = ['regular', 'playoffs'] if season_arg == 'both' else [season_arg]

    if argv:
        mode = argv[0].lower()
        if mode == 'teams':
            for st in season_types:
                MoneyPuckTeamImporter.import_all(season_type=st)
        elif mode == 'players':
            for st in season_types:
                MoneyPuckPlayerImporter.import_all(season_type=st)
        elif mode == 'shots':
            MoneyPuckShotsImporter.import_all()  # shots files are not split by season type
        elif mode == 'odds':
            MoneyPuckOddsImporter.import_all()
        elif mode == 'all':
            # Import everything
            for st in season_types:
                MoneyPuckTeamImporter.import_all(season_type=st)
                MoneyPuckPlayerImporter.import_all(season_type=st)
            MoneyPuckShotsImporter.import_all()
            MoneyPuckOddsImporter.import_all()
        else:
            print("Usage: python mp_import.py [teams|players|shots|odds|all] [regular|playoffs|both]")
            print("  teams   - Import only team data")
            print("  players - Import only player data")
            print("  shots   - Import only shot data")
            print("  odds    - Import only odds data")
            print("  all     - Import everything (default)")
            print("  Trailing season type (default 'regular') controls regular vs playoff CSVs.")
    else:
        # Import everything by default (no args)
        for st in season_types:
            MoneyPuckTeamImporter.import_all(season_type=st)
            MoneyPuckPlayerImporter.import_all(season_type=st)
        MoneyPuckShotsImporter.import_all()
        MoneyPuckOddsImporter.import_all()
