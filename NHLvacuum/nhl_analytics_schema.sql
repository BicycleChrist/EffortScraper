-- NHL Analytics Database Schema
-- Optimized for game-by-game analytics and simulations
-- Uses TEXT game_id (actual NHL game ID) instead of auto-increment

-- ============================================================================
-- DIMENSION TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_abbr TEXT NOT NULL UNIQUE,
    team_name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,  -- NHL player ID
    player_name TEXT NOT NULL,
    position TEXT CHECK(position IN ('C', 'L', 'R', 'D', 'G', 'F')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,  -- Actual NHL game ID (e.g., "2015020457")
    home_team_id INTEGER NOT NULL,
    away_team_id INTEGER NOT NULL,
    game_date DATE NOT NULL,
    season TEXT,  -- e.g., "2024-2025"
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (away_team_id) REFERENCES teams(team_id)
);

CREATE TABLE IF NOT EXISTS situations (
    situation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    situation_code TEXT NOT NULL UNIQUE,  -- '5v5', 'All', 'EV', 'PP', 'PK'
    situation_name TEXT NOT NULL,
    description TEXT
);

-- ============================================================================
-- FACT TABLES - Player Stats
-- ============================================================================

CREATE TABLE IF NOT EXISTS player_game_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,

    -- Time on ice
    toi_seconds REAL,

    -- Scoring stats
    goals INTEGER DEFAULT 0,
    total_assists INTEGER DEFAULT 0,
    first_assists INTEGER DEFAULT 0,
    second_assists INTEGER DEFAULT 0,
    total_points INTEGER DEFAULT 0,

    -- Shooting stats
    shots INTEGER DEFAULT 0,
    shooting_pct REAL,
    ixg REAL,  -- Individual expected goals

    -- Corsi/Fenwick individual
    icf INTEGER DEFAULT 0,  -- Individual Corsi For
    iscf INTEGER DEFAULT 0,  -- Individual Scoring Chances For
    ihdcf INTEGER DEFAULT 0,  -- Individual High Danger Chances For

    -- Rush and rebound stats
    rush_attempts INTEGER DEFAULT 0,
    rebound_attempts INTEGER DEFAULT 0,
    rebounds_created INTEGER DEFAULT 0,

    -- Discipline
    pim INTEGER DEFAULT 0,
    total_penalties INTEGER DEFAULT 0,
    minor_penalties INTEGER DEFAULT 0,
    major_penalties INTEGER DEFAULT 0,
    misconduct_penalties INTEGER DEFAULT 0,
    penalties_drawn INTEGER DEFAULT 0,

    -- Possession metrics
    giveaways INTEGER DEFAULT 0,
    takeaways INTEGER DEFAULT 0,

    -- Physical play
    hits INTEGER DEFAULT 0,
    hits_taken INTEGER DEFAULT 0,
    shots_blocked INTEGER DEFAULT 0,

    -- Faceoffs
    faceoffs_won INTEGER DEFAULT 0,
    faceoffs_lost INTEGER DEFAULT 0,
    faceoff_pct REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id),
    UNIQUE(game_id, player_id, team_id, situation_id)
);

CREATE TABLE IF NOT EXISTS goalie_game_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,

    -- Time on ice
    toi_seconds REAL,

    -- Overall stats
    shots_against INTEGER DEFAULT 0,
    saves INTEGER DEFAULT 0,
    goals_against INTEGER DEFAULT 0,
    expected_goals_against REAL,
    save_pct REAL,
    gaa REAL,  -- Goals Against Average

    -- High Danger stats
    hd_shots_against INTEGER DEFAULT 0,
    hd_saves INTEGER DEFAULT 0,
    hd_goals_against INTEGER DEFAULT 0,
    hd_save_pct REAL,

    -- Medium Danger stats
    md_shots_against INTEGER DEFAULT 0,
    md_saves INTEGER DEFAULT 0,
    md_goals_against INTEGER DEFAULT 0,
    md_save_pct REAL,

    -- Low Danger stats
    ld_shots_against INTEGER DEFAULT 0,
    ld_saves INTEGER DEFAULT 0,
    ld_goals_against INTEGER DEFAULT 0,
    ld_save_pct REAL,

    -- Shot type stats
    rush_shots_against INTEGER DEFAULT 0,
    rebound_shots_against INTEGER DEFAULT 0,

    -- Shot quality
    avg_shot_distance REAL,
    avg_goal_distance REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id),
    UNIQUE(game_id, player_id, team_id, situation_id)
);

CREATE TABLE IF NOT EXISTS player_onice_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,

    -- Time on ice
    toi_seconds REAL,

    -- Corsi (all shot attempts)
    cf INTEGER DEFAULT 0,  -- Corsi For
    ca INTEGER DEFAULT 0,  -- Corsi Against
    cf_pct REAL,
    cf_pct_rel REAL,  -- Relative to team average

    -- Fenwick (unblocked shot attempts)
    ff INTEGER DEFAULT 0,
    fa INTEGER DEFAULT 0,
    ff_pct REAL,
    ff_pct_rel REAL,

    -- Shots
    sf INTEGER DEFAULT 0,
    sa INTEGER DEFAULT 0,
    sf_pct REAL,
    sf_pct_rel REAL,

    -- Goals
    gf INTEGER DEFAULT 0,
    ga INTEGER DEFAULT 0,
    gf_pct REAL,
    gf_pct_rel REAL,

    -- Expected Goals
    xgf REAL,
    xga REAL,
    xgf_pct REAL,
    xgf_pct_rel REAL,

    -- Scoring Chances
    scf INTEGER DEFAULT 0,
    sca INTEGER DEFAULT 0,
    scf_pct REAL,
    scf_pct_rel REAL,

    -- High Danger Chances
    hdcf INTEGER DEFAULT 0,
    hdca INTEGER DEFAULT 0,
    hdcf_pct REAL,
    hdcf_pct_rel REAL,

    -- Zone starts
    off_zone_shift_starts INTEGER DEFAULT 0,
    neu_zone_shift_starts INTEGER DEFAULT 0,
    def_zone_shift_starts INTEGER DEFAULT 0,
    otf_shift_starts INTEGER DEFAULT 0,  -- On the fly
    off_zone_shift_start_pct REAL,

    -- Faceoffs
    off_zone_faceoffs INTEGER DEFAULT 0,
    neu_zone_faceoffs INTEGER DEFAULT 0,
    def_zone_faceoffs INTEGER DEFAULT 0,
    off_zone_faceoff_pct REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id),
    UNIQUE(game_id, player_id, team_id, situation_id)
);

CREATE TABLE IF NOT EXISTS player_shift_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,

    -- Time on ice
    toi_seconds REAL,

    -- Shift metrics
    shifts INTEGER DEFAULT 0,
    avg_shift_length REAL,
    shift_std_dev REAL,
    short_shifts INTEGER DEFAULT 0,
    long_shifts INTEGER DEFAULT 0,
    extra_short_shifts INTEGER DEFAULT 0,
    extra_long_shifts INTEGER DEFAULT 0,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id),
    UNIQUE(game_id, player_id, team_id, situation_id)
);

CREATE TABLE IF NOT EXISTS line_combinations (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,
    player1_id TEXT NOT NULL,
    player2_id TEXT NOT NULL,
    player3_id TEXT NOT NULL,

    -- Time on ice together
    toi_seconds REAL,

    -- Corsi
    cf INTEGER DEFAULT 0,
    ca INTEGER DEFAULT 0,
    cf_pct REAL,
    cf_pct_rel REAL,

    -- Fenwick
    ff INTEGER DEFAULT 0,
    fa INTEGER DEFAULT 0,
    ff_pct REAL,
    ff_pct_rel REAL,

    -- Shots
    sf INTEGER DEFAULT 0,
    sa INTEGER DEFAULT 0,
    sf_pct REAL,
    sf_pct_rel REAL,

    -- Goals
    gf INTEGER DEFAULT 0,
    ga INTEGER DEFAULT 0,
    gf_pct REAL,
    gf_pct_rel REAL,

    -- Expected Goals
    xgf REAL,
    xga REAL,
    xgf_pct REAL,
    xgf_pct_rel REAL,

    -- Scoring Chances
    scf INTEGER DEFAULT 0,
    sca INTEGER DEFAULT 0,
    scf_pct REAL,
    scf_pct_rel REAL,

    -- High Danger Chances
    hdcf INTEGER DEFAULT 0,
    hdca INTEGER DEFAULT 0,
    hdcf_pct REAL,
    hdcf_pct_rel REAL,

    -- Rush attempts
    rush_attempts_for INTEGER DEFAULT 0,
    rush_attempts_against INTEGER DEFAULT 0,
    rush_attempt_pct REAL,
    rush_attempt_pct_rel REAL,

    -- Rebound attempts
    rebound_attempts_for INTEGER DEFAULT 0,
    rebound_attempts_against INTEGER DEFAULT 0,
    rebound_attempt_pct REAL,
    rebound_attempt_pct_rel REAL,

    -- Zone starts
    off_zone_faceoffs INTEGER DEFAULT 0,
    neu_zone_faceoffs INTEGER DEFAULT 0,
    def_zone_faceoffs INTEGER DEFAULT 0,
    off_zone_faceoff_pct REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id),
    FOREIGN KEY (player1_id) REFERENCES players(player_id),
    FOREIGN KEY (player2_id) REFERENCES players(player_id),
    FOREIGN KEY (player3_id) REFERENCES players(player_id),
    UNIQUE(game_id, team_id, situation_id, player1_id, player2_id, player3_id)
);

CREATE TABLE IF NOT EXISTS team_game_overview (
    game_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    situation_id INTEGER NOT NULL,
    period INTEGER NOT NULL,  -- 1, 2, 3, or 0 for 'Final'

    -- Time on ice
    toi_seconds REAL,

    -- Corsi
    cf REAL DEFAULT 0,
    ca REAL DEFAULT 0,
    cf_pct REAL,

    -- Fenwick
    ff REAL DEFAULT 0,
    fa REAL DEFAULT 0,
    ff_pct REAL,

    -- Shots
    sf REAL DEFAULT 0,
    sa REAL DEFAULT 0,
    sf_pct REAL,

    -- Scoring Chances
    scf REAL DEFAULT 0,
    sca REAL DEFAULT 0,
    scf_pct REAL,

    -- High Danger Chances
    hdcf REAL DEFAULT 0,
    hdca REAL DEFAULT 0,
    hdcf_pct REAL,

    -- Expected Goals
    xgf REAL,
    xga REAL,
    xgf_pct REAL,

    -- Actual Goals
    gf REAL DEFAULT 0,
    ga REAL DEFAULT 0,
    gf_pct REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (game_id, team_id, situation_id, period),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (situation_id) REFERENCES situations(situation_id)
);

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Game indexes
CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date);
CREATE INDEX IF NOT EXISTS idx_games_teams ON games(home_team_id, away_team_id);
CREATE INDEX IF NOT EXISTS idx_games_season ON games(season);

-- Player stats indexes
CREATE INDEX IF NOT EXISTS idx_player_stats_game ON player_game_stats(game_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_player ON player_game_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_team ON player_game_stats(team_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_situation ON player_game_stats(situation_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_composite ON player_game_stats(player_id, team_id, situation_id);

-- Goalie stats indexes
CREATE INDEX IF NOT EXISTS idx_goalie_stats_game ON goalie_game_stats(game_id);
CREATE INDEX IF NOT EXISTS idx_goalie_stats_player ON goalie_game_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_goalie_stats_team ON goalie_game_stats(team_id);
CREATE INDEX IF NOT EXISTS idx_goalie_stats_situation ON goalie_game_stats(situation_id);

-- On-ice stats indexes
CREATE INDEX IF NOT EXISTS idx_onice_stats_game ON player_onice_stats(game_id);
CREATE INDEX IF NOT EXISTS idx_onice_stats_player ON player_onice_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_onice_stats_team ON player_onice_stats(team_id);
CREATE INDEX IF NOT EXISTS idx_onice_stats_situation ON player_onice_stats(situation_id);
CREATE INDEX IF NOT EXISTS idx_onice_stats_composite ON player_onice_stats(player_id, team_id, situation_id);

-- Shift stats indexes
CREATE INDEX IF NOT EXISTS idx_shift_stats_game ON player_shift_stats(game_id);
CREATE INDEX IF NOT EXISTS idx_shift_stats_player ON player_shift_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_shift_stats_composite ON player_shift_stats(player_id, situation_id);

-- Line combinations indexes
CREATE INDEX IF NOT EXISTS idx_lines_game ON line_combinations(game_id);
CREATE INDEX IF NOT EXISTS idx_lines_team ON line_combinations(team_id);
CREATE INDEX IF NOT EXISTS idx_lines_players ON line_combinations(player1_id, player2_id, player3_id);
CREATE INDEX IF NOT EXISTS idx_lines_situation ON line_combinations(situation_id);

-- Team overview indexes
CREATE INDEX IF NOT EXISTS idx_overview_game ON team_game_overview(game_id);
CREATE INDEX IF NOT EXISTS idx_overview_team ON team_game_overview(team_id);
CREATE INDEX IF NOT EXISTS idx_overview_period ON team_game_overview(period);

-- ============================================================================
-- VIEWS
-- ============================================================================

CREATE VIEW IF NOT EXISTS v_player_season_stats AS
SELECT
    p.player_name,
    p.position,
    t.team_abbr,
    s.situation_code,
    g.season,
    COUNT(DISTINCT ps.game_id) as games_played,
    SUM(ps.toi_seconds) / 60.0 as total_toi_minutes,
    SUM(ps.goals) as goals,
    SUM(ps.total_assists) as assists,
    SUM(ps.total_points) as points,
    SUM(ps.shots) as shots,
    CASE WHEN SUM(ps.shots) > 0
         THEN (CAST(SUM(ps.goals) AS REAL) / SUM(ps.shots)) * 100
         ELSE NULL END as shooting_pct,
    SUM(ps.ixg) as total_ixg,
    SUM(ps.hits) as hits,
    SUM(ps.shots_blocked) as blocks,
    CASE WHEN SUM(ps.faceoffs_won + ps.faceoffs_lost) > 0
         THEN (CAST(SUM(ps.faceoffs_won) AS REAL) / SUM(ps.faceoffs_won + ps.faceoffs_lost)) * 100
         ELSE NULL END as faceoff_pct
FROM player_game_stats ps
JOIN players p ON ps.player_id = p.player_id
JOIN teams t ON ps.team_id = t.team_id
JOIN situations s ON ps.situation_id = s.situation_id
JOIN games g ON ps.game_id = g.game_id
GROUP BY p.player_id, t.team_id, s.situation_id, g.season;

CREATE VIEW IF NOT EXISTS v_player_season_onice AS
SELECT
    p.player_name,
    p.position,
    t.team_abbr,
    s.situation_code,
    g.season,
    COUNT(DISTINCT oi.game_id) as games_played,
    SUM(oi.toi_seconds) / 60.0 as total_toi_minutes,
    AVG(oi.cf_pct) as avg_cf_pct,
    AVG(oi.ff_pct) as avg_ff_pct,
    AVG(oi.xgf_pct) as avg_xgf_pct,
    AVG(oi.hdcf_pct) as avg_hdcf_pct,
    SUM(oi.gf) as total_gf,
    SUM(oi.ga) as total_ga,
    SUM(oi.xgf) as total_xgf,
    SUM(oi.xga) as total_xga
FROM player_onice_stats oi
JOIN players p ON oi.player_id = p.player_id
JOIN teams t ON oi.team_id = t.team_id
JOIN situations s ON oi.situation_id = s.situation_id
JOIN games g ON oi.game_id = g.game_id
GROUP BY p.player_id, t.team_id, s.situation_id, g.season;

CREATE VIEW IF NOT EXISTS v_goalie_season_stats AS
SELECT
    p.player_name,
    t.team_abbr,
    s.situation_code,
    g.season,
    COUNT(DISTINCT gs.game_id) as games_played,
    SUM(gs.toi_seconds) / 60.0 as total_toi_minutes,
    SUM(gs.shots_against) as shots_against,
    SUM(gs.saves) as saves,
    SUM(gs.goals_against) as goals_against,
    CASE WHEN SUM(gs.shots_against) > 0
         THEN (CAST(SUM(gs.saves) AS REAL) / SUM(gs.shots_against)) * 100
         ELSE NULL END as save_pct,
    CASE WHEN SUM(gs.toi_seconds) > 0
         THEN (CAST(SUM(gs.goals_against) AS REAL) / SUM(gs.toi_seconds)) * 3600
         ELSE NULL END as gaa,
    SUM(gs.expected_goals_against) as total_xga,
    CASE WHEN SUM(gs.hd_shots_against) > 0
         THEN (CAST(SUM(gs.hd_saves) AS REAL) / SUM(gs.hd_shots_against)) * 100
         ELSE NULL END as hd_save_pct
FROM goalie_game_stats gs
JOIN players p ON gs.player_id = p.player_id
JOIN teams t ON gs.team_id = t.team_id
JOIN situations s ON gs.situation_id = s.situation_id
JOIN games g ON gs.game_id = g.game_id
GROUP BY p.player_id, t.team_id, s.situation_id, g.season;
