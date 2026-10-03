"""
RAPM pipeline (one file): stint reconstruction + ridge RAPM + per-season ratings.

Subcommands:
  python rapm.py stints      Parse EdgeStats/shifts/*.json -> 2.2M 5v5 stints with
                             mp_shots xG attributed -> rapm_stints_5v5.pkl
  python rapm.py by-season   Leakage-safe EXPANDING-WINDOW O/D-RAPM per season
                             (season S fit on prior seasons) -> rapm_by_season.csv
                             (consumed by model_test.process_roster_rapm)
  python rapm.py fit         Single full-sample RAPM fit + leaderboard / face-validity
                             -> rapm_ratings.csv
  python rapm.py all         stints, then by-season (the model_test prep sequence)

RAPM is offense/defense split, exposure-scaled (total xG = T*rate, so short stints
can't explode):
    Obs A (home attacking): y=home_xg ; +T for home OFFENSE cols, away DEFENSE cols,
                            intercept, home dummy
    Obs B (away attacking): y=away_xg ; +T for away OFFENSE cols, home DEFENSE cols,
                            intercept
  beta_O[i] = effect on own team's xGF/60 (higher=better offense)
  beta_D[i] = effect on opponent's xGF/60 (lower=better defense); net = O - D
"""
import argparse
import json
import glob
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.sparse import csr_matrix

DB = "nhl_analytics.db"
SHIFTS_DIR = "EdgeStats/shifts"
STINTS = "rapm_stints_5v5.pkl"
FIT_OUT = "rapm_ratings.csv"
BY_SEASON_OUT = "rapm_by_season.csv"

# fit (single full-sample) settings
MIN_TOI_MIN = 2500.0
LAMBDAS = [0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0]
# by-season settings
LAM_SEASON = 5.0
MIN_PRIOR_STINTS = 50000


# ===========================================================================
# 1. STINT RECONSTRUCTION  (shifts -> stints + xG)
# ===========================================================================
def to_sec(period, mmss):
    m, s = mmss.split(':')
    return (int(period) - 1) * 1200 + int(m) * 60 + int(s)


def load_mappings(con):
    nhl2int = {int(n): int(t) for t, n in
               con.execute("SELECT team_id, NHL_TEAM_ID FROM teams WHERE NHL_TEAM_ID IS NOT NULL "
                           "UNION SELECT team_id, nhl_team_id FROM team_nhl_id_aliases")}
    games = {str(g): (int(h), int(a)) for g, h, a in
             con.execute("SELECT game_id, home_team_id, away_team_id FROM games WHERE game_type IN (2, 3)")}  # no preseason
    goalies = {str(r[0]).replace(' [G]', '').strip()
               for r in con.execute("SELECT DISTINCT player_id FROM mp_goalie_game_stats")}
    goalies |= {str(r[0]).replace(' [G]', '').strip()
                for r in con.execute("SELECT DISTINCT player_id FROM players WHERE position='G'")}
    return nhl2int, games, goalies


def game_stints(records, game_id, nhl2int, games, goalies):
    """5v5 stint dicts for one game (no shot xG yet)."""
    if game_id not in games:
        return []
    home_int, away_int = games[game_id]
    intervals = []
    for r in records:
        try:
            st = to_sec(r['period'], r['startTime'])
            en = to_sec(r['period'], r['endTime'])
        except Exception:
            continue
        if en <= st:
            continue
        tid = nhl2int.get(int(r['teamId']))
        side = 'H' if tid == home_int else ('A' if tid == away_int else None)
        if side is None:
            continue
        pid = str(r['playerId']).replace(' [G]', '').strip()
        intervals.append((st, en, pid, side, pid in goalies))
    if not intervals:
        return []
    bounds = sorted({i[0] for i in intervals} | {i[1] for i in intervals})
    stints = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        mid = (a + b) / 2.0
        h_sk, a_sk = [], []
        for st, en, pid, side, is_g in intervals:
            if st <= mid < en and not is_g:
                (h_sk if side == 'H' else a_sk).append(pid)
        if len(h_sk) == 5 and len(a_sk) == 5:
            stints.append({'game_id': game_id, 'start': a, 'end': b, 'dur': b - a,
                           'home_team': home_int, 'away_team': away_int,
                           'home_skaters': ' '.join(sorted(h_sk)),
                           'away_skaters': ' '.join(sorted(a_sk)),
                           'home_xg': 0.0, 'away_xg': 0.0})
    return stints


def attribute_shots(stints, shots_df):
    if not stints or shots_df.empty:
        return stints
    starts = np.array([s['start'] for s in stints])
    ends = np.array([s['end'] for s in stints])
    order = np.argsort(starts)
    starts_s, ends_s = starts[order], ends[order]
    for t, xg, is_home in zip(shots_df['time'].values, shots_df['x_goal'].values,
                              shots_df['is_home_team'].values):
        idx = np.searchsorted(starts_s, t, side='right') - 1
        if 0 <= idx < len(stints) and starts_s[idx] <= t < ends_s[idx]:
            s = stints[order[idx]]
            if int(is_home) == 1:
                s['home_xg'] += float(xg or 0.0)
            else:
                s['away_xg'] += float(xg or 0.0)
    return stints


def cmd_stints():
    con = sqlite3.connect(DB)
    nhl2int, games, goalies = load_mappings(con)
    print(f"mappings: {len(nhl2int)} teams, {len(games)} games, {len(goalies)} goalies")
    shots = pd.read_sql_query(
        "SELECT game_id, time, COALESCE(x_goal,0) AS x_goal, is_home_team "
        "FROM mp_shots WHERE time IS NOT NULL", con)
    shots['game_id'] = shots['game_id'].astype(str)
    shots_by_game = {g: d for g, d in shots.groupby('game_id')}
    con.close()

    files = sorted(glob.glob(f"{SHIFTS_DIR}/*.json"))
    print(f"parsing {len(files)} shift files...")
    all_stints, n_games = [], 0
    for i, fp in enumerate(files, 1):
        try:
            d = json.loads(Path(fp).read_text(encoding='utf-8'))
        except Exception:
            continue
        gid = str(d.get('_metadata', {}).get('game_id') or '')
        if not gid:
            continue
        st = game_stints(d.get('data', []), gid, nhl2int, games, goalies)
        if st:
            attribute_shots(st, shots_by_game.get(gid, pd.DataFrame()))
            all_stints.extend(st)
            n_games += 1
        if i % 1000 == 0:
            print(f"  {i}/{len(files)} files, {len(all_stints):,} stints so far")

    df = pd.DataFrame(all_stints)
    df.to_pickle(STINTS)
    tot_dur, tot_xg = df['dur'].sum(), df['home_xg'].sum() + df['away_xg'].sum()
    print(f"\ngames {n_games} | 5v5 stints {len(df):,} | 5v5 min {tot_dur/60:,.0f} "
          f"({tot_dur/60/n_games:.1f}/g) | xG {tot_xg:,.0f} ({tot_xg/n_games:.2f}/g) -> {STINTS}")


# ===========================================================================
# 2. RIDGE RAPM CORE  (design / solve / predict)  -- importable
# ===========================================================================
def _codes(df, uniques=None):
    N = len(df)
    H = df['home_skaters'].str.split(expand=True).values
    A = df['away_skaters'].str.split(expand=True).values
    if uniques is None:
        codes, uniques = pd.factorize(np.concatenate([H.ravel(), A.ravel()]))
        Hc = codes[:N * 5].reshape(N, 5)
        Ac = codes[N * 5:].reshape(N, 5)
    else:
        idx = {p: i for i, p in enumerate(uniques)}
        Hc = np.vectorize(lambda p: idx.get(p, -1))(H)
        Ac = np.vectorize(lambda p: idx.get(p, -1))(A)
    return Hc, Ac, uniques


def build_design(df):
    """Return X (2N x 2P+2), y (2N), uniques, P."""
    Hc, Ac, uniques = _codes(df)
    P, N = len(uniques), len(df)
    T = df['dur'].values / 3600.0
    INT, HOME = 2 * P, 2 * P + 1
    c1 = np.empty((N, 12), np.int64); d1 = np.empty((N, 12))
    c1[:, 0:5] = Hc;      d1[:, 0:5] = T[:, None]
    c1[:, 5:10] = P + Ac; d1[:, 5:10] = T[:, None]
    c1[:, 10] = INT;      d1[:, 10] = T
    c1[:, 11] = HOME;     d1[:, 11] = T
    r1 = np.repeat(np.arange(N), 12)
    c2 = np.empty((N, 11), np.int64); d2 = np.empty((N, 11))
    c2[:, 0:5] = Ac;      d2[:, 0:5] = T[:, None]
    c2[:, 5:10] = P + Hc; d2[:, 5:10] = T[:, None]
    c2[:, 10] = INT;      d2[:, 10] = T
    r2 = np.repeat(np.arange(N, 2 * N), 11)
    X = csr_matrix((np.concatenate([d1.ravel(), d2.ravel()]),
                   (np.concatenate([r1, r2]), np.concatenate([c1.ravel(), c2.ravel()]))),
                   shape=(2 * N, 2 * P + 2))
    y = np.concatenate([df['home_xg'].values, df['away_xg'].values]).astype(np.float64)
    return X, y, uniques, P


def solve_ridge(X, y, P, lam):
    AtA = (X.T @ X).toarray()
    Aty = X.T @ y
    reg = np.full(2 * P + 2, float(lam))
    reg[2 * P] = 0.0
    reg[2 * P + 1] = 0.0
    AtA[np.diag_indices_from(AtA)] += reg
    return np.linalg.solve(AtA, Aty)


def predict_net(df, beta, uniques, P):
    bO = pd.Series(beta[:P], index=uniques)
    bD = pd.Series(beta[P:2 * P], index=uniques)
    b_int, b_home = beta[2 * P], beta[2 * P + 1]
    T = df['dur'].values / 3600.0
    H = df['home_skaters'].str.split(expand=True)
    A = df['away_skaters'].str.split(expand=True)

    def s(frame, bser):
        out = np.zeros(len(frame))
        for k in range(5):
            out += frame[k].map(bser).fillna(0.0).values
        return out
    return T * (b_int + b_home + s(H, bO) + s(A, bD)) - T * (b_int + s(A, bO) + s(H, bD))


def player_toi_min(df):
    H = df['home_skaters'].str.split(expand=True).values
    A = df['away_skaters'].str.split(expand=True).values
    dur = df['dur'].values
    return (pd.Series(np.concatenate([np.repeat(dur, 5), np.repeat(dur, 5)]))
            .groupby(np.concatenate([H.ravel(), A.ravel()])).sum() / 60.0)


# ===========================================================================
# 3. PER-SEASON RATINGS  (leakage-safe expanding window) -> model_test
# ===========================================================================
def cmd_by_season():
    stints = pd.read_pickle(STINTS)
    con = sqlite3.connect(DB)
    gseason = dict(con.execute("SELECT game_id, season FROM games WHERE game_type IN (2, 3)"))
    con.close()
    stints['season'] = stints['game_id'].map(gseason)
    stints = stints.dropna(subset=['season'])
    seasons = sorted(stints['season'].unique())
    print(f"seasons: {seasons}")

    rows = []
    for S in seasons:
        prior = stints[stints['season'] < S]
        leaked = len(prior) < MIN_PRIOR_STINTS
        src = stints[stints['season'] == S] if leaked else prior
        X, y, ids, P = build_design(src)
        beta = solve_ridge(X, y, P, LAM_SEASON)
        toi = player_toi_min(src).to_dict()
        for j, pid in enumerate(ids):
            rows.append({'season': S, 'player_id': pid, 'o_rapm': beta[j], 'd_rapm': beta[P + j],
                         'toi_5v5_min': toi.get(pid, 0.0), 'leaked': int(leaked)})
        print(f"  {S}: {'OWN (first-season leak)' if leaked else 'prior'} {len(src):,} stints, {P} players")

    # Upcoming season (no stints yet): fit on every completed season, so its games get
    # real ratings instead of all falling back to league average.
    y = int(seasons[-1][:4]) + 1
    nxt = f"{y}-{y + 1}"
    X, y_, ids, P = build_design(stints)
    beta = solve_ridge(X, y_, P, LAM_SEASON)
    toi = player_toi_min(stints).to_dict()
    for j, pid in enumerate(ids):
        rows.append({'season': nxt, 'player_id': pid, 'o_rapm': beta[j], 'd_rapm': beta[P + j],
                     'toi_5v5_min': toi.get(pid, 0.0), 'leaked': 0})
    print(f"  {nxt}: prior (all {len(seasons)} seasons) {len(stints):,} stints, {P} players")
    pd.DataFrame(rows).to_csv(BY_SEASON_OUT, index=False)
    print(f"\nsaved {len(rows)} (season,player) ratings -> {BY_SEASON_OUT}")


# ===========================================================================
# 4. SINGLE FIT + LEADERBOARD / FACE-VALIDITY
# ===========================================================================
def cmd_fit():
    df = pd.read_pickle(STINTS)
    print(f"loaded {len(df):,} 5v5 stints")
    con = sqlite3.connect(DB)
    gdate = dict(con.execute("SELECT game_id, game_date FROM games WHERE game_type IN (2, 3)"))
    names = dict(con.execute("SELECT player_id, player_name FROM players"))
    pos = dict(con.execute("SELECT player_id, position FROM players"))
    con.close()
    df['date'] = df['game_id'].map(lambda g: gdate.get(g, ''))
    games_sorted = df[['game_id', 'date']].drop_duplicates().sort_values('date')['game_id'].values
    cut = int(len(games_sorted) * 0.85)
    test_games = set(games_sorted[cut:])
    is_test = df['game_id'].isin(test_games).values
    tr, te = df[~is_test].reset_index(drop=True), df[is_test].reset_index(drop=True)
    print(f"train stints {len(tr):,} | test stints {len(te):,} ({len(test_games)} test games)")

    Xtr, ytr, ids_tr, Ptr = build_design(tr)
    te_actual = (te.assign(net=te['home_xg'] - te['away_xg']).groupby('game_id')['net'].sum())
    print("\nlambda  |  test game R^2 | corr")
    best = None
    for lam in LAMBDAS:
        beta = solve_ridge(Xtr, ytr, Ptr, lam)
        pred = predict_net(te, beta, ids_tr, Ptr)
        te_pred = pd.Series(pred, index=te.index).groupby(te['game_id']).sum()
        m = pd.concat([te_actual, te_pred], axis=1).dropna()
        m.columns = ['actual', 'pred']
        r2 = 1 - ((m['actual'] - m['pred']) ** 2).sum() / ((m['actual'] - m['actual'].mean()) ** 2).sum()
        corr = np.corrcoef(m['actual'], m['pred'])[0, 1]
        flag = ''
        if best is None or r2 > best[1]:
            best = (lam, r2); flag = '  <-- best'
        print(f"  {lam:7.3f} |   {r2:+.4f}     | {corr:+.4f}{flag}")

    best_lam = best[0]
    print(f"\nRefitting on all {len(df):,} stints at lambda={best_lam} ...")
    X, y, ids, P = build_design(df)
    beta = solve_ridge(X, y, P, best_lam)
    toi_min = player_toi_min(df).to_dict()
    rt = pd.DataFrame({'player_id': ids,
                       'name': [names.get(p, names.get(p + ' [G]', p)) for p in ids],
                       'position': [pos.get(p, '?') for p in ids],
                       'o_rapm': beta[:P], 'd_rapm': beta[P:2 * P]})
    rt['net_rapm'] = rt['o_rapm'] - rt['d_rapm']
    rt['toi_5v5_min'] = rt['player_id'].map(toi_min).fillna(0.0)
    rt = rt.sort_values('net_rapm', ascending=False)
    rt.to_csv(FIT_OUT, index=False)
    print(f"intercept {beta[2*P]:+.3f} | home boost {beta[2*P+1]:+.3f} | saved {len(rt)} -> {FIT_OUT}\n")

    qual = rt[rt['toi_5v5_min'] >= MIN_TOI_MIN]
    for label, col in [('NET (O-D)', 'net_rapm'), ('OFFENSE', 'o_rapm')]:
        print(f"=== TOP 12 by {label} (>= {MIN_TOI_MIN:.0f} 5v5 min) ===")
        for _, r in qual.sort_values(col, ascending=False).head(12).iterrows():
            print(f"  {r[col]:+.3f}  {r['name']:22} {r['position']:2}  (O {r['o_rapm']:+.2f} / D {r['d_rapm']:+.2f})")
        print()


def main():
    ap = argparse.ArgumentParser(description="RAPM pipeline: stints + ridge + per-season ratings")
    ap.add_argument('cmd', nargs='?', default='help',
                    choices=['stints', 'by-season', 'fit', 'all', 'help'])
    a = ap.parse_args()
    if a.cmd == 'stints':
        cmd_stints()
    elif a.cmd == 'by-season':
        cmd_by_season()
    elif a.cmd == 'fit':
        cmd_fit()
    elif a.cmd == 'all':
        cmd_stints(); cmd_by_season()
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
