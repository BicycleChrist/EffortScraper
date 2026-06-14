-- ============================================================================
--  AHL Database Schema  (SQLite)
-- ----------------------------------------------------------------------------
--  Source: ahl_data/game_<id>.json  — HockeyTech "gameSummary" feed
--          (lscluster.hockeytech.com, client_code=ahl), 25,311 games,
--          22 AHL seasons (2004-05 -> 2025-26), regular season + playoffs.
--
--  Design notes
--  ------------
--  * Dimensions (season, team, player) are de-duplicated; per-game facts
--    reference them by the HockeyTech integer id.
--  * Team/player attributes that drift over time (a team's city, a player's
--    jersey/position) are stored on the per-game fact rows, while the
--    dimension table keeps the stable identity + most-recently-seen label.
--  * "time" / "timeOnIce" values from the feed are mm:ss strings; we keep the
--    raw string AND a derived integer seconds column for easy math.
--  * Booleans arrive as the strings '0'/'1' (goal properties) or JSON bools
--    (shootout) — all normalised to INTEGER 0/1 here.
--  * Every fact table cascades from games so a game can be re-loaded cleanly.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
--  DIMENSIONS
-- ----------------------------------------------------------------------------

-- One row per HockeyTech season feed id. game_type distinguishes the regular
-- season feed from its paired playoff feed. label/start_year are seeded by the
-- loader from the known id->season map.
CREATE TABLE seasons (
    season_id     INTEGER PRIMARY KEY,   -- e.g. 90
    label         TEXT,                  -- e.g. '2025-26'
    start_year    INTEGER,               -- e.g. 2025
    game_type     TEXT CHECK (game_type IN ('regular','playoff'))
);

CREATE TABLE teams (
    team_id       INTEGER PRIMARY KEY,   -- HockeyTech team id, e.g. 440
    name          TEXT,                  -- 'Abbotsford Canucks'  (last seen)
    city          TEXT,                  -- 'Abbotsford'
    nickname      TEXT,                  -- 'Canucks'
    abbreviation  TEXT,                  -- 'ABB'
    division      TEXT                   -- 'Pacific Division' (last seen)
);

CREATE TABLE players (
    player_id     INTEGER PRIMARY KEY,   -- HockeyTech person id, e.g. 9674
    first_name    TEXT,
    last_name     TEXT,
    birth_date    TEXT                   -- ISO 'YYYY-MM-DD' or NULL
);

-- ----------------------------------------------------------------------------
--  CORE FACT: one row per game
-- ----------------------------------------------------------------------------
CREATE TABLE games (
    game_id          INTEGER PRIMARY KEY,           -- details.id, e.g. 1028586
    season_id        INTEGER REFERENCES seasons(season_id),
    game_number      INTEGER,                        -- details.gameNumber
    game_date        TEXT,                           -- details.GameDateISO8601 (full tz)
    game_date_local  TEXT,                           -- date(game_date), for grouping
    venue            TEXT,
    attendance       INTEGER,
    status           TEXT,                           -- 'Final', 'Final OT', 'Final SO'
    is_final         INTEGER,                        -- details.final 0/1
    has_shootout     INTEGER,                        -- 0/1
    went_overtime    INTEGER,                        -- derived from status / periods
    duration         TEXT,                           -- 'H:MM'
    home_team_id     INTEGER REFERENCES teams(team_id),
    visitor_team_id  INTEGER REFERENCES teams(team_id),
    home_score       INTEGER,                        -- homeTeam.stats.goals
    visitor_score    INTEGER,                        -- visitingTeam.stats.goals
    htv_game_id      INTEGER,                        -- details.htvGameId
    game_report_url  TEXT
);
CREATE INDEX idx_games_season       ON games(season_id);
CREATE INDEX idx_games_date         ON games(game_date_local);
CREATE INDEX idx_games_home         ON games(home_team_id);
CREATE INDEX idx_games_visitor      ON games(visitor_team_id);

-- ----------------------------------------------------------------------------
--  TEAM-LEVEL game stats (homeTeam.stats / visitingTeam.stats + record snapshot)
-- ----------------------------------------------------------------------------
CREATE TABLE team_game_stats (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    team_id          INTEGER REFERENCES teams(team_id),
    is_home          INTEGER,                        -- 1 home, 0 visitor
    goals            INTEGER,
    shots            INTEGER,
    pp_goals         INTEGER,                        -- powerPlayGoals
    pp_opportunities INTEGER,                        -- powerPlayOpportunities
    pim              INTEGER,                        -- penaltyMinuteCount
    infractions      INTEGER,                        -- infractionCount
    hits             INTEGER,
    faceoff_wins     INTEGER,
    faceoff_attempts INTEGER,
    -- record as it stood for this team going into / after the game:
    record_wins      INTEGER,
    record_losses    INTEGER,
    record_ot_losses INTEGER,
    record_so_losses INTEGER,
    record_formatted TEXT,                           -- '19-22-6-3'
    PRIMARY KEY (game_id, team_id)
);

-- ----------------------------------------------------------------------------
--  PER-PERIOD line score (periods[].stats)
-- ----------------------------------------------------------------------------
CREATE TABLE period_scores (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    period_id        INTEGER,                        -- 1,2,3,4(OT)...
    period_name      TEXT,                           -- '1st','OT','SO'
    home_goals       INTEGER,
    home_shots       INTEGER,
    visitor_goals    INTEGER,
    visitor_shots    INTEGER,
    PRIMARY KEY (game_id, period_id)
);

-- ----------------------------------------------------------------------------
--  SKATER box scores (homeTeam.skaters[] / visitingTeam.skaters[])
-- ----------------------------------------------------------------------------
CREATE TABLE skater_games (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    player_id        INTEGER REFERENCES players(player_id),
    team_id          INTEGER REFERENCES teams(team_id),
    is_home          INTEGER,
    jersey_number    INTEGER,
    position         TEXT,                           -- C/LW/RW/D
    starting         INTEGER,                        -- 0/1
    goals            INTEGER,
    assists          INTEGER,
    points           INTEGER,
    plus_minus       INTEGER,
    pim              INTEGER,
    shots            INTEGER,
    hits             INTEGER,
    blocked_shots    INTEGER,
    faceoff_wins     INTEGER,
    faceoff_attempts INTEGER,
    toi              TEXT,                            -- 'MM:SS' (often '0:00' pre-tracking)
    toi_seconds      INTEGER,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX idx_skater_player ON skater_games(player_id);
CREATE INDEX idx_skater_team   ON skater_games(team_id);

-- ----------------------------------------------------------------------------
--  GOALIE box scores (homeTeam.goalies[] / visitingTeam.goalies[]),
--  with the W/L/OTL/SOL decision merged in from goalieLog[].
-- ----------------------------------------------------------------------------
CREATE TABLE goalie_games (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    player_id        INTEGER REFERENCES players(player_id),
    team_id          INTEGER REFERENCES teams(team_id),
    is_home          INTEGER,
    jersey_number    INTEGER,
    starting         INTEGER,
    decision         TEXT,                            -- 'W','L','OTL','SOL', or NULL
    goals_against    INTEGER,
    shots_against    INTEGER,
    saves            INTEGER,
    toi              TEXT,                            -- 'MM:SS'
    toi_seconds      INTEGER,
    goals            INTEGER,                         -- goalies can score/assist
    assists          INTEGER,
    pim              INTEGER,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX idx_goalie_player ON goalie_games(player_id);

-- ----------------------------------------------------------------------------
--  GOALS (periods[].goals[])
-- ----------------------------------------------------------------------------
CREATE TABLE goals (
    goal_id          INTEGER PRIMARY KEY,             -- game_goal_id
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    team_id          INTEGER REFERENCES teams(team_id),
    period_id        INTEGER,
    time             TEXT,                            -- 'MM:SS' into the period
    time_seconds     INTEGER,
    scorer_id        INTEGER REFERENCES players(player_id),
    scorer_goal_num  INTEGER,                         -- scorer's season goal #
    is_power_play    INTEGER,
    is_short_handed  INTEGER,
    is_empty_net     INTEGER,
    is_penalty_shot  INTEGER,
    is_insurance     INTEGER,
    is_game_winning  INTEGER
);
CREATE INDEX idx_goals_game   ON goals(game_id);
CREATE INDEX idx_goals_scorer ON goals(scorer_id);

-- Assists on a goal (0,1,2 per goal). assist_order: 1 = primary, 2 = secondary.
CREATE TABLE goal_assists (
    goal_id          INTEGER REFERENCES goals(goal_id) ON DELETE CASCADE,
    player_id        INTEGER REFERENCES players(player_id),
    assist_order     INTEGER,
    PRIMARY KEY (goal_id, player_id)
);
CREATE INDEX idx_goal_assists_player ON goal_assists(player_id);

-- On-ice (+/-) skaters for each goal — powers on-ice / WOWY analytics.
-- on_ice = 'plus' (benefited) or 'minus' (was scored on).
CREATE TABLE goal_on_ice (
    goal_id          INTEGER REFERENCES goals(goal_id) ON DELETE CASCADE,
    player_id        INTEGER REFERENCES players(player_id),
    on_ice           TEXT CHECK (on_ice IN ('plus','minus')),
    PRIMARY KEY (goal_id, player_id, on_ice)
);
CREATE INDEX idx_goal_on_ice_player ON goal_on_ice(player_id);

-- ----------------------------------------------------------------------------
--  PENALTIES (periods[].penalties[])
-- ----------------------------------------------------------------------------
CREATE TABLE penalties (
    penalty_id       INTEGER PRIMARY KEY,             -- game_penalty_id
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    period_id        INTEGER,
    time             TEXT,
    time_seconds     INTEGER,
    against_team_id  INTEGER REFERENCES teams(team_id),
    taken_by_id      INTEGER REFERENCES players(player_id),
    served_by_id     INTEGER REFERENCES players(player_id),
    description      TEXT,                            -- 'High-sticking'
    minutes          INTEGER,
    is_bench         INTEGER,
    is_power_play    INTEGER,                         -- gave opponent a PP
    rule_number      TEXT
);
CREATE INDEX idx_penalties_game   ON penalties(game_id);
CREATE INDEX idx_penalties_player ON penalties(taken_by_id);

-- ----------------------------------------------------------------------------
--  SHOOTOUT attempts (shootoutDetails.{home,visiting}TeamShots[])
-- ----------------------------------------------------------------------------
CREATE TABLE shootout_shots (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    shot_order       INTEGER,
    is_home          INTEGER,
    shooter_id       INTEGER REFERENCES players(player_id),
    goalie_id        INTEGER REFERENCES players(player_id),
    team_id          INTEGER REFERENCES teams(team_id),
    is_goal          INTEGER,
    is_game_winning  INTEGER,
    PRIMARY KEY (game_id, is_home, shot_order)
);

-- ----------------------------------------------------------------------------
--  GAME STARS / MVP  (mostValuablePlayers[])
-- ----------------------------------------------------------------------------
CREATE TABLE game_stars (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    star_rank        INTEGER,                         -- 1 = first star
    player_id        INTEGER REFERENCES players(player_id),
    team_id          INTEGER REFERENCES teams(team_id),
    is_goalie        INTEGER,
    PRIMARY KEY (game_id, star_rank)
);

-- ----------------------------------------------------------------------------
--  COACHES & OFFICIALS (lightweight; officials have no stable id)
-- ----------------------------------------------------------------------------
CREATE TABLE game_coaches (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    team_id          INTEGER REFERENCES teams(team_id),
    person_id        INTEGER,
    first_name       TEXT,
    last_name        TEXT,
    role             TEXT                             -- 'Head Coach', ...
);
CREATE INDEX idx_coaches_game ON game_coaches(game_id);

CREATE TABLE game_officials (
    game_id          INTEGER REFERENCES games(game_id) ON DELETE CASCADE,
    role             TEXT,                            -- 'Referee','Linesperson'
    first_name       TEXT,
    last_name        TEXT,
    jersey_number    INTEGER
);
CREATE INDEX idx_officials_game ON game_officials(game_id);

-- ============================================================================
--  CONVENIENCE VIEWS
-- ============================================================================

-- Player full name + age helper.
CREATE VIEW v_players AS
SELECT player_id,
       TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')) AS full_name,
       birth_date
FROM players;

-- Skater game lines enriched with names, team, opponent, season, date.
CREATE VIEW v_skater_lines AS
SELECT sg.game_id, g.season_id, s.label AS season, g.game_date_local AS date,
       sg.player_id, p.first_name, p.last_name,
       sg.team_id, t.abbreviation AS team,
       CASE WHEN sg.is_home=1 THEN g.visitor_team_id ELSE g.home_team_id END AS opp_team_id,
       sg.position, sg.goals, sg.assists, sg.points, sg.plus_minus,
       sg.shots, sg.pim, sg.hits, sg.blocked_shots, sg.toi_seconds
FROM skater_games sg
JOIN games   g ON g.game_id = sg.game_id
JOIN seasons s ON s.season_id = g.season_id
JOIN players p ON p.player_id = sg.player_id
JOIN teams   t ON t.team_id   = sg.team_id;

-- Career/season skater totals roll-up.
CREATE VIEW v_skater_season_totals AS
SELECT sg.player_id, g.season_id, s.label AS season,
       COUNT(*)                AS gp,
       SUM(sg.goals)           AS g,
       SUM(sg.assists)         AS a,
       SUM(sg.points)          AS pts,
       SUM(sg.plus_minus)      AS plus_minus,
       SUM(sg.pim)             AS pim,
       SUM(sg.shots)           AS shots
FROM skater_games sg
JOIN games   g ON g.game_id = sg.game_id
JOIN seasons s ON s.season_id = g.season_id
GROUP BY sg.player_id, g.season_id;

-- Goalie season totals with sv% and GAA-ready inputs.
CREATE VIEW v_goalie_season_totals AS
SELECT gg.player_id, g.season_id, s.label AS season,
       COUNT(*)                                          AS gp,
       SUM(CASE WHEN gg.decision='W'   THEN 1 ELSE 0 END) AS wins,
       SUM(CASE WHEN gg.decision='L'   THEN 1 ELSE 0 END) AS losses,
       SUM(CASE WHEN gg.decision IN ('OTL','SOL') THEN 1 ELSE 0 END) AS ot_losses,
       SUM(gg.shots_against)                             AS sa,
       SUM(gg.saves)                                     AS sv,
       SUM(gg.goals_against)                             AS ga,
       SUM(gg.toi_seconds)                               AS toi_seconds,
       ROUND(1.0*SUM(gg.saves)/NULLIF(SUM(gg.shots_against),0), 4) AS sv_pct
FROM goalie_games gg
JOIN games   g ON g.game_id = gg.game_id
JOIN seasons s ON s.season_id = g.season_id
GROUP BY gg.player_id, g.season_id;
