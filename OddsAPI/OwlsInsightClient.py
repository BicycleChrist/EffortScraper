#!/usr/bin/env python3
"""
OwlsInsightClient.py — async client for the Owls Insight odds API.

https://owlsinsight.com/docs — REST aggregator for sharp odds (Pinnacle +
PS3838 realtime feeds WITH per-market limits), 15 sportsbooks, prediction
markets, props, splits, live scores, and a historical archive.

Auth: Bearer key from Creds.OI_KEY (MVP tier: 300k req/month, 400/min,
15 concurrent). Every response carries x-ratelimit-* headers; the client
tracks them and backs off on 429.

Design mirrors novig_async.py: plain aiohttp coroutines that take a
ClientSession, no Qt imports, so the module drops into EffortOdds via
qasync/run_in_executor without a dedicated QThread. A thin sync wrapper
(OwlsInsightSync) and a CLI are provided for testing.

Endpoint map (all paths relative to https://api.owlsinsight.com):
  v1 unified:   /api/v1/{sport}/odds|moneyline|spreads|totals
                  ?books=&alternates=true&league=&exclude_exchanges=
  v1 realtime:  /api/v1/{sport}/realtime  and  /{sport}/ps3838-realtime
                  (Pinnacle wire format, per-market limits[], ?league= filter)
  v1 EV:        /api/v1/{sport}/ev?min_ev=&book=
  v1 props:     /api/v1/{sport}/props[/fanduel|draftkings|caesars|betmgm|bet365]
                /api/v1/{sport}/props/history?game_id=&player=&category=&hours=
  v1 splits:    /api/v1/{sport}/splits            (Circa + DK handle/tickets)
  v1 scores:    /api/v1/scores/live  and  /api/v1/{sport}/scores/live
  v1 normalize: /api/v1/normalize?name=&sport=    (+ /normalize/batch?names=)
  v1 history:   /api/v1/history/games|odds|props|stats|tennis-stats|
                  closing-odds|player-props|public-betting|game-stats-detail|
                  cs2/matches|cs2/players
  v1 prophetx:  /api/v1/prophetx/odds?sport=&kind=
  v2 sources:   /api/v2/{book}/... — per-book native payloads with ETag/304
                  support (pinnacle, bet365, fanduel, draftkings, hardrock,
                  mybookie, thunderpick, kalshi, polymarket, ...).
                  Multi-league books expose .../leagues for slug discovery —
                  always resolve dynamically, the league sets churn.

Limits ledger: Pinnacle publishes maxRiskStake per market and steps limits up
over an event's life (low overnight -> raised near start). The archive does
NOT store limits, so LimitLedger samples /{sport}/realtime on a coarse
cadence (one request covers the whole slate) and records every change to
SQLite. ~10-15 req/day/sport buys the full limit-raise history.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from Creds import OI_KEY

BASE_URL = "https://api.owlsinsight.com"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
MAX_CONCURRENT = 12          # tier allows 15; leave headroom
LEDGER_DB = Path(__file__).resolve().parent / "oi_limit_ledger.db"

V1_SPORTS = ("nba", "ncaab", "nfl", "nhl", "ncaaf", "mlb", "mma",
             "soccer", "tennis", "cs2", "valorant", "lol", "dota2")
REALTIME_SPORTS = ("nba", "ncaab", "nfl", "nhl", "ncaaf", "mlb", "tennis",
                   "soccer", "cricket", "darts", "handball")

# Per-book sport coverage for the v1 unified feed (docs "Sportsbooks &
# Coverage" table, 2026-07). Lets consumers distinguish "book doesn't cover
# this sport" (expected absence) from "book request failed". The Vegas
# books are US-majors only; BetOnline is MMA-only; tennis is Pinnacle/FD/
# DK/Novig. Player props exist only for US majors (no soccer/tennis props).
US_MAJORS = frozenset({"mlb", "nba", "nfl", "nhl", "ncaab", "ncaaf"})
V1_BOOK_COVERAGE: Dict[str, frozenset] = {
    "pinnacle":    US_MAJORS | {"soccer", "tennis"},
    "fanduel":     US_MAJORS | {"soccer", "tennis"},
    "draftkings":  US_MAJORS | {"soccer", "tennis"},
    "novig":       US_MAJORS | {"soccer", "tennis"},
    "betmgm":      US_MAJORS | {"soccer"},
    "bet365":      US_MAJORS | {"soccer"},
    "caesars":     US_MAJORS | {"soccer"},
    "circa":       US_MAJORS,
    "westgate":    US_MAJORS,
    "wynn":        US_MAJORS,
    "south_point": US_MAJORS,
    "stations":    US_MAJORS,
    "betonline":   frozenset({"mma"}),
    "1xbet":       frozenset({"cs2", "lol", "valorant", "soccer", "mlb"}),
}


def expected_books(sport: str) -> List[str]:
    """v1 unified books that should carry this sport (per coverage table)."""
    return sorted(b for b, sports in V1_BOOK_COVERAGE.items()
                  if sport in sports)


class OwlsInsightError(Exception):
    """API/transport failure. .status carries the HTTP code when known."""

    def __init__(self, message: str, status: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class OwlsInsightClient:
    """Async REST client. One instance per ClientSession lifetime.

    Usage:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as s:
            oi = OwlsInsightClient(s)
            odds = await oi.get_odds("tennis", books=["pinnacle"])
    """

    def __init__(self, session: aiohttp.ClientSession, api_key: str = OI_KEY):
        self.session = session
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "Accept": "application/json"}
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        # last-seen rate-limit state, updated on every response
        self.remaining_minute: Optional[int] = None
        self.remaining_month: Optional[int] = None
        # url -> (etag, parsed_body) for v2 If-None-Match short-circuits
        self._etag_cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------ core

    async def _get(self, path: str, params: Optional[dict] = None,
                   use_etag: bool = False, _retries: int = 2) -> Any:
        url = f"{BASE_URL}{path}"
        headers = dict(self._headers)
        cache_key = None
        if use_etag:
            cache_key = url + "?" + json.dumps(params or {}, sort_keys=True)
            cached = self._etag_cache.get(cache_key)
            if cached:
                headers["If-None-Match"] = f'"{cached[0]}"'
        async with self._sem:
            try:
                async with self.session.get(url, params=params,
                                            headers=headers) as resp:
                    self._track_limits(resp.headers)
                    if resp.status == 304 and cache_key:
                        return self._etag_cache[cache_key][1]
                    if resp.status == 429 and _retries > 0:
                        wait = float(resp.headers.get("retry-after", 2))
                        await asyncio.sleep(min(wait, 30))
                        return await self._get(path, params, use_etag,
                                               _retries - 1)
                    try:
                        body = await resp.json(content_type=None)
                    except (json.JSONDecodeError, ValueError):
                        # non-JSON body (404 page, empty response, proxy
                        # error) — surface it as a typed error, not a raw
                        # decode exception
                        raise OwlsInsightError(
                            f"{resp.status} on {path}: non-JSON response",
                            status=resp.status) from None
                    if resp.status >= 400:
                        raise OwlsInsightError(
                            f"{resp.status} on {path}: "
                            f"{body.get('message') or body.get('error') or body}",
                            status=resp.status)
                    if cache_key:
                        etag = (body.get("etag")
                                or (resp.headers.get("ETag") or "").strip('"'))
                        if etag:
                            self._etag_cache[cache_key] = (etag, body)
                    return body
            except aiohttp.ClientError as e:
                raise OwlsInsightError(f"transport error on {path}: {e}") from e

    def _track_limits(self, headers) -> None:
        rm = headers.get("x-ratelimit-remaining-minute")
        if rm and rm.isdigit():
            self.remaining_minute = int(rm)
        rmo = headers.get("x-ratelimit-remaining-month")
        if rmo and rmo.isdigit():
            self.remaining_month = int(rmo)

    @staticmethod
    def _csv(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return ",".join(value)

    @staticmethod
    def _params(**kwargs) -> dict:
        """Drop None values; stringify bools the way the API expects."""
        out = {}
        for k, v in kwargs.items():
            if v is None:
                continue
            out[k] = str(v).lower() if isinstance(v, bool) else v
        return out

    # ------------------------------------------------------- v1 unified odds

    async def get_odds(self, sport: str, market: str = "odds",
                       books: Optional[Sequence[str]] = None,
                       alternates: bool = False,
                       league: Optional[str] = None,
                       exclude_exchanges: bool = False) -> dict:
        """Unified odds. market: odds | moneyline | spreads | totals.

        Response .data is keyed by book name; Pinnacle markets carry
        limits[] and (with alternates=True) alternateLines[].
        """
        return await self._get(f"/api/v1/{sport}/{market}", self._params(
            books=self._csv(books), alternates=alternates or None,
            league=league, exclude_exchanges=exclude_exchanges or None))

    # ---------------------------------------------------------- v1 realtime

    async def get_realtime(self, sport: str, league: Optional[str] = None,
                           backup_feed: bool = False) -> dict:
        """Real-time sharp odds (Pinnacle MQTT, or PS3838 backup feed).

        Full slate for the sport incl. low-tier events, every market with
        limits[] ({type: maxRiskStake, amount}). Per-event freshness block.
        """
        ep = "ps3838-realtime" if backup_feed else "realtime"
        return await self._get(f"/api/v1/{sport}/{ep}",
                               self._params(league=league))

    async def get_esports_realtime(self, game: str, league: str) -> dict:
        """Pinnacle esports realtime (cs2|lol|valorant|dota2). league required."""
        return await self._get(f"/api/v1/{game}/realtime", {"league": league})

    # ----------------------------------------------------------------- v1 EV

    async def get_ev(self, sport: str, min_ev: Optional[float] = None,
                     book: Optional[str] = None) -> dict:
        """+EV moneyline bets vs de-vigged consensus (beta, pregame h2h only)."""
        return await self._get(f"/api/v1/{sport}/ev",
                               self._params(min_ev=min_ev, book=book))

    # -------------------------------------------------------------- v1 props

    async def get_props(self, sport: str, book: Optional[str] = None,
                        game_id: Optional[str] = None,
                        player: Optional[str] = None,
                        category: Optional[str] = None,
                        books: Optional[Sequence[str]] = None) -> dict:
        """Player props. book=None aggregates all books; alt lines included."""
        path = (f"/api/v1/{sport}/props/{book}" if book
                else f"/api/v1/{sport}/props")
        return await self._get(path, self._params(
            game_id=game_id, player=player, category=category,
            books=self._csv(books)))

    async def get_props_history(self, sport: str, game_id: str, player: str,
                                category: str,
                                hours: Optional[int] = None) -> dict:
        return await self._get(f"/api/v1/{sport}/props/history", self._params(
            game_id=game_id, player=player, category=category, hours=hours))

    # ---------------------------------------------------- v1 splits / scores

    async def get_splits(self, sport: str) -> dict:
        """Circa + DraftKings handle%/tickets% (US majors only)."""
        return await self._get(f"/api/v1/{sport}/splits")

    async def get_live_scores(self, sport: Optional[str] = None) -> dict:
        path = (f"/api/v1/{sport}/scores/live" if sport
                else "/api/v1/scores/live")
        return await self._get(path)

    # ---------------------------------------------------- schedule / results

    async def get_schedule(self, sport: str) -> dict:
        """TODAY's not-yet-started games (status.state == 'pre'). Same event
        shape as live scores, incl. team logoUrl. Today only — historical
        slates live in /history/games."""
        return await self._get(f"/api/v1/{sport}/schedule")

    async def get_results(self, sport: str) -> dict:
        """TODAY's completed games with final scores (status.state == 'post')."""
        return await self._get(f"/api/v1/{sport}/results")

    # --------------------------------------------------------- player stats

    async def get_stats(self, sport: str = "nba",
                        date: Optional[str] = None,
                        player: Optional[str] = None) -> dict:
        """Box scores for today's (or a given date's) games. NBA."""
        return await self._get(f"/api/v1/{sport}/stats",
                               self._params(date=date, player=player))

    async def get_player_averages(self, sport: str, player_name: str,
                                  opponent: Optional[str] = None) -> dict:
        """L5/L10/L20 rolling averages + game log (NBA, NCAAB).
        opponent= filters to head-to-head games."""
        return await self._get(f"/api/v1/{sport}/stats/averages", self._params(
            playerName=player_name, opponent=opponent))

    # ------------------------------------------------------------ normalize

    async def normalize(self, name: str, sport: str) -> dict:
        return await self._get("/api/v1/normalize",
                               {"name": name, "sport": sport})

    async def normalize_batch(self, names: Sequence[str], sport: str) -> dict:
        """Up to 25 names per call."""
        return await self._get("/api/v1/normalize/batch",
                               {"names": ",".join(names), "sport": sport})

    # -------------------------------------------------------------- history

    async def get_history_games(self, **filters) -> dict:
        """Filters: sport, season, team, gameType, startDate, endDate,
        limit (<=100), offset."""
        return await self._get("/api/v1/history/games", self._params(**filters))

    async def get_history_odds(self, event_id: str, limit: Optional[int] = None,
                               offset: Optional[int] = None) -> dict:
        """Archived odds ticks: (book, market, side, price, point,
        recordedAt). NOTE: no limits and no Pinnacle rows — that's what
        LimitLedger exists for."""
        return await self._get("/api/v1/history/odds", self._params(
            eventId=event_id, limit=limit, offset=offset))

    async def get_history_props(self, event_id: str, **filters) -> dict:
        return await self._get("/api/v1/history/props",
                               self._params(eventId=event_id, **filters))

    async def get_history_stats(self, **filters) -> dict:
        return await self._get("/api/v1/history/stats", self._params(**filters))

    async def get_tennis_stats(self, event_id: str) -> dict:
        """Per-match and per-set stats (aces, serve %, BPs, winners, UEs)."""
        return await self._get("/api/v1/history/tennis-stats",
                               {"eventId": event_id})

    async def get_closing_odds(self, **filters) -> dict:
        """Filters: eventId or sport (+book, startDate, endDate, season,
        limit, offset). 9 books, 2016-present."""
        return await self._get("/api/v1/history/closing-odds",
                               self._params(**filters))

    async def get_history_player_props(self, **filters) -> dict:
        """NBA 2022-, MLB 2024-25 closing prop lines (ESPN BET + DK).
        Filters: eventId/sport/player, propType, book, dates, limit, offset."""
        return await self._get("/api/v1/history/player-props",
                               self._params(**filters))

    async def get_public_betting(self, **filters) -> dict:
        return await self._get("/api/v1/history/public-betting",
                               self._params(**filters))

    async def get_game_stats_detail(self, event_id: str) -> dict:
        return await self._get("/api/v1/history/game-stats-detail",
                               {"eventId": event_id})

    # ------------------------------------------------------------- prophetx

    async def get_prophetx(self, sports, kind: Optional[str] = None) -> dict:
        """sports: slug or list (basketball, baseball, tennis, golf, ...).
        kind: game | prop."""
        return await self._get("/api/v1/prophetx/odds", self._params(
            sport=self._csv(sports), kind=kind))

    # ------------------------------------------------------------ v2 source
    # Per-book native payloads. Multi-league books need a slug from
    # v2_leagues() — the live league set churns, never hard-code slugs.
    # All v2 endpoints support ETag/304; _get(use_etag=True) handles it.

    async def v2_leagues(self, book: str, sport: str,
                         state: Optional[str] = None) -> dict:
        """League discovery. state ('az'|'fl') applies to hardrock only."""
        if book == "hardrock":
            return await self._get(f"/api/v2/hardrock/{state}/{sport}/leagues")
        return await self._get(f"/api/v2/{book}/{sport}/leagues")

    async def v2_source(self, book: str, sport: str,
                        league: Optional[str] = None,
                        state: Optional[str] = None) -> dict:
        """Native book payload. Examples:
            v2_source("pinnacle", "tennis", league="atp-bastad-r1")
              -> raw MQTT matchups: markets with limits[], cutoffAt, version
            v2_source("hardrock", "TENNIS", state="fl")
              -> selections carry rootIdx (decode via hardrock_ladder)
            v2_source("bet365", "nba") / ("mybookie", "mlb")
            v2_source("kalshi", "mlb", league="kxmlbgame")
        """
        if book == "hardrock":
            path = f"/api/v2/hardrock/{state}/{sport}"
        else:
            path = f"/api/v2/{book}/{sport}"
        return await self._get(path, self._params(league=league),
                               use_etag=True)

    async def hardrock_ladder(self) -> Dict[int, int]:
        """rootIdx -> American odds map (static reference, cached)."""
        if not hasattr(self, "_hr_ladder"):
            body = await self._get("/api/v2/hardrock/ladder")
            self._hr_ladder = {e["rootIdx"]: e["americanOdds"]
                               for e in body["data"]["ladder"]}
        return self._hr_ladder

    async def decode_root_idx(self, root_idx: int) -> int:
        """Hard Rock price decode: exact lookup, linear interpolation in the
        gaps (per the API's own decoding note)."""
        ladder = await self.hardrock_ladder()
        if root_idx in ladder:
            return ladder[root_idx]
        keys = sorted(ladder)
        if root_idx <= keys[0]:
            return ladder[keys[0]]
        if root_idx >= keys[-1]:
            return ladder[keys[-1]]
        lo = max(k for k in keys if k < root_idx)
        hi = min(k for k in keys if k > root_idx)
        frac = (root_idx - lo) / (hi - lo)
        raw = ladder[lo] + frac * (ladder[hi] - ladder[lo])
        # ladder skips the +/-100 discontinuity; clamp per API docs
        if -100 < raw < 100:
            raw = 100 if raw > 0 else -100
        return int(round(raw))


# ===========================================================================
# FULL BOOK MATRIX — every book's view of one sport, fetched concurrently
# ===========================================================================
#
# Books arrive over three surfaces:
#   * v1 unified /odds     -> up to 15 books in ONE response (mainlines only)
#   * v1 /realtime         -> Pinnacle full depth + limits
#   * v2 per-book sources  -> native depth (CRIS, Stake, Thunderpick, ...)
# Bovada is NOT offered by this API (Boddssuck/BovProps.py remains the source).
#
# V2_SPORT_SLUGS maps our canonical sport -> each v2 book's own slug.
# None = book doesn't cover the sport (skipped). Multi-league books
# (bookmaker, hardrock, fanduel, draftkings, pinnacle-v2) need a league key,
# which full_book_matrix resolves via /leagues with an optional substring hint.

V2_SPORT_SLUGS: Dict[str, Dict[str, Optional[str]]] = {
    # book -> {canonical sport -> book slug}
    "bookmaker":   {"tennis": "tennis", "soccer": "soccer", "mlb": "baseball",
                    "nba": "basketball", "ncaab": "basketball",
                    "nfl": "football", "ncaaf": "football",
                    "nhl": "ice-hockey", "mma": "mma",
                    "cs2": "esports", "lol": "esports", "valorant": "esports"},
    "stake":       {"tennis": "tennis", "soccer": "soccer", "mlb": "baseball",
                    "nba": "basketball", "nhl": "ice-hockey"},
    "thunderpick": {"tennis": "tennis", "mlb": "baseball", "nba": "basketball",
                    "cs2": "cs2", "lol": "lol", "valorant": "valorant",
                    "dota2": "dota2"},
    "mybookie":    {"mlb": "mlb", "nba": "nba"},
    "underdog":    {"tennis": "tennis", "mlb": "mlb", "nba": "nba",
                    "nhl": "nhl", "nfl": "nfl", "soccer": "soccer",
                    "mma": "mma", "cs2": "cs2", "lol": "lol",
                    "valorant": "valorant"},
    "hardrock":    {"tennis": "TENNIS", "mlb": "BASEBALL", "nba": "BASKETBALL",
                    "nhl": "ICE_HOCKEY", "nfl": "AMERICAN_FOOTBALL",
                    "soccer": "SOCCER", "mma": "MMA"},
}
# hardrock big sports need ?league=; small sports (TENNIS, MMA, ...) don't.
HARDROCK_LEAGUE_REQUIRED = {"BASKETBALL", "BASEBALL", "ICE_HOCKEY", "SOCCER"}
# multi-league v2 books whose league key we resolve via /leagues + hint
V2_LEAGUE_BOOKS = ("bookmaker", "fanduel", "draftkings", "pinnacle")


async def full_book_matrix(oi: "OwlsInsightClient", sport: str,
                           league_hint: Optional[str] = None,
                           v2_books: Optional[Sequence[str]] = None,
                           hardrock_state: str = "fl") -> Dict[str, Any]:
    """Fetch every available book's view of a sport concurrently.

    Returns {source_name: payload} where payload is that source's raw
    response (None on failure — sources fail independently). Sources:
      'unified'  v1 odds, .data keyed by book (up to 15 books, one request)
      'realtime' Pinnacle sharp feed with limits
      per-v2-book entries ('bookmaker', 'stake', 'thunderpick', ...)

    league_hint: case-insensitive substring to pick a league for the
    multi-league v2 books (e.g. "istanbul"); those books are skipped when
    no hint is given and the sport needs one.
    """
    if v2_books is None:
        v2_books = ("bookmaker", "stake", "thunderpick", "mybookie",
                    "hardrock", "underdog")

    async def guarded(name, coro):
        try:
            return name, await coro
        except OwlsInsightError as e:
            return name, {"__error__": str(e)}

    async def v2_with_league(book: str, slug: str):
        """Resolve a league via /leagues + hint, then fetch it."""
        state = hardrock_state if book == "hardrock" else None
        lg = await oi.v2_leagues(book, slug, state=state)
        keys = [l.get("leagueKey") for l in lg.get("leagues", [])]
        pick = None
        if league_hint:
            hint = league_hint.lower()
            pick = next((k for k in keys if k and hint in k.lower()), None)
        if pick is None:
            return {"__skipped__": f"no league match for hint "
                                   f"{league_hint!r}; available: {keys}"}
        return await oi.v2_source(book, slug, league=pick, state=state)

    # unified/realtime cover the whole sport in one request each; do NOT
    # league-filter them — book league labels differ ("WTA 125K Istanbul"
    # vs "WTA Istanbul III") and a filter silently drops books. The hint
    # is only for resolving v2 league slugs.
    tasks = [
        guarded("unified", oi.get_odds(sport, alternates=True)),
    ]
    if sport in REALTIME_SPORTS or sport in ("cs2", "lol", "valorant",
                                             "dota2"):
        tasks.append(guarded("realtime", oi.get_realtime(sport)))
    for book in v2_books:
        slug = V2_SPORT_SLUGS.get(book, {}).get(sport)
        if slug is None:
            continue
        if book == "hardrock":
            if slug in HARDROCK_LEAGUE_REQUIRED:
                tasks.append(guarded(book, v2_with_league(book, slug)))
            else:
                tasks.append(guarded(book, oi.v2_source(
                    book, slug, state=hardrock_state)))
        elif book in V2_LEAGUE_BOOKS:
            tasks.append(guarded(book, v2_with_league(book, slug)))
        else:
            tasks.append(guarded(book, oi.v2_source(book, slug)))
    results = await asyncio.gather(*tasks)
    return dict(results)


def _surname_key(name: str) -> frozenset:
    """Order/format-insensitive tennis participant key: longest token of
    each name ('Yuan, Yue' / 'Yue Yuan' -> 'yuan')."""
    import re
    parts = [p.strip() for p in re.split(
        r"\s+vs\.?\s+|\s+@\s+", name, flags=re.IGNORECASE) if p.strip()]
    keys = set()
    for p in parts:
        tokens = [t.strip(".,").lower() for t in p.split() if len(t) > 2]
        if tokens:
            keys.add(max(tokens, key=len))
    return frozenset(keys)


def match_event_across_books(matrix: Dict[str, Any], home: str,
                             away: str) -> Dict[str, Any]:
    """Best-effort per-source extraction of one event from a
    full_book_matrix() result, keyed by surname/team-token overlap.
    Returns {source_or_book: native_event_payload}."""
    want = _surname_key(f"{home} vs {away}")
    found: Dict[str, Any] = {}

    def hit(name: str) -> bool:
        return len(_surname_key(name) & want) >= min(2, len(want))

    uni = matrix.get("unified") or {}
    for book, events in (uni.get("data") or {}).items():
        for e in events:
            if hit(f"{e.get('home_team','')} vs {e.get('away_team','')}"):
                found[book] = e
                break
    rt = matrix.get("realtime") or {}
    for e in rt.get("data", []):
        if hit(f"{e.get('home_team','')} vs {e.get('away_team','')}"):
            found["pinnacle_realtime"] = e
            break
    # underdog is excluded: its payload is relational (players/appearances/
    # over_under_lines), not per-event — join it by player name instead.
    for source in ("bookmaker", "stake", "thunderpick", "mybookie",
                   "hardrock"):
        body = matrix.get(source)
        if not isinstance(body, dict) or "__error__" in body:
            continue
        data = body.get("data")
        items: Iterable = (data.values() if isinstance(data, dict)
                           else data or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            label = (item.get("name")
                     or " vs ".join(filter(None, [
                         (item.get("visitor") or {}).get("team")
                         if isinstance(item.get("visitor"), dict) else None,
                         (item.get("home") or {}).get("team")
                         if isinstance(item.get("home"), dict) else None]))
                     or json.dumps(item)[:200])
            if hit(label):
                found[source] = item
                break
    return found


# ===========================================================================
# LIMIT LEDGER — sampled history of Pinnacle maxRiskStake per market
# ===========================================================================
#
# One /realtime request returns the entire slate for a sport, so a coarse
# cadence (default 2h, densifying to 30min inside the final 3h before an
# event starts) captures every limit step for every event at ~10-15
# requests/day/sport. Rows are written only when a limit CHANGES.

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS limit_ledger (
    sport        TEXT NOT NULL,
    event_id     TEXT NOT NULL,   -- Pinnacle event id
    home_team    TEXT,
    away_team    TEXT,
    league       TEXT,
    commence_time TEXT,
    market_key   TEXT NOT NULL,   -- h2h / spreads / totals / special_...
    point        REAL,            -- line for spread/total markets, else NULL
    limit_amount REAL NOT NULL,
    seen_at      TEXT NOT NULL,   -- UTC ISO, when this value was observed
    PRIMARY KEY (event_id, market_key, point, seen_at)
);
CREATE INDEX IF NOT EXISTS idx_ledger_event ON limit_ledger(event_id);
CREATE INDEX IF NOT EXISTS idx_ledger_sport_time ON limit_ledger(sport, seen_at);
"""


class LimitLedger:
    """Samples realtime feeds and persists limit changes to SQLite."""

    def __init__(self, db_path: Path = LEDGER_DB):
        self.db_path = db_path
        self._db = sqlite3.connect(str(db_path))
        self._db.executescript(LEDGER_SCHEMA)
        # (event_id, market_key, point) -> last recorded amount
        self._last: Dict[tuple, float] = {}
        self._load_last()

    def _load_last(self) -> None:
        cur = self._db.execute(
            """SELECT event_id, market_key, point, limit_amount FROM (
                   SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY event_id, market_key, point
                       ORDER BY seen_at DESC) rn
                   FROM limit_ledger) WHERE rn = 1""")
        for eid, mkey, point, amount in cur:
            self._last[(eid, mkey, point)] = amount

    def record_snapshot(self, sport: str, realtime_body: dict) -> int:
        """Diff one get_realtime() response against known state.
        Returns number of new rows (changes) written."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for event in realtime_body.get("data", []):
            eid = str(event.get("id"))
            for bm in event.get("bookmakers", []):
                for market in bm.get("markets", []):
                    limits = market.get("limits") or []
                    amount = next((l["amount"] for l in limits
                                   if l.get("type") == "maxRiskStake"), None)
                    if amount is None:
                        continue
                    point = next((o.get("point")
                                  for o in market.get("outcomes", [])
                                  if o.get("point") is not None), None)
                    key = (eid, market["key"], point)
                    if self._last.get(key) == amount:
                        continue
                    self._last[key] = amount
                    rows.append((sport, eid, event.get("home_team"),
                                 event.get("away_team"), event.get("league"),
                                 event.get("commence_time"), market["key"],
                                 point, amount, now))
        if rows:
            self._db.executemany(
                "INSERT OR IGNORE INTO limit_ledger VALUES "
                "(?,?,?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
        return len(rows)

    def event_history(self, event_id: str) -> List[tuple]:
        return self._db.execute(
            "SELECT market_key, point, limit_amount, seen_at FROM limit_ledger"
            " WHERE event_id = ? ORDER BY seen_at, market_key",
            (str(event_id),)).fetchall()

    def close(self) -> None:
        self._db.close()


def _next_sample_delay(realtime_body: dict, coarse_s: float = 7200,
                       dense_s: float = 1800, dense_window_s: float = 10800,
                       floor_s: float = 300) -> float:
    """Cadence policy: coarse by default, dense when any pregame event starts
    within dense_window_s. Never below floor_s."""
    now = datetime.now(timezone.utc)
    soonest = None
    for event in realtime_body.get("data", []):
        if event.get("isLive"):
            continue
        ct = event.get("commence_time")
        if not ct:
            continue
        try:
            start = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            continue
        dt = (start - now).total_seconds()
        if dt > 0 and (soonest is None or dt < soonest):
            soonest = dt
    if soonest is not None and soonest <= dense_window_s:
        return max(min(dense_s, soonest), floor_s)
    return coarse_s


async def run_limit_ledger(sports: Sequence[str],
                           db_path: Path = LEDGER_DB,
                           once: bool = False,
                           log=print) -> None:
    """Sampler loop. `once=True` takes a single snapshot and returns."""
    ledger = LimitLedger(db_path)
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            oi = OwlsInsightClient(session)
            while True:
                delay = 7200.0
                for sport in sports:
                    try:
                        body = await oi.get_realtime(sport)
                    except OwlsInsightError as e:
                        log(f"[ledger] {sport}: {e}")
                        continue
                    n = ledger.record_snapshot(sport, body)
                    events = body.get("meta", {}).get("events", 0)
                    log(f"[ledger] {sport}: {events} events, "
                        f"{n} limit changes recorded")
                    delay = min(delay, _next_sample_delay(body))
                if once:
                    return
                log(f"[ledger] next sample in {delay / 60:.0f} min "
                    f"(remaining this month: {oi.remaining_month})")
                await asyncio.sleep(delay)
    finally:
        ledger.close()



# ===========================================================================
# EFFORTODDS INTEGRATION LAYER
# ===========================================================================
# Everything below serves EffortOdds' odds pipeline (EffortOdds.py and
# oddsscreen.py): TheOddsAPI-schema conversion, per-game merging with the
# append-only / PREFER_OI_BOOKS policy, "oi:" query-slot slates, and the
# QueryList sport/market catalog entries. It is Qt-free and fail-soft —
# any OI failure degrades to "no extra books", never an exception into the
# render loop.

# ---------------------------------------------------------------------------
# OI-native query slots ("oi:" pseudo sport keys)
#
# TheOddsAPI's league list doesn't know about OI-only coverage (full-depth
# tennis down to ITF, esports via 1xBet, cricket/darts/handball via the
# Pinnacle realtime feed). These pseudo-sports are appended to the QueryList
# sport picker; refresh_data routes "oi:*" slots through fetch_oi_games()
# instead of PropClient, building the tab entirely from OI data.
# ---------------------------------------------------------------------------

OI_QUERY_SPORTS = [
    ("OI · Tennis (ATP→ITF)", "oi:tennis"),
    ("OI · Soccer (all leagues)", "oi:soccer"),
    ("OI · CS2", "oi:cs2"),
    ("OI · League of Legends", "oi:lol"),
    ("OI · Valorant", "oi:valorant"),
    ("OI · Dota 2", "oi:dota2"),
    ("OI · Cricket", "oi:cricket"),
    ("OI · Darts", "oi:darts"),
    ("OI · Handball", "oi:handball"),
    ("OI · MMA", "oi:mma"),
]

# Markets panel entries (GAME_MARKETS format: {section: [keys]}). Keys are
# OI's own market keys; unknown ones fall back to the raw key as the
# checkbox label. Dynamic per-event "special_*" keys can't be listed here —
# they're included only when the user has no market filter active.
OI_GAME_MARKETS = {
    "oi:tennis": {
        "GAME LINES": ["h2h", "spreads", "totals", "total_games"],
        "SETS": ["1st_set_winner", "1st_set_spreads", "1st_set_totals",
                 "team_totals"],
    },
    "oi:soccer": {
        "GAME LINES": ["h2h", "spreads", "totals", "double_chance", "btts",
                       "asian_handicap"],
        "1ST HALF": ["first_half_h2h", "first_half_spreads",
                     "first_half_totals"],
        "SECONDARY": ["corners", "corner_spreads", "corner_totals", "cards"],
    },
    "oi:cs2": {
        "GAME LINES": ["h2h", "spreads", "totals", "map_winner",
                       "correct_score"],
        "ROUNDS": ["round_totals", "round_handicap"],
    },
    "oi:lol": {"GAME LINES": ["h2h", "spreads", "totals", "map_winner",
                              "correct_score"]},
    "oi:valorant": {"GAME LINES": ["h2h", "spreads", "totals", "map_winner",
                                   "correct_score"]},
    "oi:dota2": {"GAME LINES": ["h2h", "spreads", "totals"]},
    "oi:cricket": {"GAME LINES": ["h2h", "spreads", "totals"]},
    "oi:darts": {"GAME LINES": ["h2h", "spreads", "totals"]},
    "oi:handball": {"GAME LINES": ["h2h", "spreads", "totals"]},
    "oi:mma": {"GAME LINES": ["h2h", "spreads", "totals"]},
}

# TheOddsAPI sport_key -> OI sport. Prefix matching handles the per-league
# keys ("soccer_epl", "tennis_atp_wimbledon", ...). Keys with no entry
# (e.g. basketball_nba_summer_league) are simply not covered by OI.
_EXACT_SPORT_MAP = {
    "basketball_nba": "nba",
    "basketball_ncaab": "ncaab",
    "basketball_wnba": None,          # OI: EV-only, no unified odds
    "americanfootball_nfl": "nfl",
    "americanfootball_ncaaf": "ncaaf",
    "icehockey_nhl": "nhl",
    "baseball_mlb": "mlb",
    "mma_mixed_martial_arts": "mma",
}
_PREFIX_SPORT_MAP = (
    ("soccer_", "soccer"),
    ("tennis_", "tennis"),
)

# OI book key -> canonical id, for dedup against TheOddsAPI titles.
# TheOddsAPI titles observed in the grid: "BetOnline.ag", "FanDuel",
# "DraftKings", "Hard Rock Bet", "BetRivers", "Bovada", "Fliff",
# "theScore Bet", "BetMGM", "Caesars", "Bet365", ...
_TITLE_ALIASES = {
    "betonlineag": "betonline",
    "hardrockbet": "hardrock",
    "circasports": "circa",
    "thescorebet": "thescore",
    "southpoint": "south_point",
    "stationcasinos": "stations",
    "1xbet": "1xbet",
}


def _canon(title: str) -> str:
    """Canonical book id: lowercase alphanumerics, then alias-folded."""
    key = "".join(ch for ch in title.lower() if ch.isalnum())
    return _TITLE_ALIASES.get(key, key)


def map_sport_key(theoddsapi_sport_key: str) -> Optional[str]:
    """TheOddsAPI sport_key -> OI sport, or None when OI has no coverage."""
    if theoddsapi_sport_key in _EXACT_SPORT_MAP:
        return _EXACT_SPORT_MAP[theoddsapi_sport_key]
    for prefix, oi_sport in _PREFIX_SPORT_MAP:
        if theoddsapi_sport_key.startswith(prefix):
            return oi_sport
    return None


def _team_key(*names: str) -> frozenset:
    """Order-insensitive event key from full team/player names."""
    return frozenset("".join(ch for ch in n.lower() if ch.isalnum())
                     for n in names if n)


def _participant_key(*names: str) -> frozenset:
    """Fallback key: longest token per participant. Handles 'Yuan, Yue'
    vs 'Yue Yuan' and 'LA Clippers' vs 'Los Angeles Clippers'."""
    keys = set()
    for n in names:
        tokens = ["".join(ch for ch in t.lower() if ch.isalnum())
                  for t in n.replace(",", " ").split()]
        tokens = [t for t in tokens if len(t) > 2]
        if tokens:
            # lexicographic tiebreak: plain max(key=len) keeps the FIRST
            # longest token, so equal-length first/last names key
            # differently per format ('Blanch, Darwin' -> blanch,
            # 'Darwin Blanch' -> darwin) and cross-book grouping forks.
            keys.add(max(tokens, key=lambda t: (len(t), t)))
    return frozenset(keys)


def _parse_iso(ts) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
        # Hard Rock ships epoch milliseconds (as a string) in eventTime;
        # fromisoformat would misparse "1784022000000" as a year-1784 date
        # and blow every grouping window.
        val = float(ts)
        if val > 1e11:      # ms vs s
            val /= 1000.0
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:          # some sources ship naive timestamps
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _convert_bookmaker(bm: dict, wanted_markets: Optional[Set[str]]) -> Optional[dict]:
    """OI bookmaker entry -> TheOddsAPI-shaped entry.

    Copies market-level maxRiskStake down to per-outcome bet_limit and maps
    event_link -> link. Suspended markets and (when a market filter is
    given) unrequested market keys are dropped. Returns None if nothing
    survives the filter.
    """
    markets_out = []
    for market in bm.get("markets", []):
        if market.get("suspended"):
            continue
        mkey = market.get("key")
        if wanted_markets is not None and mkey not in wanted_markets:
            continue
        limit = next((l.get("amount") for l in market.get("limits") or []
                      if l.get("type") == "maxRiskStake"), None)
        outcomes = []
        for o in market.get("outcomes", []):
            oc = {"name": o.get("name"), "price": o.get("price")}
            if o.get("point") is not None:
                oc["point"] = o["point"]
            if limit is not None:
                oc["bet_limit"] = limit
            outcomes.append(oc)
        if outcomes:
            markets_out.append({
                "key": mkey,
                "last_update": market.get("last_update"),
                "outcomes": outcomes,
            })
    if not markets_out:
        return None
    entry = {
        "key": bm.get("key", ""),
        "title": bm.get("title") or bm.get("key", "?"),
        "markets": markets_out,
    }
    if bm.get("event_link"):
        entry["link"] = bm["event_link"]
    return entry


def _merged_bookmakers(rec: dict, wanted_markets: Optional[Set[str]]) -> List[dict]:
    """Convert a slate record's raw bookmaker entries, ONE entry per
    canonical book with markets UNIONED across duplicates.

    Pinnacle publishes some events as multiple realtime records (main
    markets in one, game_spreads/game_totals or specials in another), and
    the same book also appears in both the realtime and unified feeds.
    First occurrence of a market key wins (realtime is ingested before
    unified, so the fresher copy is kept); later records only contribute
    market keys not yet present.
    """
    merged: Dict[str, dict] = {}
    order: List[str] = []
    for bm in rec.get("bookmakers", []):
        canon = _canon(bm.get("title") or bm.get("key", ""))
        entry = _convert_bookmaker(bm, wanted_markets)
        if entry is None:
            continue
        if canon not in merged:
            merged[canon] = entry
            order.append(canon)
            continue
        kept = merged[canon]
        have = {m["key"] for m in kept["markets"]}
        kept["markets"].extend(m for m in entry["markets"]
                               if m["key"] not in have)
        if "link" not in kept and entry.get("link"):
            kept["link"] = entry["link"]
    return [merged[c] for c in order]


def _rename_participants(event: dict, rec: dict) -> None:
    """Rewrite event/outcome participant names to the record's canonical
    form ('Kolar, Zdenek' -> 'Zdenek Kolar') so row labels match across
    books. Matching is by participant key (longest name token)."""
    ren = {}
    for ev_name in (event.get("home_team"), event.get("away_team")):
        if not ev_name:
            continue
        ek = _participant_key(ev_name)
        for rec_name in (rec.get("home_team"), rec.get("away_team")):
            if rec_name and ev_name != rec_name \
                    and ek == _participant_key(rec_name):
                ren[ev_name] = rec_name
    if not ren:
        return
    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            for o in market.get("outcomes", []):
                if o.get("name") in ren:
                    o["name"] = ren[o["name"]]


# ── v2 extra books (CRIS / Stake / Hard Rock) ──────────────────────────────
# Slow-moving sources fetched only on full (include_unified) cycles, never
# on the fast realtime tick. Each converter turns the book's native payload
# into TheOddsAPI-shaped events and feeds them through slate.add_event, so
# grouping, name canonicalization, and market-union all apply unchanged.

CRIS_MAX_LEAGUES = 40      # soccer lists 80+ boards; cap the fan-out

_WIRE_FRACTIONS = {"½": ".5", "¼": ".25", "¾": ".75"}


def _parse_american(s) -> Optional[int]:
    try:
        return int(str(s).replace("+", "").strip())
    except (ValueError, TypeError):
        return None


def _dec_to_american(d) -> Optional[int]:
    try:
        d = float(d)
    except (ValueError, TypeError):
        return None
    if d <= 1.001:
        return None
    return int(round((d - 1) * 100)) if d >= 2 else int(round(-100 / (d - 1)))


def _decode_root(ladder: Dict[int, int], idx) -> Optional[int]:
    """Hard Rock rootIdx -> American odds (exact lookup + interpolation)."""
    if idx in ladder:
        return ladder[idx]
    keys = sorted(ladder)
    if not keys:
        return None
    if idx <= keys[0]:
        return ladder[keys[0]]
    if idx >= keys[-1]:
        return ladder[keys[-1]]
    lo = max(k for k in keys if k < idx)
    hi = min(k for k in keys if k > idx)
    raw = ladder[lo] + (idx - lo) / (hi - lo) * (ladder[hi] - ladder[lo])
    if -100 < raw < 100:
        raw = 100 if raw > 0 else -100
    return int(round(raw))


def _split_matchup(name: str) -> Optional[tuple]:
    parts = re.split(r"\s+vs\.?\s+", name or "", flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


async def _ingest_cris(client: "OwlsInsightClient", oi_sport: str,
                       slate: "OISlate", log) -> None:
    """Bookmaker.eu game boards -> h2h only (spread/total lines ship
    unpriced upstream). One request per league board, capped."""
    slug = V2_SPORT_SLUGS["bookmaker"].get(oi_sport)
    if not slug:
        return
    leagues = await client.v2_leagues("bookmaker", slug)
    keys = [l.get("leagueKey") for l in leagues.get("leagues", [])
            if l.get("leagueKey")][:CRIS_MAX_LEAGUES]

    async def one(key):
        try:
            return await client.v2_source("bookmaker", slug, league=key)
        except Exception as e:  # noqa: BLE001
            log(f"[OI] cris board {key} failed: {e}")
            return None

    for body in await asyncio.gather(*(one(k) for k in keys)):
        data = (body or {}).get("data") or {}
        items = data.values() if isinstance(data, dict) else data
        for m in items:
            if not isinstance(m, dict) or m.get("kind") != "game":
                continue
            v, h = m.get("visitor") or {}, m.get("home") or {}
            away, home = v.get("team", ""), h.get("team", "")
            if not away or not home:
                continue
            outcomes = []
            for side, name in ((v, away), (h, home)):
                price = _parse_american(side.get("moneyline"))
                if price is not None:
                    outcomes.append({"name": name, "price": price})
            draw = m.get("draw")
            if isinstance(draw, dict):
                price = _parse_american(draw.get("moneyline"))
                if price is not None:
                    outcomes.append({"name": "Draw", "price": price})
            if len(outcomes) < 2:
                continue
            slate.add_event("cris", {
                "home_team": home, "away_team": away,
                "commence_time": None,       # wire format is "7/14 8:00am PT"
                "league": m.get("section"),
                "bookmakers": [{"key": "cris", "title": "CRIS",
                                "markets": [{"key": "h2h",
                                             "outcomes": outcomes}]}]})


_STAKE_H2H_NAMES = {"winner", "match winner", "1x2", "match result"}


async def _ingest_stake(client: "OwlsInsightClient", oi_sport: str,
                        slate: "OISlate", log) -> None:
    """Stake.com boards -> h2h (decimal odds converted to American)."""
    slug = V2_SPORT_SLUGS["stake"].get(oi_sport)
    if not slug:
        return
    body = await client.v2_source("stake", slug)
    for item in body.get("data") or []:
        teams = _split_matchup(item.get("name", ""))
        if teams is None:
            continue
        away, home = teams
        for market in item.get("markets", []):
            if (market.get("name") or "").strip().lower() not in _STAKE_H2H_NAMES:
                continue
            outs = []
            for o in market.get("outcomes", []):
                if o.get("active") is False:
                    continue
                price = _dec_to_american(o.get("odds"))
                if price is not None:
                    outs.append({"name": o.get("name", ""), "price": price})
            if len(outs) >= 2:
                slate.add_event("stake", {
                    "home_team": home, "away_team": away,
                    "commence_time": None,
                    "bookmakers": [{"key": "stake", "title": "Stake",
                                    "markets": [{"key": "h2h",
                                                 "outcomes": outs}]}]})
            break


# Hard Rock market-name -> our market key. Multiple markets mapping to the
# same key (alt lines, per-set variants) is fine: each outcome renders as
# its own row keyed by point.
_HR_MARKET_MAP = {
    "to win": "h2h", "match winner": "h2h", "winner": "h2h",
    "moneyline": "h2h", "match betting": "h2h",
    "total sets": "totals", "set spread": "spreads",
    "total games": "total_games", "total games spread": "game_spreads",
}


def _hr_leaf_selections(market: dict):
    for sel in market.get("selections") or []:
        if not isinstance(sel, dict):
            continue
        if sel.get("rootIdx") is None and sel.get("selections"):
            yield from (s for s in sel["selections"] if isinstance(s, dict))
        else:
            yield sel


async def _ingest_hardrock(client: "OwlsInsightClient", oi_sport: str,
                           slate: "OISlate", log) -> None:
    """Hard Rock (FL) -> h2h / totals / spreads / game markets, rootIdx
    decoded to American via the ladder. League-bucketed big sports are
    skipped for now (tennis/MMA return the full board in one call)."""
    slug = V2_SPORT_SLUGS["hardrock"].get(oi_sport)
    if not slug or slug in HARDROCK_LEAGUE_REQUIRED:
        return
    body = await client.v2_source("hardrock", slug, state="fl")
    ladder = await client.hardrock_ladder()
    for ev in body.get("data") or []:
        teams = _split_matchup(ev.get("name", ""))
        if teams is None:
            parts = ev.get("participants") or []
            if len(parts) < 2:
                continue
            teams = (parts[0].get("name", ""), parts[1].get("name", ""))
        away, home = teams
        markets_out = []
        for market in ev.get("markets", []):
            key = _HR_MARKET_MAP.get((market.get("name") or "").strip().lower())
            if key is None or market.get("state") not in (None, "OPEN"):
                continue
            outs = []
            for sel in _hr_leaf_selections(market):
                if sel.get("state") not in (None, "ACTIVE"):
                    continue
                price = _decode_root(ladder, sel.get("rootIdx")) \
                    if sel.get("rootIdx") is not None else None
                if price is None:
                    continue
                name = (sel.get("name") or sel.get("type") or "").strip()
                out = {"name": name, "price": price}
                if key != "h2h":
                    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", name)
                    if m:
                        out["point"] = float(m.group(1))
                        out["name"] = name[:m.start()].strip()
                outs.append(out)
            if len(outs) >= 2:
                markets_out.append({"key": key, "outcomes": outs})
        if markets_out:
            start = _parse_iso(ev.get("eventTime"))   # epoch-ms wire format
            slate.add_event("hardrock", {
                "home_team": home, "away_team": away,
                "commence_time": start.isoformat() if start else None,
                "isLive": ev.get("inplay"),
                "bookmakers": [{"key": "hardrock", "title": "Hard Rock",
                                "markets": markets_out}]})


async def _ingest_v2_books(client: "OwlsInsightClient", oi_sport: str,
                           slate: "OISlate", log) -> None:
    """CRIS + Stake + Hard Rock, concurrently, each fail-soft."""
    async def guarded(coro, name):
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            log(f"[OI] {name} ingest failed: {e}")
    await asyncio.gather(
        guarded(_ingest_cris(client, oi_sport, slate, log), "cris"),
        guarded(_ingest_stake(client, oi_sport, slate, log), "stake"),
        guarded(_ingest_hardrock(client, oi_sport, slate, log), "hardrock"))


def _group_window(oi_sport: str) -> float:
    """Doubleheader sports need the tight window; everyone else gets the
    wide one so live-restamped commence_times still group."""
    return (OISlate.GROUP_WINDOW_DOUBLEHEADER_S if oi_sport == "mlb"
            else OISlate.GROUP_WINDOW_S)


class OISlate:
    """One sport's OI unified odds, indexed for per-game merging."""

    # Books disagree on commence_time for the same match — listing lag,
    # reschedules, books restamping a live match's start to "now", and
    # DraftKings tennis shipping feed-write timestamps as commence_time
    # (observed 13h+ off the true start). A rematch of the same pair needs
    # a different round, which is never inside 36h, so the default window
    # is very generous. Baseball is the exception: doubleheaders are the
    # same teams ~4-9h apart, so MLB callers pass the tight window.
    GROUP_WINDOW_S = 36 * 3600
    GROUP_WINDOW_DOUBLEHEADER_S = 3 * 3600

    def __init__(self, wanted_markets: Optional[Set[str]] = None,
                 group_window_s: Optional[float] = None):
        self.wanted_markets = wanted_markets
        self.group_window_s = group_window_s or self.GROUP_WINDOW_S
        # exact team-name key -> [event dicts]
        self._by_teams: Dict[frozenset, List[dict]] = {}
        # surname fallback key -> [event dicts]
        self._by_surnames: Dict[frozenset, List[dict]] = {}
        self.books_available: Set[str] = set()

    def add_event(self, book_key: str, event: dict) -> None:
        # v1 data arrives keyed by book: the same event appears once per
        # book. Group per logical event so merge sees all books at once.
        home, away = event.get("home_team", ""), event.get("away_team", "")
        tkey = _team_key(home, away)
        bucket = self._by_teams.setdefault(tkey, [])
        when = _parse_iso(event.get("commence_time"))

        def pick(cands):
            for candidate in cands:
                have = _parse_iso(candidate["commence_time"])
                if when is None or have is None:
                    return candidate
                if abs((have - when).total_seconds()) <= self.group_window_s:
                    return candidate
            return None

        rec = pick(bucket)
        if rec is None:
            # Cross-format fallback: v2 books name players "Kolar, Zdenek"
            # where the v1 feeds say "Zdenek Kolar" — same participant key.
            rec = pick(self._by_surnames.get(_participant_key(home, away), []))
            if rec is not None:
                bucket.append(rec)   # alias this name format to the record
        if rec is None:
            rec = {"home_team": home, "away_team": away,
                   "commence_time": event.get("commence_time"),
                   "league": event.get("league"),
                   "is_live": bool(event.get("isLive")),
                   "bookmakers": []}
            bucket.append(rec)
            self._by_surnames.setdefault(
                _participant_key(home, away), []).append(rec)
        if event.get("league") and not rec.get("league"):
            rec["league"] = event["league"]
        if event.get("isLive"):
            rec["is_live"] = True
        # Rewrite this book's participant names to the record's canonical
        # form so grid row labels ("... | Moneyline: Zdenek Kolar") line up
        # across books instead of forking per name format.
        _rename_participants(event, rec)
        for bm in event.get("bookmakers", []):
            rec["bookmakers"].append(bm)
            self.books_available.add(_canon(bm.get("title") or book_key))

    def find(self, home: str, away: str,
             commence_time: Optional[str]) -> Optional[dict]:
        candidates = self._by_teams.get(_team_key(home, away))
        if not candidates:
            skey = _participant_key(home, away)
            candidates = [rec for k, recs in self._by_surnames.items()
                          if len(k & skey) >= min(2, len(skey) or 1)
                          for rec in recs]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # doubleheaders / reschedules: closest start time wins
        want = _parse_iso(commence_time)
        if want is None:
            return candidates[0]

        def distance(rec):
            have = _parse_iso(rec.get("commence_time"))
            return (abs((have - want).total_seconds())
                    if have else float("inf"))
        return min(candidates, key=distance)


async def fetch_oi_slate(session: aiohttp.ClientSession,
                         theoddsapi_sport_key: str,
                         wanted_markets: Optional[Set[str]] = None,
                         log=print) -> Optional[OISlate]:
    """One OI unified-odds request for the sport; returns an indexed slate,
    or None when the sport is uncovered or the request fails (fail-soft —
    the caller's TheOddsAPI flow proceeds unchanged)."""
    oi_sport = map_sport_key(theoddsapi_sport_key)
    if oi_sport is None:
        return None
    slate = OISlate(wanted_markets, group_window_s=_group_window(oi_sport))
    client = OwlsInsightClient(session)
    # Realtime feed FIRST: within an event record the first entry per
    # canonical book wins, so Pinnacle resolves to the realtime MQTT copy
    # (seconds-fresh, limits on every market) rather than the polled
    # unified copy. TheOddsAPI's own Pinnacle is delayed — see
    # PREFER_OI_BOOKS in merge_oi_books.
    if oi_sport in REALTIME_SPORTS:
        try:
            body = await client.get_realtime(oi_sport)
            for event in body.get("data", []):
                slate.add_event("pinnacle", event)
        except (OwlsInsightError, Exception) as e:  # noqa: BLE001 — fail-soft
            log(f"[OI] realtime fetch failed for {oi_sport}: {e}")
    try:
        body = await client.get_odds(oi_sport)
        for book_key, events in (body.get("data") or {}).items():
            for event in events or []:
                slate.add_event(book_key, event)
    except (OwlsInsightError, Exception) as e:  # noqa: BLE001 — fail-soft
        log(f"[OI] slate fetch failed for {oi_sport}: {e}")
    if not slate._by_teams:
        return None
    return slate


async def fetch_oi_games(session: aiohttp.ClientSession,
                         oi_pseudo_key: str,
                         wanted_markets: Optional[Set[str]] = None,
                         include_unified: bool = True,
                         sort_mode: str = "live",
                         log=print) -> List[dict]:
    """Build a full games list for an 'oi:' query slot, entirely from OI.

    Fetches the Pinnacle realtime feed FIRST (full market depth + limits;
    first entry per canonical book wins at synthesis time) and the unified
    v1 odds second (adds FanDuel/DraftKings/Novig/... where covered).
    Either feed failing is tolerated; both failing returns [].

    Returns TheOddsAPI-shaped event dicts, ready for the refresh_data row
    builder: {id, home_team, away_team, commence_time, league, bookmakers}.
    Sorted by (league, commence_time) so tournaments group in the grid.
    """
    oi_sport = oi_pseudo_key.split(":", 1)[1]
    slate = OISlate(wanted_markets, group_window_s=_group_window(oi_sport))
    client = OwlsInsightClient(session)
    if oi_sport in REALTIME_SPORTS:  # esports realtime needs ?league — skip
        try:
            body = await client.get_realtime(oi_sport)
            for event in body.get("data", []):
                slate.add_event("pinnacle", event)
        except (OwlsInsightError, Exception) as e:  # noqa: BLE001
            log(f"[OI] realtime fetch failed for {oi_sport}: {e}")
    # include_unified=False is the fast lane (in-table tick / Screen view):
    # realtime-only cycles between slower full refreshes. Full cycles also
    # pull the v2 extra books (CRIS/Stake/Hard Rock), which move slowly
    # upstream and would waste the tick budget. Realtime-only sports
    # (cricket/darts/handball) have no unified endpoint at all.
    if include_unified and oi_sport in V1_SPORTS:
        try:
            body = await client.get_odds(oi_sport)
            for book_key, events in (body.get("data") or {}).items():
                for event in events or []:
                    slate.add_event(book_key, event)
        except (OwlsInsightError, Exception) as e:  # noqa: BLE001
            log(f"[OI] unified fetch failed for {oi_sport}: {e}")
        await _ingest_v2_books(client, oi_sport, slate, log)

    games: List[dict] = []
    emitted = set()   # cross-format aliasing puts one rec in several buckets
    for bucket in slate._by_teams.values():
        for rec in bucket:
            if id(rec) in emitted:
                continue
            emitted.add(id(rec))
            bookmakers = _merged_bookmakers(rec, wanted_markets)
            if not bookmakers:
                continue
            date = (rec.get("commence_time") or "")[:10]
            games.append({
                "id": f"oi:{rec['away_team']}@{rec['home_team']}:{date}",
                "sport_key": oi_pseudo_key,
                "home_team": rec["home_team"],
                "away_team": rec["away_team"],
                "commence_time": rec.get("commence_time"),
                "league": rec.get("league"),
                "is_live": rec.get("is_live", False),
                "bookmakers": bookmakers,
            })
    sort_oi_games(games, sort_mode)
    return games


def oi_game_sort_key(g: dict):
    """In-play first (feed isLive flag OR already-started by clock), then
    league, then start time. Used by the grid slate and the Screen view."""
    started = g.get("is_live") or False
    if not started:
        ct = _parse_iso(g.get("commence_time"))
        started = bool(ct and ct <= datetime.now(timezone.utc))
    return (0 if started else 1,
            g.get("league") or "~",
            g.get("commence_time") or "~")


def _max_pinnacle_limit(g: dict) -> float:
    mx = 0
    for bm in g.get("bookmakers", []):
        for market in bm.get("markets", []):
            for o in market.get("outcomes", []):
                limit = o.get("bet_limit") or 0
                if limit > mx:
                    mx = limit
    return mx


OI_SORT_MODES = (("Live first", "live"), ("Start time", "time"),
                 ("Pinnacle limit", "limit"), ("League", "league"))


def sort_oi_games(games: List[dict], mode: str = "live") -> None:
    """In-place sort for oi: tab slates. Modes: live (in-play first, then
    league/time), time (soonest first), limit (highest Pinnacle maxRiskStake
    first — limit height tracks how much Pinnacle trusts its number),
    league (alphabetical grouping)."""
    if mode == "time":
        games.sort(key=lambda g: (g.get("commence_time") or "~",
                                  g.get("league") or "~"))
    elif mode == "limit":
        games.sort(key=lambda g: (-_max_pinnacle_limit(g),)
                   + oi_game_sort_key(g))
    elif mode == "league":
        games.sort(key=lambda g: (g.get("league") or "~",
                                  g.get("commence_time") or "~"))
    else:
        games.sort(key=oi_game_sort_key)


# Books whose OI copy REPLACES an existing TheOddsAPI entry rather than
# being skipped. TheOddsAPI relays Pinnacle (incl. limits) with delay; OI's
# comes off the realtime MQTT feed. For every other overlapping book the
# TheOddsAPI copy is kept (append-only).
PREFER_OI_BOOKS = frozenset({"pinnacle"})


def merge_oi_books(odds: dict, slate: Optional[OISlate]) -> int:
    """Merge OI books into a TheOddsAPI event-odds dict, in place.

    Append-only for books already present — except PREFER_OI_BOOKS, whose
    existing (delayed) entry is replaced by the OI realtime copy. Returns
    the number of books added or replaced. Never raises.
    """
    if not slate or not isinstance(odds, dict):
        return 0
    try:
        rec = slate.find(odds.get("home_team", ""), odds.get("away_team", ""),
                         odds.get("commence_time"))
        if rec is None:
            return 0
        book_list = odds.setdefault("bookmakers", [])
        present = {_canon(bm.get("title", "")): idx
                   for idx, bm in enumerate(book_list)}
        touched = 0
        for entry in _merged_bookmakers(rec, slate.wanted_markets):
            canon = _canon(entry["title"])
            if canon in present and canon not in PREFER_OI_BOOKS:
                continue
            if canon in present:      # PREFER_OI_BOOKS: swap in place, keep
                old = book_list[present[canon]]   # the column title stable
                entry["title"] = old.get("title") or entry["title"]
                if "link" not in entry and old.get("link"):
                    entry["link"] = old["link"]
                book_list[present[canon]] = entry
            else:
                book_list.append(entry)
                present[canon] = len(book_list) - 1
            touched += 1
        return touched
    except Exception as e:  # noqa: BLE001 — never break the render loop
        print(f"[OI] merge failed for {odds.get('id', '?')}: {e}")
        return 0

# ===========================================================================
# SYNC WRAPPER — for scripts/CLI; UI code should use the async client
# ===========================================================================

class OwlsInsightSync:
    """Blocking convenience wrapper: each call spins an event loop.
    Do NOT use from the Qt thread — that's what the async client is for."""

    def __getattr__(self, name):
        async_method = getattr(OwlsInsightClient, name, None)
        if async_method is None or not asyncio.iscoroutinefunction(async_method):
            raise AttributeError(name)

        def call(*args, **kwargs):
            async def runner():
                async with aiohttp.ClientSession(
                        timeout=REQUEST_TIMEOUT) as session:
                    client = OwlsInsightClient(session)
                    return await getattr(client, name)(*args, **kwargs)
            return asyncio.run(runner())
        return call


# ===========================================================================
# CLI
# ===========================================================================

def _main() -> None:
    ap = argparse.ArgumentParser(description="Owls Insight API client")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("odds", help="unified v1 odds")
    p.add_argument("sport")
    p.add_argument("--books")
    p.add_argument("--league")
    p.add_argument("--alternates", action="store_true")

    p = sub.add_parser("realtime", help="Pinnacle realtime w/ limits")
    p.add_argument("sport")
    p.add_argument("--league")
    p.add_argument("--backup", action="store_true", help="PS3838 feed")

    p = sub.add_parser("ev", help="+EV value bets")
    p.add_argument("sport")
    p.add_argument("--min-ev", type=float)
    p.add_argument("--book")

    p = sub.add_parser("v2", help="per-book native source")
    p.add_argument("book")
    p.add_argument("sport")
    p.add_argument("--league")
    p.add_argument("--state", default="fl", help="hardrock: az|fl")

    p = sub.add_parser("v2-leagues", help="v2 league discovery")
    p.add_argument("book")
    p.add_argument("sport")
    p.add_argument("--state", default="fl")

    p = sub.add_parser("matrix", help="all books for a sport, concurrently")
    p.add_argument("sport")
    p.add_argument("--league", help="league hint substring (e.g. istanbul)")
    p.add_argument("--event", help="'Home/Away' filter, surname matching")

    p = sub.add_parser("ledger", help="limit-ledger sampler")
    p.add_argument("sports", nargs="+",
                   help=f"realtime sports: {', '.join(REALTIME_SPORTS)}")
    p.add_argument("--once", action="store_true", help="single snapshot")

    p = sub.add_parser("ledger-show", help="print limit history for an event")
    p.add_argument("event_id")

    args = ap.parse_args()
    sync = OwlsInsightSync()

    if args.cmd == "odds":
        books = args.books.split(",") if args.books else None
        out = sync.get_odds(args.sport, books=books, league=args.league,
                            alternates=args.alternates)
    elif args.cmd == "realtime":
        out = sync.get_realtime(args.sport, league=args.league,
                                backup_feed=args.backup)
    elif args.cmd == "ev":
        out = sync.get_ev(args.sport, min_ev=args.min_ev, book=args.book)
    elif args.cmd == "v2":
        out = sync.v2_source(args.book, args.sport, league=args.league,
                             state=args.state)
    elif args.cmd == "v2-leagues":
        out = sync.v2_leagues(args.book, args.sport, state=args.state)
    elif args.cmd == "matrix":
        async def _matrix():
            async with aiohttp.ClientSession(
                    timeout=REQUEST_TIMEOUT) as session:
                oi = OwlsInsightClient(session)
                m = await full_book_matrix(oi, args.sport,
                                           league_hint=args.league)
                if args.event:
                    home, _, away = args.event.partition("/")
                    return match_event_across_books(m, home, away)
                return m
        out = asyncio.run(_matrix())
    elif args.cmd == "ledger":
        asyncio.run(run_limit_ledger(args.sports, once=args.once))
        return
    elif args.cmd == "ledger-show":
        ledger = LimitLedger()
        for mkey, point, amount, seen in ledger.event_history(args.event_id):
            pt = f" @{point}" if point is not None else ""
            print(f"{seen}  {mkey:40s}{pt:8s} ${amount:,.0f}")
        ledger.close()
        return
    else:  # pragma: no cover
        ap.error("unknown command")
        return

    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    _main()
