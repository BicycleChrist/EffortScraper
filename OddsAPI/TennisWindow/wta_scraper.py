#!/usr/bin/env python3
"""WTA Historical Rankings Scraper.

Pulls weekly WTA singles rankings from the official API
(api.wtatennis.com/tennis/players/ranked) into the same `rankings` table the
ATP scraper uses, tagged tour='WTA'. The API accepts an `at=` date (mid-week
dates snap back to the Monday), pages of up to 100 rows, and reaches back
well past 2016 — so the historical backfill is one paged GET per week, no
HTML scraping.

First run migrates the rankings table: adds a `tour` column (existing rows
become 'ATP') and widens the UNIQUE constraint to (ranking_date, rank, tour)
so the two tours' number-ones stop evicting each other.

Usage:
    python wta_scraper.py                 # incremental: current week only
    python wta_scraper.py --backfill      # weekly back to 2022-07-18 (ATP parity)
    python wta_scraper.py --backfill --since 2018-01-01
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "tennis_rankings.db"
API = "https://api.wtatennis.com/tennis/players/ranked"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
DEPTH_PAGES = 5          # 5 x 100 = top 500 (ATP table holds ~top 465)
BACKFILL_START = date(2022, 7, 18)   # first ATP week in the table
REQUEST_DELAY = 0.35


def migrate_schema(con: sqlite3.Connection) -> None:
    """Add tour column + widen the UNIQUE constraint (one-time, idempotent)."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(rankings)")]
    if "tour" in cols:
        return
    print("Migrating rankings schema (adding tour column)...")
    con.executescript("""
        CREATE TABLE rankings_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ranking_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            points INTEGER NOT NULL,
            age INTEGER DEFAULT 0,
            tournaments_played INTEGER DEFAULT 0,
            rank_change INTEGER DEFAULT NULL,
            tour TEXT NOT NULL DEFAULT 'ATP',
            UNIQUE(ranking_date, rank, tour) ON CONFLICT REPLACE
        );
        INSERT INTO rankings_new (id, ranking_date, rank, player_name, points,
                                  age, tournaments_played, rank_change, tour)
            SELECT id, ranking_date, rank, player_name, points,
                   age, tournaments_played, rank_change, 'ATP' FROM rankings;
        DROP TABLE rankings;
        ALTER TABLE rankings_new RENAME TO rankings;
        CREATE INDEX IF NOT EXISTS idx_date_rank ON rankings(ranking_date, rank);
        CREATE INDEX IF NOT EXISTS idx_player_date ON rankings(player_name, ranking_date);
        CREATE INDEX IF NOT EXISTS idx_tour ON rankings(tour);
    """)
    con.commit()
    print("Migration done.")


def fetch_week(session: requests.Session, at: date):
    """All ranked players for the ranking week containing `at`.

    Returns (ranking_date_str, rows) — the API snaps `at` to its Monday."""
    rows, ranked_at = [], None
    for page in range(DEPTH_PAGES):
        r = session.get(API, params={
            "page": page, "pageSize": 100, "type": "rankSingles",
            "sort": "asc", "metric": "SINGLES", "at": at.isoformat(),
        }, headers=HEADERS, timeout=30)
        r.raise_for_status()
        chunk = r.json()
        if not isinstance(chunk, list) or not chunk:
            break
        for it in chunk:
            p = it.get("player") or {}
            ranked_at = (it.get("rankedAt") or "")[:10] or ranked_at
            age = 0
            dob = p.get("dateOfBirth")
            if dob:
                try:
                    d = datetime.strptime(dob[:10], "%Y-%m-%d").date()
                    age = at.year - d.year - ((at.month, at.day) < (d.month, d.day))
                except ValueError:
                    pass
            rows.append((ranked_at, it.get("ranking"), p.get("fullName") or "",
                         it.get("points") or 0, age,
                         it.get("tournamentsPlayed") or 0,
                         it.get("movement"), "WTA"))
        if len(chunk) < 100:
            break
        time.sleep(REQUEST_DELAY)
    return ranked_at, [r_ for r_ in rows if r_[1] and r_[2]]


def store(con: sqlite3.Connection, rows) -> None:
    con.executemany(
        "INSERT INTO rankings (ranking_date, rank, player_name, points, age, "
        "tournaments_played, rank_change, tour) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()


def mondays(since: date, until: date):
    d = since + timedelta(days=(7 - since.weekday()) % 7) if since.weekday() else since
    while d <= until:
        yield d
        d += timedelta(weeks=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="fetch every week back to --since (default 2022-07-18)")
    ap.add_argument("--since", type=lambda s: date.fromisoformat(s),
                    default=BACKFILL_START)
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    migrate_schema(con)
    have = {d for (d,) in con.execute(
        "SELECT DISTINCT ranking_date FROM rankings WHERE tour='WTA'")}
    session = requests.Session()

    weeks = list(mondays(args.since, date.today())) if args.backfill else [date.today()]
    fetched = 0
    for wk in weeks:
        if args.backfill and wk.isoformat() in have:
            continue
        try:
            ranked_at, rows = fetch_week(session, wk)
        except requests.RequestException as e:
            print(f"  {wk}: request failed ({e}); continuing")
            time.sleep(2)
            continue
        if not rows:
            print(f"  {wk}: no data")
            continue
        if ranked_at in have:      # holiday weeks snap to an already-stored Monday
            continue
        have.add(ranked_at)
        store(con, rows)
        fetched += 1
        print(f"  {ranked_at}: {len(rows)} players")
        time.sleep(REQUEST_DELAY)

    n, w = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ranking_date) FROM rankings WHERE tour='WTA'"
    ).fetchone()
    print(f"\nWTA rankings: {n} rows over {w} weeks ({fetched} weeks fetched this run)")
    con.close()


if __name__ == "__main__":
    main()
