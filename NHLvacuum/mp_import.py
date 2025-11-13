#!/usr/bin/env python3
"""
MoneyPuck Data Importer

Import MoneyPuck team and player data into the NHL analytics database.
Contains separate classes for team and player imports.
"""

import sqlite3
import csv
from pathlib import Path
from typing import Dict, Optional, Tuple


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
    def import_csv(csv_path: str, conn: sqlite3.Connection) -> Tuple[int, int]:
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
    def import_all():
        """Import all team CSV files"""
        conn = sqlite3.connect(MoneyPuckTeamImporter.DB_PATH)
        cursor = conn.cursor()

        # Ensure tables exist
        MoneyPuckTeamImporter.ensure_tables(conn)

        # Ensure 'other' situation exists
        MoneyPuckTeamImporter.ensure_other_situation(cursor)
        conn.commit()

        # Clear existing data
        MoneyPuckTeamImporter.clear_table(conn)

        print("\n" + "="*80)
        print("IMPORTING MONEYPUCK TEAM DATA")
        print("="*80 + "\n")

        team_files = sorted(Path(MoneyPuckTeamImporter.MP_TEAM_DATA_DIR).glob('*.csv'))

        total_imported = 0
        total_skipped = 0

        for csv_file in team_files:
            team_abbr = csv_file.stem
            print(f"Importing {team_abbr}...", end=' ')

            imported, skipped = MoneyPuckTeamImporter.import_csv(str(csv_file), conn)
            total_imported += imported
            total_skipped += skipped

            print(f"✓ {imported} rows imported, {skipped} skipped")

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
    def import_skater_csv(csv_path: str, conn: sqlite3.Connection) -> Tuple[int, int]:
        """Import a single skater CSV file"""
        cursor = conn.cursor()
        player_id = Path(csv_path).stem

        # Verify player exists
        cursor.execute("SELECT COUNT(*) FROM players WHERE player_id = ?", (player_id,))
        if cursor.fetchone()[0] == 0:
            return 0, 0

        rows_imported = 0
        rows_skipped = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
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
    def import_goalie_csv(csv_path: str, conn: sqlite3.Connection) -> Tuple[int, int]:
        """Import a single goalie CSV file"""
        cursor = conn.cursor()
        player_id = Path(csv_path).stem

        # Verify player exists
        cursor.execute("SELECT COUNT(*) FROM players WHERE player_id = ? AND position = 'G'", (player_id,))
        if cursor.fetchone()[0] == 0:
            return 0, 0

        rows_imported = 0
        rows_skipped = 0

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
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
    def import_all():
        """Import all skater and goalie data"""
        conn = sqlite3.connect(MoneyPuckPlayerImporter.DB_PATH)

        # Ensure tables exist
        MoneyPuckPlayerImporter.ensure_tables(conn)

        # Clear existing data
        MoneyPuckPlayerImporter.clear_tables(conn)

        print("\n" + "="*80)
        print("IMPORTING MONEYPUCK SKATER DATA")
        print("="*80 + "\n")

        skater_files = sorted(Path(MoneyPuckPlayerImporter.MP_SKATER_DATA_DIR).glob('*.csv'))
        total_skater_imported = 0
        total_skater_skipped = 0
        skaters_processed = 0

        for i, csv_file in enumerate(skater_files, 1):
            imported, skipped = MoneyPuckPlayerImporter.import_skater_csv(str(csv_file), conn)
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

        goalie_files = sorted(Path(MoneyPuckPlayerImporter.MP_GOALIE_DATA_DIR).glob('*.csv'))
        total_goalie_imported = 0
        total_goalie_skipped = 0
        goalies_processed = 0

        for csv_file in goalie_files:
            imported, skipped = MoneyPuckPlayerImporter.import_goalie_csv(str(csv_file), conn)
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


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        if mode == 'teams':
            MoneyPuckTeamImporter.import_all()
        elif mode == 'players':
            MoneyPuckPlayerImporter.import_all()
        else:
            print("Usage: python mp_import.py [teams|players|all]")
            print("  teams   - Import only team data")
            print("  players - Import only player data")
            print("  all     - Import everything (default)")
    else:
        # Import everything by default
        MoneyPuckTeamImporter.import_all()
        MoneyPuckPlayerImporter.import_all()
