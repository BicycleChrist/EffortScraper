#!/usr/bin/env python3
"""
Load the AHL game JSON corpus (ahl_data/game_*.json) into a SQLite database
defined by schema.sql.

Usage:
    python load_db.py                      # load everything into ahl.db
    python load_db.py --db ahl.db          # choose output file
    python load_db.py --limit 200          # quick sample load (testing)
    python load_db.py --data-dir ahl_data  # source dir

Idempotent: each game's rows are deleted (cascade) before re-insert, so the
loader can be re-run over new/updated game files safely.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Season feed ids -> regular vs playoff (from ahl_scraper.py).
REGULAR_SEASON_IDS = {1, 8, 12, 16, 30, 34, 37, 40, 43, 46, 48, 51, 54, 57, 61,
                      65, 68, 73, 77, 81, 86, 90}
PLAYOFF_SEASON_IDS = {7, 10, 15, 29, 33, 36, 39, 42, 47, 50, 53, 56, 60, 64, 69,
                      72, 76, 80, 84, 88, 92}


def to_int(v):
    """Coerce '12' / 12 / '' / None -> int or None."""
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_bool(v):
    """'0'/'1', 0/1, True/False -> 0/1 (None stays None)."""
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        return 1 if int(v) else 0
    except (TypeError, ValueError):
        return None


def mmss_to_seconds(s):
    """'64:57' -> 3897 ; '0:52' -> 52 ; 'H:MM:SS' supported ; '' -> None."""
    if not s or not isinstance(s, str) or ':' not in s:
        return None
    try:
        parts = [int(p) for p in s.split(':')]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def season_start_year(iso_date):
    """Derive AHL season start year from an ISO game date.
    Sep-Dec -> that year; Jan-Aug -> previous year (handles playoff feeds)."""
    if not iso_date or len(iso_date) < 7:
        return None
    try:
        year = int(iso_date[0:4])
        month = int(iso_date[5:7])
    except ValueError:
        return None
    return year if month >= 9 else year - 1


class Loader:
    def __init__(self, conn):
        self.conn = conn
        self.teams_seen = {}     # team_id -> attrs (last write wins)
        self.players_seen = {}   # player_id -> (first,last,birth)
        self.season_year = {}    # season_id -> min start_year observed

    # --- dimension helpers (accumulated in memory, flushed at the end) ----
    def note_team(self, t):
        if not t or t.get('id') is None:
            return None
        tid = t['id']
        self.teams_seen[tid] = (t.get('name'), t.get('city'), t.get('nickname'),
                                t.get('abbreviation'), t.get('divisionName'))
        return tid

    def note_player(self, info):
        if not info or info.get('id') in (None, 0):
            return None
        pid = info['id']
        bd = info.get('birthDate') or None
        prev = self.players_seen.get(pid)
        # keep a birth date if we ever see one
        if prev and not bd:
            bd = prev[2]
        self.players_seen[pid] = (info.get('firstName'), info.get('lastName'), bd)
        return pid

    # --- main per-game loader ---------------------------------------------
    def load_game(self, data):
        d = data.get('details', {})
        gid = d.get('id')
        if gid is None:
            return False
        c = self.conn

        # wipe any prior rows for this game (cascade clears children)
        c.execute("DELETE FROM games WHERE game_id=?", (gid,))

        home = data.get('homeTeam', {}) or {}
        away = data.get('visitingTeam', {}) or {}
        home_id = self.note_team(home.get('info'))
        away_id = self.note_team(away.get('info'))

        season_id = to_int(d.get('seasonId'))
        iso = d.get('GameDateISO8601')
        sy = season_start_year(iso)
        if season_id is not None and sy is not None:
            self.season_year[season_id] = min(self.season_year.get(season_id, sy), sy)

        status = d.get('status') or ''
        home_stats = home.get('stats', {}) or {}
        away_stats = away.get('stats', {}) or {}
        home_score = to_int(home_stats.get('goals', home_stats.get('goalCount')))
        away_score = to_int(away_stats.get('goals', away_stats.get('goalCount')))

        c.execute("""INSERT INTO games
            (game_id, season_id, game_number, game_date, game_date_local, venue,
             attendance, status, is_final, has_shootout, went_overtime, duration,
             home_team_id, visitor_team_id, home_score, visitor_score,
             htv_game_id, game_report_url)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gid, season_id, to_int(d.get('gameNumber')), iso,
             iso[:10] if iso else None, d.get('venue'), to_int(d.get('attendance')),
             status, to_bool(d.get('final')), to_bool(data.get('hasShootout')),
             1 if ('OT' in status or 'SO' in status) else 0, d.get('duration'),
             home_id, away_id, home_score, away_score,
             to_int(d.get('htvGameId')), d.get('gameReportUrl')))

        self._load_team_stats(gid, home_id, 1, home)
        self._load_team_stats(gid, away_id, 0, away)
        self._load_period_scores(gid, data.get('periods', []))
        self._load_skaters(gid, home_id, 1, home.get('skaters', []))
        self._load_skaters(gid, away_id, 0, away.get('skaters', []))
        self._load_goalies(gid, home_id, 1, home)
        self._load_goalies(gid, away_id, 0, away)
        self._load_goals_penalties(gid, data.get('periods', []))
        self._load_shootout(gid, data.get('shootoutDetails'))
        self._load_stars(gid, data.get('mostValuablePlayers', []))
        self._load_coaches(gid, home_id, home.get('coaches', []))
        self._load_coaches(gid, away_id, away.get('coaches', []))
        self._load_officials(gid, data.get('referees', []), data.get('linesmen', []))
        return True

    def _load_team_stats(self, gid, tid, is_home, team):
        s = team.get('stats', {}) or {}
        rec = (team.get('seasonStats', {}) or {}).get('teamRecord', {}) or {}
        self.conn.execute("""INSERT INTO team_game_stats
            (game_id, team_id, is_home, goals, shots, pp_goals, pp_opportunities,
             pim, infractions, hits, faceoff_wins, faceoff_attempts,
             record_wins, record_losses, record_ot_losses, record_so_losses,
             record_formatted)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gid, tid, is_home, to_int(s.get('goals', s.get('goalCount'))),
             to_int(s.get('shots')), to_int(s.get('powerPlayGoals')),
             to_int(s.get('powerPlayOpportunities')),
             to_int(s.get('penaltyMinuteCount')), to_int(s.get('infractionCount')),
             to_int(s.get('hits')), to_int(s.get('faceoffWins')),
             to_int(s.get('faceoffAttempts')),
             to_int(rec.get('wins')), to_int(rec.get('losses')),
             to_int(rec.get('OTLosses')), to_int(rec.get('SOLosses')),
             rec.get('formattedRecord')))

    def _load_period_scores(self, gid, periods):
        for per in periods or []:
            info = per.get('info', {}) or {}
            st = per.get('stats', {}) or {}
            self.conn.execute("""INSERT OR IGNORE INTO period_scores
                (game_id, period_id, period_name, home_goals, home_shots,
                 visitor_goals, visitor_shots)
                VALUES (?,?,?,?,?,?,?)""",
                (gid, to_int(info.get('id')), info.get('longName'),
                 to_int(st.get('homeGoals')), to_int(st.get('homeShots')),
                 to_int(st.get('visitingGoals')), to_int(st.get('visitingShots'))))

    def _load_skaters(self, gid, tid, is_home, skaters):
        for sk in skaters or []:
            info = sk.get('info', {}) or {}
            pid = self.note_player(info)
            if pid is None:
                continue
            s = sk.get('stats', {}) or {}
            self.conn.execute("""INSERT OR REPLACE INTO skater_games
                (game_id, player_id, team_id, is_home, jersey_number, position,
                 starting, goals, assists, points, plus_minus, pim, shots, hits,
                 blocked_shots, faceoff_wins, faceoff_attempts, toi, toi_seconds)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, pid, tid, is_home, to_int(info.get('jerseyNumber')),
                 info.get('position'), to_int(sk.get('starting')),
                 to_int(s.get('goals')), to_int(s.get('assists')),
                 to_int(s.get('points')), to_int(s.get('plusMinus')),
                 to_int(s.get('penaltyMinutes')), to_int(s.get('shots')),
                 to_int(s.get('hits')), to_int(s.get('blockedShots')),
                 to_int(s.get('faceoffWins')), to_int(s.get('faceoffAttempts')),
                 s.get('toi'), mmss_to_seconds(s.get('toi'))))

    def _load_goalies(self, gid, tid, is_home, team):
        # map player_id -> decision (W/L/OTL/SOL) from goalieLog
        decisions = {}
        for gl in team.get('goalieLog', []) or []:
            info = gl.get('info', {}) or {}
            if info.get('id') is not None:
                decisions[info['id']] = gl.get('result')
        for go in team.get('goalies', []) or []:
            info = go.get('info', {}) or {}
            pid = self.note_player(info)
            if pid is None:
                continue
            s = go.get('stats', {}) or {}
            self.conn.execute("""INSERT OR REPLACE INTO goalie_games
                (game_id, player_id, team_id, is_home, jersey_number, starting,
                 decision, goals_against, shots_against, saves, toi, toi_seconds,
                 goals, assists, pim)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, pid, tid, is_home, to_int(info.get('jerseyNumber')),
                 to_int(go.get('starting')), decisions.get(pid),
                 to_int(s.get('goalsAgainst')), to_int(s.get('shotsAgainst')),
                 to_int(s.get('saves')), s.get('timeOnIce'),
                 mmss_to_seconds(s.get('timeOnIce')),
                 to_int(s.get('goals')), to_int(s.get('assists')),
                 to_int(s.get('penaltyMinutes'))))

    def _load_goals_penalties(self, gid, periods):
        for per in periods or []:
            for goal in per.get('goals', []) or []:
                goal_id = to_int(goal.get('game_goal_id'))
                if goal_id is None:
                    continue
                team_id = self.note_team(goal.get('team'))
                scorer = goal.get('scoredBy', {}) or {}
                scorer_id = self.note_player(scorer)
                props = goal.get('properties', {}) or {}
                pinfo = goal.get('period', {}) or {}
                self.conn.execute("""INSERT OR REPLACE INTO goals
                    (goal_id, game_id, team_id, period_id, time, time_seconds,
                     scorer_id, scorer_goal_num, is_power_play, is_short_handed,
                     is_empty_net, is_penalty_shot, is_insurance, is_game_winning)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (goal_id, gid, team_id, to_int(pinfo.get('id')),
                     goal.get('time'), mmss_to_seconds(goal.get('time')),
                     scorer_id, to_int(goal.get('scorerGoalNumber')),
                     to_bool(props.get('isPowerPlay')),
                     to_bool(props.get('isShortHanded')),
                     to_bool(props.get('isEmptyNet')),
                     to_bool(props.get('isPenaltyShot')),
                     to_bool(props.get('isInsuranceGoal')),
                     to_bool(props.get('isGameWinningGoal'))))
                for i, a in enumerate(goal.get('assists', []) or [], start=1):
                    apid = self.note_player(a)
                    if apid is not None:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO goal_assists VALUES (?,?,?)",
                            (goal_id, apid, i))
                for side, key in (('plus', 'plus_players'), ('minus', 'minus_players')):
                    for pl in goal.get(key, []) or []:
                        opid = self.note_player(pl)
                        if opid is not None:
                            self.conn.execute(
                                "INSERT OR IGNORE INTO goal_on_ice VALUES (?,?,?)",
                                (goal_id, opid, side))
            for pen in per.get('penalties', []) or []:
                pen_id = to_int(pen.get('game_penalty_id'))
                if pen_id is None:
                    continue
                against = self.note_team(pen.get('againstTeam'))
                taken = self.note_player(pen.get('takenBy'))
                served = self.note_player(pen.get('servedBy'))
                pinfo = pen.get('period', {}) or {}
                self.conn.execute("""INSERT OR REPLACE INTO penalties
                    (penalty_id, game_id, period_id, time, time_seconds,
                     against_team_id, taken_by_id, served_by_id, description,
                     minutes, is_bench, is_power_play, rule_number)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pen_id, gid, to_int(pinfo.get('id')), pen.get('time'),
                     mmss_to_seconds(pen.get('time')), against, taken, served,
                     pen.get('description'), to_int(pen.get('minutes')),
                     to_bool(pen.get('isBench')), to_bool(pen.get('isPowerPlay')),
                     pen.get('ruleNumber') or None))

    def _load_shootout(self, gid, so):
        if not so:
            return
        for is_home, key in ((1, 'homeTeamShots'), (0, 'visitingTeamShots')):
            for i, shot in enumerate(so.get(key, []) or [], start=1):
                shooter = self.note_player(shot.get('shooter'))
                goalie = self.note_player(shot.get('goalie'))
                team_id = self.note_team(shot.get('shooterTeam'))
                self.conn.execute("""INSERT OR REPLACE INTO shootout_shots
                    (game_id, shot_order, is_home, shooter_id, goalie_id, team_id,
                     is_goal, is_game_winning)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (gid, i, is_home, shooter, goalie, team_id,
                     to_bool(shot.get('isGoal')), to_bool(shot.get('isGameWinningGoal'))))

    def _load_stars(self, gid, mvps):
        for rank, mvp in enumerate(mvps or [], start=1):
            player = (mvp.get('player', {}) or {}).get('info', {}) or {}
            pid = self.note_player(player)
            tid = self.note_team(mvp.get('team'))
            self.conn.execute("""INSERT OR REPLACE INTO game_stars
                (game_id, star_rank, player_id, team_id, is_goalie)
                VALUES (?,?,?,?,?)""",
                (gid, rank, pid, tid, to_bool(mvp.get('isGoalie'))))

    def _load_coaches(self, gid, tid, coaches):
        for co in coaches or []:
            self.conn.execute("""INSERT INTO game_coaches
                (game_id, team_id, person_id, first_name, last_name, role)
                VALUES (?,?,?,?,?,?)""",
                (gid, tid, to_int(co.get('personId')), co.get('firstName'),
                 co.get('lastName'), co.get('role')))

    def _load_officials(self, gid, referees, linesmen):
        for off in (referees or []) + (linesmen or []):
            self.conn.execute("""INSERT INTO game_officials
                (game_id, role, first_name, last_name, jersey_number)
                VALUES (?,?,?,?,?)""",
                (gid, off.get('role'), off.get('firstName'), off.get('lastName'),
                 to_int(off.get('jerseyNumber'))))

    # --- flush dimensions --------------------------------------------------
    def flush_dimensions(self):
        c = self.conn
        c.executemany(
            """INSERT INTO teams (team_id,name,city,nickname,abbreviation,division)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(team_id) DO UPDATE SET
                 name=excluded.name, city=excluded.city, nickname=excluded.nickname,
                 abbreviation=excluded.abbreviation, division=excluded.division""",
            [(tid, *attrs) for tid, attrs in self.teams_seen.items()])
        c.executemany(
            """INSERT INTO players (player_id,first_name,last_name,birth_date)
               VALUES (?,?,?,?)
               ON CONFLICT(player_id) DO UPDATE SET
                 first_name=excluded.first_name, last_name=excluded.last_name,
                 birth_date=COALESCE(excluded.birth_date, players.birth_date)""",
            [(pid, f, l, b) for pid, (f, l, b) in self.players_seen.items()])
        rows = []
        for sid, sy in self.season_year.items():
            label = f"{sy}-{str(sy + 1)[2:]}" if sy is not None else None
            gtype = 'playoff' if sid in PLAYOFF_SEASON_IDS else 'regular'
            rows.append((sid, label, sy, gtype))
        c.executemany(
            """INSERT INTO seasons (season_id,label,start_year,game_type)
               VALUES (?,?,?,?)
               ON CONFLICT(season_id) DO UPDATE SET
                 label=excluded.label, start_year=excluded.start_year,
                 game_type=excluded.game_type""", rows)


def main():
    ap = argparse.ArgumentParser(description="Load AHL game JSON into SQLite")
    ap.add_argument('--db', default='ahl.db')
    ap.add_argument('--data-dir', default='ahl_data')
    ap.add_argument('--schema', default='schema.sql')
    ap.add_argument('--limit', type=int, default=None, help='only load N games (testing)')
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    files = sorted(data_dir.glob('game_*.json'))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"No game_*.json files in {data_dir}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(Path(args.schema).read_text())
    # Dimensions are flushed at the end, so fact rows reference not-yet-inserted
    # ids during the load. Disable FK enforcement for speed/ordering, then
    # verify integrity with foreign_key_check once everything is in.
    conn.execute("PRAGMA foreign_keys=OFF")

    loader = Loader(conn)
    ok = bad = 0
    for i, f in enumerate(files, 1):
        try:
            data = json.loads(f.read_text())
            if loader.load_game(data):
                ok += 1
            else:
                bad += 1
        except Exception as e:
            bad += 1
            print(f"  [skip] {f.name}: {e}", file=sys.stderr)
        if i % 1000 == 0:
            conn.commit()
            print(f"  ... {i}/{len(files)} games", flush=True)

    loader.flush_dimensions()
    conn.commit()

    # summary
    print(f"\nLoaded {ok} games ({bad} skipped) into {args.db}")
    for tbl in ('seasons', 'teams', 'players', 'games', 'skater_games',
                'goalie_games', 'goals', 'goal_assists', 'goal_on_ice',
                'penalties', 'shootout_shots', 'game_stars'):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:16} {n:>9,}")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        print(f"\n  WARNING: {len(violations)} foreign-key violations "
              f"(e.g. {violations[:3]})")
    else:
        print("\n  foreign-key check: OK (no orphaned references)")
    conn.close()


if __name__ == '__main__':
    main()
