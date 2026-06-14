#!/usr/bin/env python3
"""
AHL Historical Game Data Scraper
Pulls data directly from the HockeyTech API backing theahl.com/stats/
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import requests


def _parse_date(s):
    """Parse 'YYYY-MM-DD' (the seasons feed format) into a date, or None."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


API_BASE = "https://lscluster.hockeytech.com/feed/index.php"
API_KEY = "ccb91f29d6744675"
CLIENT_CODE = "ahl"

# Season IDs from the bootstrap endpoint (both regular season and playoff).
# WARNING: HockeyTech season IDs are NOT chronological — id 46 is the *earliest*
# regular season (2004-05) while id 1 is 2005-06. Mapping below verified against
# actual game dates (the modulekit `seasons` feed is the source of truth):
# Regular: 46=2004-05, 1=2005-06, 8=2006-07, 12=2007-08, 16=2008-09,
#          30=2009-10, 34=2010-11, 37=2011-12, 40=2012-13, 43=2013-14,
#          48=2014-15, 51=2015-16, 54=2016-17, 57=2017-18, 61=2018-19,
#          65=2019-20, 68=2020-21, 73=2021-22, 77=2022-23, 81=2023-24,
#          86=2024-25, 90=2025-26
# Playoffs: 92=2026 Calder Cup, 88=2025, 84=2024, 80=2023, ... (oldest) 7
REGULAR_SEASON_IDS = [1, 8, 12, 16, 30, 34, 37, 40, 43, 46, 48, 51, 54, 57, 61, 65, 68, 73, 77, 81, 86, 90]
PLAYOFF_SEASON_IDS = [7, 10, 15, 29, 33, 36, 39, 42, 47, 50, 53, 56, 60, 64, 69, 72, 76, 80, 84, 88, 92]
ALL_SEASON_IDS = sorted(set(REGULAR_SEASON_IDS + PLAYOFF_SEASON_IDS))

# Thread-local storage so each worker thread gets its own requests.Session
_thread_local = threading.local()


class RateLimiter:
    """Token-bucket rate limiter — safe for concurrent threads."""

    def __init__(self, rate):
        """rate: maximum requests per second across all threads."""
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


class AHLScraper:
    """AHL game data scraper using the HockeyTech API."""

    def __init__(self, output_dir='./ahl_data', workers=8, rate=10.0):
        """
        output_dir: where to save game JSON files
        workers:    number of concurrent download threads
        rate:       max requests per second (global across all threads)
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self._rate_limiter = RateLimiter(rate)
        self._print_lock = threading.Lock()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _session(self):
        """Return this thread's requests.Session, creating it if needed."""
        if not hasattr(_thread_local, 'session'):
            s = requests.Session()
            s.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://theahl.com/',
            })
            _thread_local.session = s
        return _thread_local.session

    def _get(self, params, retries=3):
        """Make an API request with rate limiting and retry. Returns parsed JSON or None."""
        base_params = {
            'feed': 'statviewfeed',
            'key': API_KEY,
            'client_code': CLIENT_CODE,
            'lang': 'en',
            'fmt': 'json',
        }
        base_params.update(params)

        for attempt in range(retries):
            self._rate_limiter.acquire()
            try:
                resp = self._session().get(API_BASE, params=base_params, timeout=30)
                resp.raise_for_status()
                # API returns JSONP-wrapped: ({"key": ...}) — strip outer parens
                text = resp.text.strip()
                if text.startswith('(') and text.endswith(')'):
                    text = text[1:-1]
                return json.loads(text)
            except requests.RequestException as e:
                self._log(f"  [retry {attempt+1}/{retries}] {params.get('game_id','?')}: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
            except (ValueError, json.JSONDecodeError) as e:
                self._log(f"  [parse error] {params.get('game_id','?')}: {e}")
                return None
        return None

    def _log(self, msg):
        """Thread-safe print."""
        with self._print_lock:
            print(msg, flush=True)

    # ------------------------------------------------------------------
    # Schedule / game data
    # ------------------------------------------------------------------

    def get_schedule(self, season_id, played_only=True):
        """Return deduplicated list of game_id strings for a season."""
        data = self._get({'view': 'schedule', 'season': season_id})
        if not data:
            return []

        # Response: list[1] > sections[0] > data[N] > {'row': {'game_id':..., 'game_status':...}}
        game_ids = []
        seen = set()
        try:
            rows = data[0]['sections'][0]['data']
            for entry in rows:
                row = entry.get('row', {})
                gid = row.get('game_id')
                if not gid or gid in seen:
                    continue
                if played_only:
                    status = row.get('game_status', '')
                    if not status.startswith('Final'):
                        continue
                seen.add(gid)
                game_ids.append(str(gid))
        except (IndexError, KeyError, TypeError) as e:
            self._log(f"  Schedule parse error (season {season_id}): {e}")

        return game_ids

    def get_game_summary(self, game_id):
        """Return full game summary dict from API."""
        return self._get({'view': 'gameSummary', 'game_id': game_id})

    def game_file(self, game_id):
        return self.output_dir / f"game_{game_id}.json"

    # ------------------------------------------------------------------
    # Single-game worker (called from thread pool)
    # ------------------------------------------------------------------

    def scrape_game(self, game_id, overwrite=False):
        """Fetch and save a single game. Returns True on success."""
        out_file = self.game_file(game_id)
        if not overwrite and out_file.exists():
            return True  # already have it

        data = self.get_game_summary(game_id)
        if not data:
            return False

        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)

        return True

    # ------------------------------------------------------------------
    # Season / bulk scraping
    # ------------------------------------------------------------------

    def scrape_season(self, season_id, overwrite=False, played_only=True):
        """Scrape all games for a season using the thread pool.
        Returns (success_count, total_count).
        """
        self._log(f"\n=== Season {season_id} ===")
        game_ids = self.get_schedule(season_id, played_only=played_only)

        if not game_ids:
            self._log(f"  No games found (season {season_id})")
            return 0, 0

        # Separate already-done from work to do
        todo = [gid for gid in game_ids if overwrite or not self.game_file(gid).exists()]
        already = len(game_ids) - len(todo)
        self._log(f"  {len(game_ids)} games total | {already} cached | {len(todo)} to fetch")

        if not todo:
            return len(game_ids), len(game_ids)

        done_count = already
        fail_count = 0
        total = len(game_ids)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.scrape_game, gid, overwrite): gid for gid in todo}
            for future in as_completed(futures):
                gid = futures[future]
                try:
                    ok = future.result()
                except Exception as e:
                    ok = False
                    self._log(f"  [exception] game {gid}: {e}")

                with self._print_lock:
                    if ok:
                        done_count += 1
                    else:
                        fail_count += 1
                        print(f"  [FAIL] game {gid}", flush=True)
                    # Print a progress tick every 50 completions
                    completed = done_count + fail_count - already
                    if completed % 50 == 0 or completed == len(todo):
                        pct = (done_count + fail_count) / total * 100
                        print(f"  ... {done_count + fail_count}/{total} ({pct:.0f}%) "
                              f"| {fail_count} failed", flush=True)

        self._log(f"  Season {season_id}: {done_count}/{total} OK, {fail_count} failed")
        return done_count, total

    def scrape_all_seasons(self, season_ids=None, overwrite=False, played_only=True):
        """Scrape games for multiple seasons."""
        if season_ids is None:
            season_ids = ALL_SEASON_IDS

        total_ok = 0
        total_games = 0
        failed_seasons = []

        for sid in season_ids:
            ok, total = self.scrape_season(sid, overwrite=overwrite, played_only=played_only)
            total_ok += ok
            total_games += total
            if total > 0 and ok < total:
                failed_seasons.append(sid)

        self._log(f"\n=== Complete: {total_ok}/{total_games} games across {len(season_ids)} seasons ===")
        if failed_seasons:
            self._log(f"Seasons with partial failures: {failed_seasons}")

    def list_existing(self):
        """Print stats on what game files we already have."""
        files = list(self.output_dir.glob('game_*.json'))
        print(f"Existing game files in {self.output_dir}: {len(files)}")
        if files:
            ids = sorted(f.stem.replace('game_', '') for f in files)
            print(f"  Game ID range: {ids[0]} to {ids[-1]}")
        return files

    # ------------------------------------------------------------------
    # Season discovery / incremental update
    # ------------------------------------------------------------------

    def get_seasons_meta(self):
        """Fetch every season (id, name, dates, playoff flag) from the modulekit
        `seasons` feed. This is the API's own source of truth for which season
        IDs exist — so `update` can find new feeds (e.g. a new playoff round)
        without anything being hardcoded."""
        params = {
            'feed': 'modulekit', 'view': 'seasons',
            'key': API_KEY, 'client_code': CLIENT_CODE, 'fmt': 'json', 'lang': 'en',
        }
        for attempt in range(3):
            self._rate_limiter.acquire()
            try:
                resp = self._session().get(API_BASE, params=params, timeout=30)
                resp.raise_for_status()
                text = resp.text.strip()
                if text.startswith('(') and text.endswith(')'):
                    text = text[1:-1]
                return json.loads(text)['SiteKit']['Seasons']
            except (requests.RequestException, ValueError, KeyError) as e:
                self._log(f"  [seasons fetch retry {attempt+1}/3]: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
        return []

    def discover_current_seasons(self, grace_days=3, ref_date=None):
        """Return [(season_id:int, name:str)] for seasons active around today.

        'Active' = today falls within [start_date, end_date + grace_days].
        Covers the regular season and any in-progress playoff feed at once.
        If nothing is active (offseason) falls back to the single most recently
        finished season so an update still does something sensible.
        """
        today = ref_date or date.today()
        active, latest = [], None  # latest = (end_date, sid, name)
        for s in self.get_seasons_meta():
            try:
                sid = int(s.get('season_id'))
            except (TypeError, ValueError):
                continue
            name = s.get('season_name', '')
            sd, ed = _parse_date(s.get('start_date')), _parse_date(s.get('end_date'))
            if ed and (latest is None or ed > latest[0]):
                latest = (ed, sid, name)
            if sd and ed and sd <= today <= ed + timedelta(days=grace_days):
                active.append((sid, name))
        if not active and latest:
            active = [(latest[1], latest[2])]
        return active

    def update(self, grace_days=3, overwrite=False):
        """Auto-discover the current/active season(s) and fetch missing games.

        This is the self-maintaining entry point: it asks the API which seasons
        are current (regular + any live playoff round) and runs the normal
        missing-only delta on each — picking up brand-new season IDs with no
        code change. Safe to run on a schedule.
        """
        seasons = self.discover_current_seasons(grace_days=grace_days)
        if not seasons:
            self._log("update: no active seasons discovered.")
            return
        self._log("update: active season(s) -> " +
                  ", ".join(f"{sid} ({name})" for sid, name in seasons))
        for sid, name in seasons:
            self.scrape_season(sid, overwrite=overwrite)


def main():
    parser = argparse.ArgumentParser(description='Scrape AHL game data via HockeyTech API')
    subparsers = parser.add_subparsers(dest='command', required=True)

    def _add_common(p):
        p.add_argument('--output-dir', default='./ahl_data')
        p.add_argument('--workers', type=int, default=8,
                       help='Concurrent download threads (default: 8)')
        p.add_argument('--rate', type=float, default=10.0,
                       help='Max requests/sec across all threads (default: 10)')

    # Single game
    p_game = subparsers.add_parser('game', help='Scrape a single game')
    p_game.add_argument('game_id', help='Game ID (e.g. 1028452)')
    p_game.add_argument('--overwrite', action='store_true')
    _add_common(p_game)

    # Season
    p_season = subparsers.add_parser('season', help='Scrape all games for a season')
    p_season.add_argument('season_id', type=int, help='Season ID (e.g. 90 for 2025-26)')
    p_season.add_argument('--overwrite', action='store_true')
    p_season.add_argument('--all-games', action='store_true', help='Include unplayed/future games')
    _add_common(p_season)

    # All seasons
    p_all = subparsers.add_parser('all', help='Scrape all available seasons')
    p_all.add_argument('--overwrite', action='store_true')
    p_all.add_argument('--regular-only', action='store_true', help='Only regular seasons')
    p_all.add_argument('--playoffs-only', action='store_true', help='Only playoff seasons')
    p_all.add_argument('--all-games', action='store_true', help='Include unplayed/future games')
    _add_common(p_all)

    # Update — auto-discover current season(s) and fetch missing games
    p_update = subparsers.add_parser(
        'update', help='Auto-discover the current season(s) and fetch missing games')
    p_update.add_argument('--overwrite', action='store_true',
                          help='Re-fetch even cached games (catches stat corrections)')
    p_update.add_argument('--grace-days', type=int, default=3,
                          help='Also refresh seasons that ended within N days (default: 3)')
    _add_common(p_update)

    # List / seasons (no common args needed)
    subparsers.add_parser('list', help='List existing game files')
    subparsers.add_parser('seasons', help='Print known season IDs')

    args = parser.parse_args()
    output_dir = getattr(args, 'output_dir', './ahl_data')
    workers    = getattr(args, 'workers', 8)
    rate       = getattr(args, 'rate', 10.0)
    scraper = AHLScraper(output_dir=output_dir, workers=workers, rate=rate)

    if args.command == 'game':
        ok = scraper.scrape_game(args.game_id, overwrite=args.overwrite)
        sys.exit(0 if ok else 1)

    elif args.command == 'season':
        played_only = not args.all_games
        scraper.scrape_season(args.season_id, overwrite=args.overwrite, played_only=played_only)

    elif args.command == 'all':
        if args.regular_only:
            ids = REGULAR_SEASON_IDS
        elif args.playoffs_only:
            ids = PLAYOFF_SEASON_IDS
        else:
            ids = ALL_SEASON_IDS
        scraper.scrape_all_seasons(season_ids=ids, overwrite=args.overwrite,
                                   played_only=not args.all_games)

    elif args.command == 'update':
        scraper.update(grace_days=args.grace_days, overwrite=args.overwrite)

    elif args.command == 'list':
        scraper.list_existing()

    elif args.command == 'seasons':
        print(f"Regular season IDs: {REGULAR_SEASON_IDS}")
        print(f"Playoff season IDs:  {PLAYOFF_SEASON_IDS}")


if __name__ == '__main__':
    main()
