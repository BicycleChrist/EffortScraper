"""
pregame_query — everything the sim needs to know about TONIGHT before pricing a game
(merged 2026-10-01 from lineup_feed.py + pregame.py; supersedes the old sweatygoaliestatus.py).

1. Lineups + starting goalies (Daily Faceoff, embedded JSON), resolved to NHL player ids:
       team_lines(abbrev), starting_goalies(date), game_lineups(date, away, home)
   Name -> id: the team's CURRENT NHL API roster first (same-name players can't collide within a team, new
   players resolve before our DB has them), then the global players table (accent-insensitive).
2. Pre-game context for model_test manual mode:
       build_context(db, date, away, home, allow_stale=False)
   - freshness: each team's completed games (NHL API) vs the DB AND MoneyPuck coverage -> refuses if stale
   - who played last game (NHL boxscore) vs tonight's projected lineup (in/out, ratings, expected TOI)
   - roster ratings for tonight's 18 (same rule as model_test.process_roster_rapm, current-season ratings)
   - team-form continuity: share of tonight's ice time that played the games behind the rolling form inputs
   - rest from the real last game date; feed goalie + Confirmed/Likely status; warnings
Every fetch is snapshotted with its fetch time to lineup_snapshots/<date>/ (bet-time record for backtests).

CLI:  python pregame_query.py DATE AWAY HOME            pre-game context report (allow stale)
      python pregame_query.py --lineups DATE AWAY HOME  projected lineups/goalies only
"""
import datetime as _dt
import json
import os
import re
import sqlite3
import time
import unicodedata

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'nhl_analytics.db')
SNAP_DIR = os.path.join(HERE, 'lineup_snapshots')
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'}
DF = 'https://www.dailyfaceoff.com'
NHL = 'https://api-web.nhle.com/v1'

_session = requests.Session()
_session.headers.update(UA)


def _norm(name):
    n = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z]', '', n.lower())


def _get(url, tries=3):
    for i in range(tries):
        try:
            r = _session.get(url, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                time.sleep(3 * (i + 1))
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}")


def _next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ on page (layout changed?)")
    return json.loads(m.group(1))['props']['pageProps']


def _snapshot(date, kind, payload):
    d = os.path.join(SNAP_DIR, date)
    os.makedirs(d, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc)
    path = os.path.join(d, f"{stamp:%H%M%S}_{kind}.json")
    with open(path, 'w') as f:
        json.dump({'fetched_at_utc': stamp.isoformat(), 'kind': kind, 'data': payload}, f, indent=1)
    return path


# ---------------------------------------------------------------- team mapping (NHL abbrev <-> DF slug)
_TEAMS = None


def _teams():
    """{NHL abbrev: {'name', 'df_slug'}} from NHL standings names + Daily Faceoff team list."""
    global _TEAMS
    if _TEAMS is None:
        st = _get(f"{NHL}/standings/now").json()['standings']
        nhl = {t['teamAbbrev']['default']: t['teamName']['default'] for t in st}
        pp = _next_data(_get(f"{DF}/teams/boston-bruins/line-combinations").text)
        df = {_norm(t['name']): t['slug'] for t in pp['sortedTeams']}
        _TEAMS = {}
        for ab, nm in nhl.items():
            slug = df.get(_norm(nm))
            if slug is None:   # e.g. "Montréal" vs "Montreal" handled by _norm; anything else is a real gap
                raise RuntimeError(f"no Daily Faceoff slug for {ab} {nm}")
            _TEAMS[ab] = {'name': nm, 'df_slug': slug}
    return _TEAMS


# ---------------------------------------------------------------- name -> NHL id
_GLOBAL = None


def _global_ids():
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = {}
        con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        for pid, name, pos in con.execute("SELECT player_id, player_name, position FROM players"):
            pid = str(pid).replace(' [G]', '').strip()
            if pid.isdigit():
                _GLOBAL.setdefault(_norm(name), set()).add((pid, (pos or '?')[:1]))
        con.close()
    return _GLOBAL


def _roster_ids(abbrev):
    r = _get(f"{NHL}/roster/{abbrev}/current").json()
    out = {}
    for grp, pos in (('forwards', 'F'), ('defensemen', 'D'), ('goalies', 'G')):
        for p in r.get(grp, []):
            nm = f"{p['firstName']['default']} {p['lastName']['default']}"
            out.setdefault(_norm(nm), []).append((str(p['id']), pos))
    return out


def _resolve(name, pos_hint, roster):
    k = _norm(name)
    cands = roster.get(k, [])
    if len(cands) == 1:
        return cands[0][0]
    if len(cands) > 1:   # same name twice on one roster: use position
        m = [c for c in cands if c[1] == pos_hint]
        if len(m) == 1:
            return m[0][0]
    g = _global_ids().get(k, set())
    if len(g) == 1:
        return next(iter(g))[0]
    if len(g) > 1:
        m = [c for c in g if (c[1] == 'D') == (pos_hint == 'D')]
        if len(m) == 1:
            return m[0][0]
    return None


# ---------------------------------------------------------------- public API
def starting_goalies(date):
    """[{away, home, away_goalie, away_status, home_goalie, home_status, start_utc}] for a date (YYYY-MM-DD)."""
    pp = _next_data(_get(f"{DF}/starting-goalies/{date}").text)
    _snapshot(date, 'goalies', pp.get('data', []))
    out = []
    for g in pp.get('data', []):
        out.append({'away_name': g.get('awayTeamName'), 'home_name': g.get('homeTeamName'),
                    'away_goalie': g.get('awayGoalieName'), 'away_status': g.get('awayNewsStrengthName'),
                    'home_goalie': g.get('homeGoalieName'), 'home_status': g.get('homeNewsStrengthName'),
                    'start_utc': g.get('dateGmt')})
    return out


def team_lines(abbrev, date=None):
    """Projected lineup for one team (NHL abbreviation), with NHL ids."""
    date = date or _dt.date.today().isoformat()
    t = _teams()[abbrev]
    combos = _next_data(_get(f"{DF}/teams/{t['df_slug']}/line-combinations").text)['combinations']
    _snapshot(date, f'lines_{abbrev}', combos)
    roster = _roster_ids(abbrev)
    out = {'team': abbrev, 'updated_at': combos.get('updatedAt'), 'skaters': [], 'goalies': [],
           'pp1': [], 'pp2': [], 'pk1': [], 'pk2': [], 'injured': [], 'unresolved': []}
    for p in combos.get('players', []):
        grp, cat = p.get('groupName') or '', p.get('categoryName') or ''
        posid = (p.get('positionIdentifier') or '').lower()
        hint = 'G' if posid.startswith('g') else 'D' if posid in ('ld', 'rd') or 'Defense' in grp else 'F'
        pid = _resolve(p.get('name'), hint, roster)
        rec = {'id': pid, 'name': p.get('name'), 'pos': posid, 'injury': p.get('injuryStatus'),
               'gtd': bool(p.get('gameTimeDecision'))}
        if pid is None:
            out['unresolved'].append(p.get('name'))
        if cat == 'Even Strength' and grp.startswith('Forwards'):
            out['skaters'].append({**rec, 'slot': 'F' + grp.split()[-1]})
        elif cat == 'Even Strength' and grp.startswith('Defense'):
            out['skaters'].append({**rec, 'slot': 'D' + grp.split()[-1]})
        elif grp == 'Goalies':
            out['goalies'].append({**rec, 'slot': posid})
        elif cat == 'Power Play':
            out['pp1' if grp.startswith('1st') else 'pp2'].append(rec)
        elif cat == 'Penalty Kill':
            out['pk1' if grp.startswith('1st') else 'pk2'].append(rec)
        elif 'Injured' in grp or cat == 'Off Ice':
            out['injured'].append(rec)
    return out


def game_lineups(date, away, home):
    """Both teams' projected lineups + the day's starting goalie (and its status) for one game."""
    tm = _teams()
    gl = starting_goalies(date)
    game = next((g for g in gl if _norm(g['home_name']) == _norm(tm[home]['name'])
                 and _norm(g['away_name']) == _norm(tm[away]['name'])), None)
    res = {}
    for side, ab in (('away', away), ('home', home)):
        L = team_lines(ab, date)
        roster = _roster_ids(ab)
        if game and game.get(f'{side}_goalie'):
            gid = _resolve(game[f'{side}_goalie'], 'G', roster)
            L['goalie'] = {'id': gid, 'name': game[f'{side}_goalie'], 'status': game.get(f'{side}_status')}
        else:   # no goalie report yet: fall back to the lines page's G1, flagged
            g1 = next((g for g in L['goalies'] if g['slot'] == 'g1'), None)
            L['goalie'] = {'id': g1 and g1['id'], 'name': g1 and g1['name'], 'status': 'Unreported (lines G1)'}
        res[side] = L
    res['start_utc'] = game and game.get('start_utc')
    return res


# ====================================================================================================
# PRE-GAME CONTEXT
# ====================================================================================================

NHL2DB = {'NJD': 'NJ', 'SJS': 'SJ', 'TBL': 'TB', 'LAK': 'LA'}
DB2NHL = {v: k for k, v in NHL2DB.items()}
SLOT_TOI = {'F1': 18.5, 'F2': 16.5, 'F3': 14.0, 'F4': 11.5, 'D1': 22.5, 'D2': 20.0, 'D3': 17.0}   # minutes, all sit.
RAPM_PATH = os.path.join(HERE, 'rapm_by_season.csv')
ROSTER_MIN_TOI = 150.0   # same trust rule as model_test.process_roster_rapm


def _nhl(abbr):
    a = abbr.replace('.', '').upper()
    return DB2NHL.get(a, a)


def _season_of(date):
    d = _dt.date.fromisoformat(date)
    y = d.year if d.month >= 9 else d.year - 1
    return f"{y}-{y + 1}", f"{y}{y + 1}"


def _completed_games(nhl_abbr, season_code, before):
    r = _get(f"{NHL}/club-schedule-season/{nhl_abbr}/{season_code}").json()
    out = []
    for g in r.get('games', []):
        if g.get('gameType') in (2, 3) and g.get('gameState') in ('OFF', 'FINAL') and g['gameDate'] < before:
            out.append((str(g['id']), g['gameDate']))
    return sorted(out, key=lambda t: t[1])


def _boxscore_skaters(game_id, nhl_abbr):
    b = _get(f"{NHL}/gamecenter/{game_id}/boxscore").json()
    side = 'homeTeam' if b['homeTeam']['abbrev'] == nhl_abbr else 'awayTeam'
    ps = b['playerByGameStats'][side]
    sk = [(str(p['playerId']), p['name']['default']) for grp in ('forwards', 'defense') for p in ps[grp]]
    gl = [(str(p['playerId']), p['name']['default'], p.get('toi')) for p in ps['goalies']]
    return sk, gl


def _ratings(season):
    rt = pd.read_csv(RAPM_PATH, dtype={'player_id': str})
    rt['ok'] = rt['toi_5v5_min'] >= ROSTER_MIN_TOI
    use = season if season in set(rt.season) else sorted(rt.season.unique())[-1]
    r = rt[rt.season == use].set_index('player_id')
    q = r[r.ok]
    return r, float(q.d_rapm.mean()), float(q.o_rapm.mean()), use


def _expected_toi(con, pids):
    if not pids:
        return {}
    q = f"""SELECT player_id, mp_ice_time FROM (
              SELECT player_id, mp_ice_time, ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY game_id DESC) rn
              FROM mp_skater_game_stats WHERE situation_id = 2 AND mp_ice_time > 0
                AND player_id IN ({','.join('?' * len(pids))})) WHERE rn <= 20"""
    d = pd.read_sql(q, con, params=list(pids))
    d['player_id'] = d.player_id.astype(str)
    return (d.groupby('player_id').mp_ice_time.mean() / 60.0).to_dict()


FORM_WINDOW = 10            # games behind the 10-game rolling team-form inputs
CONTINUITY_WARN = 0.70


def _form_continuity(con, tid, players):
    """Ice-time overlap between tonight's expected lineup and the skaters who played the team's last
    FORM_WINDOW games in the DB (the games its rolling team-form inputs are computed from)."""
    gids = [r[0] for r in con.execute("""SELECT game_id FROM games WHERE (home_team_id=? OR away_team_id=?) AND game_type IN (2, 3)
                                        ORDER BY game_date DESC, game_id DESC LIMIT ?""", (tid, tid, FORM_WINDOW))]
    if not gids or not players:
        return None, [], []
    lu = pd.read_sql(f"""SELECT player_id, SUM(mp_ice_time) toi FROM mp_skater_game_stats
                         WHERE situation_id=2 AND team_id=? AND game_id IN ({','.join('?' * len(gids))})
                         GROUP BY player_id""", con, params=[tid] + gids)
    lu['player_id'] = lu.player_id.astype(str)
    win = (lu.set_index('player_id').toi / lu.toi.sum())
    tw = pd.Series(dict(players)); tw = tw / tw.sum()
    overlap = float(np.minimum(win.reindex(tw.index).fillna(0), tw).sum())
    not_in_window = [p for p in tw.index if p not in win.index]
    gone = win[~win.index.isin(tw.index)].sort_values(ascending=False)
    return overlap, not_in_window, list(gone.index[:8])


def _roster_rating(players, R, lg_d, lg_o):
    """players: [(pid, toi_minutes)] -> TOI-weighted (d, o) with model_test's trust/fallback rule"""
    d, o, w, n_fallback = [], [], [], 0
    for pid, toi in players:
        ok = pid in R.index and bool(R.ok.get(pid, False))
        n_fallback += not ok
        d.append(R.d_rapm[pid] if ok else lg_d); o.append(R.o_rapm[pid] if ok else lg_o); w.append(toi)
    w = np.asarray(w, float)
    return float(np.average(d, weights=w)), float(np.average(o, weights=w)), n_fallback


def build_context(db, date, away, home, allow_stale=False, verbose=True):
    away_n, home_n = _nhl(away), _nhl(home)
    season, season_code = _season_of(date)
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    teams = dict(con.execute("SELECT team_abbr, team_id FROM teams"))
    R, lg_d, lg_o, rating_season = _ratings(season)
    warnings = []
    if rating_season != season:
        warnings.append(f"no {season} ratings row; using {rating_season}")
    lu = game_lineups(date, away_n, home_n)
    ctx = {'date': date, 'season': season, 'rating_season': rating_season, 'warnings': warnings,
           'fetched_at_utc': _dt.datetime.now(_dt.timezone.utc).isoformat(), 'start_utc': lu.get('start_utc')}
    stale_any = False
    for side, ab in (('away', away_n), ('home', home_n)):
        tid = teams[NHL2DB.get(ab, ab)]
        # ---- freshness: completed games this season (or last season if none yet) vs DB
        done = _completed_games(ab, season_code, date)
        prev_code = f"{int(season_code[:4]) - 1}{season_code[:4]}"
        last_api = done[-1] if done else (_completed_games(ab, prev_code, date) or [(None, None)])[-1]
        in_db = {r[0] for r in con.execute("SELECT game_id FROM games WHERE home_team_id=? OR away_team_id=?", (tid, tid))}
        missing = [g for g, _ in done if g not in in_db]
        # a game can be in `games` (NST import) while MoneyPuck hasn't posted it yet -> team form still stale
        in_mp = {r[0] for r in con.execute(f"""SELECT DISTINCT game_id FROM mp_team_game_stats WHERE team_id=?
                                              AND game_id IN ({','.join('?' * len(done))})""", [tid] + [g for g, _ in done])} if done else set()
        missing += [g for g, _ in done if g in in_db and g not in in_mp]
        db_last = con.execute("""SELECT game_id, game_date FROM games WHERE (home_team_id=? OR away_team_id=?) AND game_type IN (2, 3)
                                 ORDER BY game_date DESC, game_id DESC LIMIT 1""", (tid, tid)).fetchone()
        if missing:
            stale_any = True
            warnings.append(f"{ab}: {len(missing)} completed game(s) missing from DB or MoneyPuck data {missing[:5]} -> team form is STALE")
        # ---- who played last game (actual), vs tonight
        last_sk, last_gl = _boxscore_skaters(last_api[0], ab) if last_api[0] else ([], [])
        L = lu[side]
        tonight = [(s['id'], s['name'], s['slot']) for s in L['skaters'] if s['id']]
        etoi = _expected_toi(con, list({p for p, _, _ in tonight} | {p for p, _ in last_sk}))
        players = [(p, etoi.get(p) or SLOT_TOI.get(slot, 14.0)) for p, _, slot in tonight]
        d, o, nfb = _roster_rating(players, R, lg_d, lg_o)
        cont, not_in_win, gone_ids = _form_continuity(con, tid, players)
        names = dict(con.execute(f"SELECT player_id, player_name FROM players WHERE player_id IN ({','.join('?' * len(gone_ids))})", gone_ids)) if gone_ids else {}
        nm_tonight = {p: n for p, n, _ in tonight}
        if cont is not None and cont < CONTINUITY_WARN:
            warnings.append(f"{ab}: only {cont*100:.0f}% of tonight's ice time played in the {FORM_WINDOW} games behind its "
                            f"team-form inputs -> rolling stats describe a DIFFERENT lineup; treat this price with caution")
        last_ids = {p for p, _ in last_sk}
        tonight_ids = {p for p, _, _ in tonight}
        def card(pid, name):
            ok = pid in R.index and bool(R.ok.get(pid, False))
            return {'id': pid, 'name': name, 'toi': round(etoi.get(pid, 0.0), 1),
                    'd_rapm': round(float(R.d_rapm[pid]), 4) if ok else None,
                    'o_rapm': round(float(R.o_rapm[pid]), 4) if ok else None}
        last_date = last_api[1]
        rest = min(max((_dt.date.fromisoformat(date) - _dt.date.fromisoformat(last_date)).days, 0), 10) if last_date else 10
        if len(tonight) < 18:
            warnings.append(f"{ab}: only {len(tonight)}/18 projected skaters resolved ({L['unresolved']})")
        if L['goalie'].get('status') not in ('Confirmed',):
            warnings.append(f"{ab}: starting goalie {L['goalie'].get('name')} is '{L['goalie'].get('status')}', not Confirmed")
        gtd = [s['name'] for s in L['skaters'] if s.get('gtd')]
        if gtd:
            warnings.append(f"{ab}: game-time decisions: {gtd}")
        ctx[side] = {'team': ab, 'team_id': tid, 'roster_drapm': d, 'roster_orapm': o, 'rating_fallbacks': nfb,
                     'goalie_id': L['goalie'].get('id'), 'goalie_name': L['goalie'].get('name'),
                     'goalie_status': L['goalie'].get('status'), 'rest': rest,
                     'last_game': {'api': last_api[0], 'api_date': last_date, 'db': db_last[0] if db_last else None,
                                   'db_date': db_last[1] if db_last else None, 'goalies': last_gl},
                     'missing_from_db': missing, 'lines_updated_at': L.get('updated_at'),
                     'form_continuity': cont,
                     'not_in_form_window': [nm_tonight.get(p, p) for p in not_in_win],
                     'gone_from_form_window': [names.get(p, p) for p in gone_ids],
                     'lineup': [{**card(p, n), 'slot': s} for p, n, s in tonight],
                     'in': [card(p, n) for p, n, _ in tonight if p not in last_ids],
                     'out': [card(p, n) for p, n in last_sk if p not in tonight_ids],
                     'injured': [i['name'] for i in L['injured']]}
    con.close()
    ctx['stale'] = stale_any
    snap = os.path.join(SNAP_DIR, date)
    os.makedirs(snap, exist_ok=True)
    path = os.path.join(snap, f"pregame_{away_n}_at_{home_n}_{_dt.datetime.now(_dt.timezone.utc):%H%M%S}.json")
    with open(path, 'w') as f:
        json.dump(ctx, f, indent=1, default=str)
    ctx['snapshot'] = path
    if verbose:
        report(ctx)
    if stale_any and not allow_stale:
        print("\n✗ DB is missing completed games for this matchup — the team-form inputs would describe an older team.")
        print("  Update first:  python nhl_update_orchestrator.py --lookback <days>   (or re-run with --allow-stale)")
        return None
    return ctx


def report(ctx):
    print("\n" + "=" * 78)
    print(f"PRE-GAME CONTEXT  {ctx['away']['team']} @ {ctx['home']['team']}  {ctx['date']}  "
          f"(ratings {ctx['rating_season']}, start {ctx.get('start_utc')})")
    print("=" * 78)
    for side in ('away', 'home'):
        c = ctx[side]
        lg = c['last_game']
        print(f"\n{side.upper()} {c['team']}: goalie {c['goalie_name']} [{c['goalie_status']}] | rest {c['rest']}d | "
              f"lines updated {c['lines_updated_at']}")
        print(f"  last game: API {lg['api']} ({lg['api_date']})  DB {lg['db']} ({lg['db_date']})"
              + (f"  ⚠ {len(c['missing_from_db'])} missing from DB" if c['missing_from_db'] else "  ✓ DB current"))
        if c.get('form_continuity') is not None:
            flag = '  ⚠ LOW' if c['form_continuity'] < CONTINUITY_WARN else ''
            print(f"  team-form continuity: {c['form_continuity']*100:.0f}% of tonight's ice time played in the last "
                  f"{FORM_WINDOW} DB games{flag}")
            if c['not_in_form_window']:
                print(f"    tonight, not in that window: {', '.join(c['not_in_form_window'])}")
            if c['gone_from_form_window']:
                print(f"    in that window, not tonight: {', '.join(c['gone_from_form_window'])}")
        print(f"  roster D-RAPM {c['roster_drapm']:+.4f}  O-RAPM {c['roster_orapm']:+.4f}  "
              f"({c['rating_fallbacks']}/18 on league-average fallback)")
        fmt = lambda x: f"{x['name']} ({x['toi']}m, D {x['d_rapm'] if x['d_rapm'] is not None else 'n/a'})"
        print(f"  IN  vs last game: {', '.join(fmt(x) for x in c['in']) or '-'}")
        print(f"  OUT vs last game: {', '.join(fmt(x) for x in c['out']) or '-'}")
        if c['injured']:
            print(f"  injured/IR: {', '.join(c['injured'])}")
    if ctx['warnings']:
        print("\nWARNINGS:")
        for w in ctx['warnings']:
            print(f"  ⚠ {w}")
    print(f"\nsnapshot: {ctx['snapshot'] if 'snapshot' in ctx else '(pending)'}")
    print("=" * 78 + "\n")


if __name__ == '__main__':
    import sys
    args = sys.argv[1:]
    if args and args[0] == '--lineups':
        date, away, home = args[1], args[2], args[3]
        lu = game_lineups(date, away, home)
        for side in ('away', 'home'):
            L = lu[side]
            print(f"\n{side.upper()} {L['team']}  (lines updated {L['updated_at']})")
            print(f"  goalie: {L['goalie']['name']} [{L['goalie']['status']}] id={L['goalie']['id']}")
            for s in L['skaters']:
                print(f"  {s['slot']:3s} {s['name']:24s} {s['id']}{'  GTD' if s['gtd'] else ''}{'  ' + s['injury'] if s['injury'] else ''}")
            print(f"  injured: {[i['name'] for i in L['injured']]}")
            if L['unresolved']:
                print(f"  UNRESOLVED NAMES: {L['unresolved']}")
    else:
        build_context(os.path.join(HERE, 'nhl_analytics.db'), args[0], args[1], args[2], allow_stale=True)
