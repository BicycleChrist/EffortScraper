-- MoneyPuck Data Schema
-- Separate tables for MoneyPuck data to keep clean separation from NST data

-- MoneyPuck Team Game Stats
CREATE TABLE IF NOT EXISTS mp_team_game_stats (
    game_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,

    -- Game metadata from MoneyPuck
    mp_season TEXT,
    mp_home_or_away TEXT,
    mp_game_date TEXT,

    -- Percentages and rates
    mp_xgoals_percentage REAL,
    mp_corsi_percentage REAL,
    mp_fenwick_percentage REAL,
    mp_ice_time REAL,

    -- Expected goals metrics (FOR)
    mp_xon_goal_for REAL,
    mp_xgoals_for REAL,
    mp_xrebounds_for REAL,
    mp_xfreeze_for REAL,
    mp_xplay_stopped_for REAL,
    mp_xplay_continued_in_zone_for REAL,
    mp_xplay_continued_outside_zone_for REAL,
    mp_flurry_adjusted_xgoals_for REAL,
    mp_score_venue_adjusted_xgoals_for REAL,
    mp_flurry_score_venue_adjusted_xgoals_for REAL,

    -- Shots and attempts (FOR)
    mp_shots_on_goal_for INTEGER,
    mp_missed_shots_for INTEGER,
    mp_blocked_shot_attempts_for INTEGER,
    mp_shot_attempts_for INTEGER,
    mp_goals_for INTEGER,
    mp_rebounds_for INTEGER,
    mp_rebound_goals_for INTEGER,
    mp_freeze_for INTEGER,
    mp_play_stopped_for INTEGER,
    mp_play_continued_in_zone_for INTEGER,
    mp_play_continued_outside_zone_for INTEGER,
    mp_saved_shots_on_goal_for INTEGER,
    mp_saved_unblocked_shot_attempts_for INTEGER,

    -- Other events (FOR)
    mp_penalties_for INTEGER,
    mp_penality_minutes_for INTEGER,
    mp_faceoffs_won_for INTEGER,
    mp_hits_for INTEGER,
    mp_takeaways_for INTEGER,
    mp_giveaways_for INTEGER,

    -- Shot danger breakdown (FOR)
    mp_low_danger_shots_for INTEGER,
    mp_medium_danger_shots_for INTEGER,
    mp_high_danger_shots_for INTEGER,
    mp_low_danger_xgoals_for REAL,
    mp_medium_danger_xgoals_for REAL,
    mp_high_danger_xgoals_for REAL,
    mp_low_danger_goals_for INTEGER,
    mp_medium_danger_goals_for INTEGER,
    mp_high_danger_goals_for INTEGER,

    -- Advanced metrics (FOR)
    mp_score_adjusted_shots_attempts_for REAL,
    mp_unblocked_shot_attempts_for INTEGER,
    mp_score_adjusted_unblocked_shot_attempts_for REAL,
    mp_dzone_giveaways_for INTEGER,
    mp_xgoals_from_xrebounds_of_shots_for REAL,
    mp_xgoals_from_actual_rebounds_of_shots_for REAL,
    mp_rebound_xgoals_for REAL,
    mp_total_shot_credit_for REAL,
    mp_score_adjusted_total_shot_credit_for REAL,
    mp_score_flurry_adjusted_total_shot_credit_for REAL,

    -- Expected goals metrics (AGAINST)
    mp_xon_goal_against REAL,
    mp_xgoals_against REAL,
    mp_xrebounds_against REAL,
    mp_xfreeze_against REAL,
    mp_xplay_stopped_against REAL,
    mp_xplay_continued_in_zone_against REAL,
    mp_xplay_continued_outside_zone_against REAL,
    mp_flurry_adjusted_xgoals_against REAL,
    mp_score_venue_adjusted_xgoals_against REAL,
    mp_flurry_score_venue_adjusted_xgoals_against REAL,

    -- Shots and attempts (AGAINST)
    mp_shots_on_goal_against INTEGER,
    mp_missed_shots_against INTEGER,
    mp_blocked_shot_attempts_against INTEGER,
    mp_shot_attempts_against INTEGER,
    mp_goals_against INTEGER,
    mp_rebounds_against INTEGER,
    mp_rebound_goals_against INTEGER,
    mp_freeze_against INTEGER,
    mp_play_stopped_against INTEGER,
    mp_play_continued_in_zone_against INTEGER,
    mp_play_continued_outside_zone_against INTEGER,
    mp_saved_shots_on_goal_against INTEGER,
    mp_saved_unblocked_shot_attempts_against INTEGER,

    -- Other events (AGAINST)
    mp_penalties_against INTEGER,
    mp_penality_minutes_against INTEGER,
    mp_faceoffs_won_against INTEGER,
    mp_hits_against INTEGER,
    mp_takeaways_against INTEGER,
    mp_giveaways_against INTEGER,

    -- Shot danger breakdown (AGAINST)
    mp_low_danger_shots_against INTEGER,
    mp_medium_danger_shots_against INTEGER,
    mp_high_danger_shots_against INTEGER,
    mp_low_danger_xgoals_against REAL,
    mp_medium_danger_xgoals_against REAL,
    mp_high_danger_xgoals_against REAL,
    mp_low_danger_goals_against INTEGER,
    mp_medium_danger_goals_against INTEGER,
    mp_high_danger_goals_against INTEGER,

    -- Advanced metrics (AGAINST)
    mp_score_adjusted_shots_attempts_against REAL,
    mp_unblocked_shot_attempts_against INTEGER,
    mp_score_adjusted_unblocked_shot_attempts_against REAL,
    mp_dzone_giveaways_against INTEGER,
    mp_xgoals_from_xrebounds_of_shots_against REAL,
    mp_xgoals_from_actual_rebounds_of_shots_against REAL,
    mp_rebound_xgoals_against REAL,
    mp_total_shot_credit_against REAL,
    mp_score_adjusted_total_shot_credit_against REAL,
    mp_score_flurry_adjusted_total_shot_credit_against REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (game_id, team_id, situation_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id)
);

-- MoneyPuck Skater Game Stats
CREATE TABLE IF NOT EXISTS mp_skater_game_stats (
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,

    -- Game metadata
    mp_season TEXT,
    mp_home_or_away TEXT,
    mp_game_date TEXT,
    mp_position TEXT,

    -- Ice time and percentages
    mp_ice_time REAL,
    mp_xgoals_percentage REAL,
    mp_corsi_percentage REAL,
    mp_fenwick_percentage REAL,

    -- Individual stats (I_F prefix in CSV)
    mp_i_f_xgoals REAL,
    mp_i_f_goals INTEGER,
    mp_i_f_first_assists INTEGER,
    mp_i_f_second_assists INTEGER,
    mp_i_f_total_assists INTEGER,
    mp_i_f_points INTEGER,
    mp_i_f_shots INTEGER,
    mp_i_f_shots_on_goal INTEGER,
    mp_i_f_missed_shots INTEGER,
    mp_i_f_blocked_shot_attempts INTEGER,
    mp_i_f_rebounds_created INTEGER,
    mp_i_f_penalties_drawn INTEGER,
    mp_i_f_giveaways INTEGER,
    mp_i_f_takeaways INTEGER,
    mp_i_f_hits INTEGER,
    mp_i_f_faceoffs_won INTEGER,
    mp_i_f_faceoffs_lost INTEGER,

    -- Shot danger (individual)
    mp_i_f_low_danger_shots INTEGER,
    mp_i_f_medium_danger_shots INTEGER,
    mp_i_f_high_danger_shots INTEGER,
    mp_i_f_low_danger_xgoals REAL,
    mp_i_f_medium_danger_xgoals REAL,
    mp_i_f_high_danger_xgoals REAL,
    mp_i_f_low_danger_goals INTEGER,
    mp_i_f_medium_danger_goals INTEGER,
    mp_i_f_high_danger_goals INTEGER,

    -- On-ice stats (OnIce_F prefix)
    mp_onice_f_xgoals REAL,
    mp_onice_f_goals INTEGER,
    mp_onice_f_shots_on_goal INTEGER,
    mp_onice_f_missed_shots INTEGER,
    mp_onice_f_blocked_shot_attempts INTEGER,
    mp_onice_f_shot_attempts INTEGER,
    mp_onice_f_rebounds INTEGER,
    mp_onice_f_rebound_goals INTEGER,
    mp_onice_f_penalties INTEGER,
    mp_onice_f_penalties_drawn INTEGER,
    mp_onice_f_faceoffs_won INTEGER,
    mp_onice_f_hits INTEGER,
    mp_onice_f_takeaways INTEGER,
    mp_onice_f_giveaways INTEGER,

    -- On-ice shot danger (FOR)
    mp_onice_f_low_danger_shots INTEGER,
    mp_onice_f_medium_danger_shots INTEGER,
    mp_onice_f_high_danger_shots INTEGER,
    mp_onice_f_low_danger_xgoals REAL,
    mp_onice_f_medium_danger_xgoals REAL,
    mp_onice_f_high_danger_xgoals REAL,
    mp_onice_f_low_danger_goals INTEGER,
    mp_onice_f_medium_danger_goals INTEGER,
    mp_onice_f_high_danger_goals INTEGER,

    -- On-ice stats (AGAINST)
    mp_onice_a_xgoals REAL,
    mp_onice_a_goals INTEGER,
    mp_onice_a_shots_on_goal INTEGER,
    mp_onice_a_missed_shots INTEGER,
    mp_onice_a_blocked_shot_attempts INTEGER,
    mp_onice_a_shot_attempts INTEGER,
    mp_onice_a_rebounds INTEGER,
    mp_onice_a_rebound_goals INTEGER,
    mp_onice_a_penalties INTEGER,
    mp_onice_a_penalties_drawn INTEGER,
    mp_onice_a_faceoffs_won INTEGER,
    mp_onice_a_hits INTEGER,
    mp_onice_a_takeaways INTEGER,
    mp_onice_a_giveaways INTEGER,

    -- On-ice shot danger (AGAINST)
    mp_onice_a_low_danger_shots INTEGER,
    mp_onice_a_medium_danger_shots INTEGER,
    mp_onice_a_high_danger_shots INTEGER,
    mp_onice_a_low_danger_xgoals REAL,
    mp_onice_a_medium_danger_xgoals REAL,
    mp_onice_a_high_danger_xgoals REAL,
    mp_onice_a_low_danger_goals INTEGER,
    mp_onice_a_medium_danger_goals INTEGER,
    mp_onice_a_high_danger_goals INTEGER,

    -- Off-ice stats (OffIce prefix - team performance when player not on ice)
    mp_office_f_xgoals REAL,
    mp_office_a_xgoals REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (game_id, player_id, situation_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id)
);

-- MoneyPuck Goalie Game Stats
CREATE TABLE IF NOT EXISTS mp_goalie_game_stats (
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,

    -- Game metadata
    mp_season TEXT,
    mp_home_or_away TEXT,
    mp_game_date TEXT,

    -- Ice time
    mp_ice_time REAL,

    -- Goals and expected goals against
    mp_xgoals_against REAL,
    mp_goals_against INTEGER,

    -- Shots against
    mp_shots_on_goal_against INTEGER,
    mp_saves INTEGER,
    mp_save_percentage REAL,
    mp_goals_saved_above_expected REAL,

    -- Shot danger breakdown
    mp_low_danger_shots_against INTEGER,
    mp_medium_danger_shots_against INTEGER,
    mp_high_danger_shots_against INTEGER,
    mp_low_danger_xgoals_against REAL,
    mp_medium_danger_xgoals_against REAL,
    mp_high_danger_xgoals_against REAL,
    mp_low_danger_goals_against INTEGER,
    mp_medium_danger_goals_against INTEGER,
    mp_high_danger_goals_against INTEGER,

    -- Rebounds and other
    mp_rebounds_against INTEGER,
    mp_rebound_goals_against INTEGER,
    mp_rebound_xgoals_against REAL,

    -- Adjusted metrics
    mp_flurry_adjusted_xgoals_against REAL,
    mp_score_venue_adjusted_xgoals_against REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (game_id, player_id, situation_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id)
);

-- MoneyPuck Shot-Level Data
CREATE TABLE IF NOT EXISTS mp_shots (
    shot_id INTEGER NOT NULL,
    game_id TEXT NOT NULL,

    -- Event details
    event TEXT,  -- SHOT, GOAL, MISS
    shot_type TEXT,  -- WRIST, SLAP, SNAP, BACKHAND, DEFLECTED, TIP-IN, WRAP-AROUND
    period INTEGER,
    time INTEGER,  -- Game time in seconds

    -- Teams and players
    team_code TEXT,
    shooter_player_id TEXT,
    shooter_name TEXT,
    goalie_player_id TEXT,
    goalie_name TEXT,

    -- Shot location (adjusted coordinates)
    x_cord_adjusted REAL,
    y_cord_adjusted REAL,
    shot_distance REAL,
    shot_angle REAL,
    shot_angle_adjusted REAL,

    -- Shot outcome
    goal INTEGER,  -- 0 or 1
    shot_was_on_goal REAL,  -- Probability
    shot_generated_rebound INTEGER,
    shot_goalie_froze INTEGER,
    shot_on_empty_net INTEGER,
    shot_play_continued_in_zone INTEGER,
    shot_play_continued_outside_zone INTEGER,
    shot_play_stopped INTEGER,
    shot_rebound INTEGER,
    shot_rush INTEGER,

    -- Expected values
    x_goal REAL,  -- Expected goal probability
    x_froze REAL,
    x_rebound REAL,
    x_play_continued_in_zone REAL,
    x_play_continued_outside_zone REAL,
    x_play_stopped REAL,
    x_shot_was_on_goal REAL,

    -- Game state
    home_team_code TEXT,
    away_team_code TEXT,
    home_team_goals INTEGER,
    away_team_goals INTEGER,
    home_skaters_on_ice INTEGER,
    away_skaters_on_ice INTEGER,
    home_empty_net INTEGER,
    away_empty_net INTEGER,
    is_home_team INTEGER,  -- 1 if shooter's team is home, 0 if away

    -- Penalty situation
    home_penalty_1_length INTEGER,
    home_penalty_1_time_left INTEGER,
    away_penalty_1_length INTEGER,
    away_penalty_1_time_left INTEGER,

    -- Shot context
    location TEXT,  -- HOMEZONE, AWAYZONE
    off_wing INTEGER,
    shot_angle_plus_rebound REAL,
    shot_angle_plus_rebound_speed REAL,
    shot_angle_rebound_royal_road INTEGER,

    -- Shooter context
    shooter_left_right TEXT,  -- L or R
    shooter_position TEXT,  -- L, R, C, D
    shooter_time_on_ice REAL,
    shooter_time_on_ice_since_faceoff REAL,

    -- Last event context
    last_event_category TEXT,
    last_event_team TEXT,
    last_event_shot_angle REAL,
    last_event_shot_distance REAL,
    last_event_x_cord_adjusted REAL,
    last_event_y_cord_adjusted REAL,
    distance_from_last_event REAL,
    time_since_last_event REAL,
    speed_from_last_event REAL,

    -- Shift/change context
    time_since_faceoff REAL,
    time_difference_since_change REAL,
    time_until_next_event REAL,
    average_rest_difference REAL,

    -- Shooting team TOI metrics
    shooting_team_average_time_on_ice REAL,
    shooting_team_average_time_on_ice_of_defencemen REAL,
    shooting_team_average_time_on_ice_of_forwards REAL,
    shooting_team_average_time_on_ice_since_faceoff REAL,
    shooting_team_max_time_on_ice REAL,
    shooting_team_min_time_on_ice REAL,
    shooting_team_defencemen_on_ice INTEGER,
    shooting_team_forwards_on_ice INTEGER,

    -- Defending team TOI metrics
    defending_team_average_time_on_ice REAL,
    defending_team_average_time_on_ice_of_defencemen REAL,
    defending_team_average_time_on_ice_of_forwards REAL,
    defending_team_average_time_on_ice_since_faceoff REAL,
    defending_team_max_time_on_ice REAL,
    defending_team_min_time_on_ice REAL,
    defending_team_defencemen_on_ice INTEGER,
    defending_team_forwards_on_ice INTEGER,

    -- Game metadata
    season TEXT,
    home_team_won INTEGER,
    is_playoff_game INTEGER,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (shot_id, game_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_mp_team_game ON mp_team_game_stats(game_id, team_id);
CREATE INDEX IF NOT EXISTS idx_mp_team_situation ON mp_team_game_stats(situation_id);
CREATE INDEX IF NOT EXISTS idx_mp_skater_game ON mp_skater_game_stats(game_id, player_id);
CREATE INDEX IF NOT EXISTS idx_mp_skater_team ON mp_skater_game_stats(team_id);
CREATE INDEX IF NOT EXISTS idx_mp_skater_situation ON mp_skater_game_stats(situation_id);
CREATE INDEX IF NOT EXISTS idx_mp_goalie_game ON mp_goalie_game_stats(game_id, player_id);
CREATE INDEX IF NOT EXISTS idx_mp_goalie_team ON mp_goalie_game_stats(team_id);
CREATE INDEX IF NOT EXISTS idx_mp_goalie_situation ON mp_goalie_game_stats(situation_id);

-- Shot data indexes
CREATE INDEX IF NOT EXISTS idx_mp_shots_game ON mp_shots(game_id);
CREATE INDEX IF NOT EXISTS idx_mp_shots_shooter ON mp_shots(shooter_player_id);
CREATE INDEX IF NOT EXISTS idx_mp_shots_goalie ON mp_shots(goalie_player_id);
CREATE INDEX IF NOT EXISTS idx_mp_shots_team ON mp_shots(team_code);
CREATE INDEX IF NOT EXISTS idx_mp_shots_event ON mp_shots(event);
CREATE INDEX IF NOT EXISTS idx_mp_shots_goal ON mp_shots(goal);
CREATE INDEX IF NOT EXISTS idx_mp_shots_season ON mp_shots(season);
CREATE INDEX IF NOT EXISTS idx_mp_shots_type ON mp_shots(shot_type);
CREATE INDEX IF NOT EXISTS idx_mp_shots_xgoal ON mp_shots(x_goal);

-- Odds indexes
CREATE INDEX IF NOT EXISTS idx_game_odds_game ON game_odds(game_id);

-- ============================================================================
-- ODDS TABLE (Wide Format)
-- ============================================================================

-- Game odds - One row per game with all sportsbooks in columns
CREATE TABLE IF NOT EXISTS game_odds (
    game_id TEXT PRIMARY KEY,

    -- MoneyPuck win probabilities (stored as decimals: 0.586 for 58.6%)
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
);

-- ============================================================================
-- HELPER VIEWS
-- ============================================================================

-- View to properly link shots to games table
-- MoneyPuck uses season year (2024) and incremental game_id (20029)
-- NHL uses full game_id format: [season_start_year]02[game_number]
-- Example: MoneyPuck season=2024, game_id=20029 -> NHL game_id=2023020029
CREATE VIEW IF NOT EXISTS v_mp_shots_with_game AS
SELECT
    s.*,
    -- Reconstruct NHL game_id format: (season-1) + '02' + game_id
    -- MoneyPuck 2024 season = NHL 2023-2024 season
    printf('%d02%s', CAST(s.season AS INTEGER) - 1, s.game_id) as nhl_game_id
FROM mp_shots s;

-- View: Game odds with team information and Pinnacle lines
CREATE VIEW IF NOT EXISTS v_game_odds_summary AS
SELECT
    g.game_id,
    g.game_date,
    g.season,
    away_team.team_abbr as away_team,
    home_team.team_abbr as home_team,
    go.mp_away_win_prob,
    go.mp_home_win_prob,
    go.pinnacle_opening_away_odds,
    go.pinnacle_opening_home_odds,
    go.pinnacle_closing_away_odds,
    go.pinnacle_closing_home_odds,
    go.scrape_status
FROM game_odds go
JOIN games g ON go.game_id = g.game_id
JOIN teams away_team ON g.away_team_id = away_team.team_id
JOIN teams home_team ON g.home_team_id = home_team.team_id;

-- View: Pinnacle line movement with MoneyPuck probabilities
CREATE VIEW IF NOT EXISTS v_pinnacle_line_movement AS
SELECT
    g.game_id,
    g.game_date,
    g.season,
    away_team.team_abbr as away_team,
    home_team.team_abbr as home_team,
    go.mp_away_win_prob,
    go.mp_home_win_prob,
    go.pinnacle_opening_away_odds,
    go.pinnacle_closing_away_odds,
    go.pinnacle_closing_away_odds - go.pinnacle_opening_away_odds as away_line_movement,
    go.pinnacle_opening_home_odds,
    go.pinnacle_closing_home_odds,
    go.pinnacle_closing_home_odds - go.pinnacle_opening_home_odds as home_line_movement
FROM game_odds go
JOIN games g ON go.game_id = g.game_id
JOIN teams away_team ON g.away_team_id = away_team.team_id
JOIN teams home_team ON g.home_team_id = home_team.team_id
WHERE go.pinnacle_opening_away_odds IS NOT NULL
  AND go.pinnacle_closing_away_odds IS NOT NULL;
