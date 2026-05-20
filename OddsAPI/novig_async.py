#!/usr/bin/env python3
"""
Novig Exchange - Async Module

Mirrors prophetx_async.py: aiohttp-based versions of the read-only Novig
GraphQL queries needed for the LiquidityWidget dump pipeline. Re-uses the
GraphQL field fragments and dump-entry shaper from NovigClient.py so the
two paths stay schema-aligned.

The sync NovigClient remains the source of truth for orderbook (`/book/batch`)
and parlay quoting. This file only covers the listing + per-event-markets
queries that dominate the cold-start scrape.
"""

import asyncio
import json
import pathlib
import random
from datetime import datetime
from typing import Optional, Sequence

import aiohttp

from NovigClient import (
    NOVIG_GRAPHQL_URL,
    NOVIG_DUMP_DIR,
    NovigError,
    _EVENT_FIELDS,
    _GAME_FIELDS,
    _MARKET_FIELDS,
    _event_to_dump_entry,
)

try:
    from Creds import NOVIG_AUTH_TOKEN
except ImportError:
    NOVIG_AUTH_TOKEN = ""


REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)

DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB", "NBA", "NHL", "NFL", "NCAAF", "NCAAB", "WNBA", "EPL",
)


def _headers() -> dict:
    if not NOVIG_AUTH_TOKEN:
        raise NovigError(
            "No Novig bearer token. Set NOVIG_AUTH_TOKEN in Creds.py.")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {NOVIG_AUTH_TOKEN}",
        "Origin": "https://novig.com",
        "Referer": "https://novig.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) NovigClient/0.1-async",
    }


# ============================================================================
# ASYNC GRAPHQL
# ============================================================================

async def gql_async(session: aiohttp.ClientSession,
                    query: str,
                    variables: Optional[dict] = None,
                    operation_name: Optional[str] = None) -> dict:
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    if operation_name is not None:
        payload["operationName"] = operation_name
    try:
        async with session.post(NOVIG_GRAPHQL_URL, json=payload,
                                headers=_headers(),
                                timeout=REQUEST_TIMEOUT) as r:
            if r.status != 200:
                text = await r.text()
                raise NovigError(f"HTTP {r.status}: {text[:500]}")
            body = await r.json()
    except aiohttp.ClientError as e:
        raise NovigError(f"transport: {e}")
    if body.get("errors"):
        raise NovigError(
            f"GraphQL errors: {json.dumps(body['errors'])[:1000]}")
    return body.get("data", {})


async def list_events_async(session: aiohttp.ClientSession,
                            league: str,
                            status_in: tuple[str, ...] = (
                                "OPEN_PREGAME", "OPEN_INGAME"),
                            visible_only: bool = True,
                            limit: int = 200) -> list[dict]:
    where: dict = {
        "league": {"_eq": league},
        "status": {"_in": list(status_in)},
        "parent_event_id": {"_is_null": True},
    }
    if visible_only:
        where["_or"] = [
            {"is_visible_pregame": {"_eq": True}},
            {"is_visible_live": {"_eq": True}},
        ]
    query = f"""
    query ListEvents($where: event_bool_exp!, $limit: Int!) {{
      event(where: $where, limit: $limit,
            order_by: {{scheduled_start: asc}}) {{
        {_EVENT_FIELDS}
        game {{ {_GAME_FIELDS} }}
      }}
    }}
    """
    data = await gql_async(session, query,
                           {"where": where, "limit": limit},
                           operation_name="ListEvents")
    return data.get("event", [])


async def get_event_markets_async(session: aiohttp.ClientSession,
                                  event_id: str,
                                  only_available: bool = False,
                                  tree_depth: int = 3
                                  ) -> Optional[dict]:
    """Async port of NovigQueries.get_event_markets. Walks sub-events to
    `tree_depth` levels so periods / props / SGP groups are included in
    one round-trip."""
    if only_available:
        market_where = {
            "_and": [
                {"status": {"_eq": "OPEN"}},
                {"_or": [
                    {"is_consensus": {"_eq": True}},
                    {"outcomes": {"available": {"_is_null": False}}},
                ]},
            ],
        }
    else:
        market_where: dict = {}

    def _nested(depth: int) -> str:
        if depth <= 0:
            return ""
        return f"""
        events {{
          {_EVENT_FIELDS}
          markets(where: $where) {{ {_MARKET_FIELDS} }}
          {_nested(depth - 1)}
        }}
        """

    query = f"""
    query EventMarkets($eventId: uuid!, $where: market_bool_exp!) {{
      event(where: {{id: {{_eq: $eventId}}}}) {{
        {_EVENT_FIELDS}
        game {{ {_GAME_FIELDS} }}
        markets(where: $where) {{ {_MARKET_FIELDS} }}
        {_nested(tree_depth)}
      }}
    }}
    """
    data = await gql_async(session, query,
                           {"eventId": event_id, "where": market_where},
                           operation_name="EventMarkets")
    events = data.get("event", [])
    return events[0] if events else None


# ============================================================================
# FULL-SLATE SCRAPE
# ============================================================================

async def FetchAllLeaguesAsync(*,
                               leagues: Sequence[str] = DEFAULT_LEAGUES,
                               max_concurrent: int = 10,
                               only_available: bool = False,
                               save: bool = True,
                               dump_dir: Optional[pathlib.Path] = None,
                               progress: bool = True) -> dict:
    """Concurrent multi-league scrape.

    Output shape is identical to NovigQueries.scrape_all_leagues (and
    ProphetX's all_markets_combined dump), so LiquidityWidget's loader
    consumes it unchanged.
    """
    dump_dir = dump_dir or NOVIG_DUMP_DIR
    semaphore = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        # Step 1: list events across all leagues (concurrent across leagues,
        # 2 status filters each).
        if progress:
            print(f"[novig.async] listing events for {len(leagues)} leagues...")

        list_tasks = []
        for lg in leagues:
            list_tasks.append(list_events_async(
                session, lg, status_in=("OPEN_PREGAME",), limit=200))
            list_tasks.append(list_events_async(
                session, lg, status_in=("OPEN_INGAME",), limit=100))
        list_results = await asyncio.gather(*list_tasks, return_exceptions=True)

        events: list[dict] = []
        for res in list_results:
            if isinstance(res, Exception):
                if progress:
                    print(f"  list error: {res}")
                continue
            events.extend(res)

        # Dedup by id (parent events can show up under multiple status
        # buckets if Novig flips them mid-listing).
        seen: set[str] = set()
        unique: list[dict] = []
        for ev in events:
            eid = ev.get("id")
            if eid and eid not in seen:
                seen.add(eid)
                unique.append(ev)

        if progress:
            print(f"[novig.async] {len(unique)} unique events "
                  f"(max_concurrent={max_concurrent})")

        # Step 2: fetch markets for each event under semaphore.
        async def fetch_with_sem(ev: dict) -> tuple[str, Optional[dict]]:
            async with semaphore:
                try:
                    node = await get_event_markets_async(
                        session, ev["id"], only_available=only_available)
                except NovigError as e:
                    if progress:
                        print(f"  {ev.get('description')}: {e}")
                    return ev["id"], None
                if not node:
                    return ev["id"], None
                return ev["id"], _event_to_dump_entry(node)

        tasks = [fetch_with_sem(ev) for ev in unique]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        dump: dict[str, dict] = {}
        ok = 0
        for i, res in enumerate(results, 1):
            if isinstance(res, Exception):
                if progress:
                    print(f"  [{i}/{len(unique)}] exception: {res}")
                continue
            eid, entry = res
            if entry is None:
                continue
            dump[eid] = entry
            ok += 1
            if progress:
                n = len((entry.get("data") or {}).get("markets") or [])
                name = entry["event_metadata"].get("name", "")[:30]
                print(f"  [{i}/{len(unique)}] {name:30s} ({n} markets)")

    if progress:
        print(f"[novig.async] {ok}/{len(unique)} events scraped")

    if save and dump:
        # Write in executor to keep the event loop responsive on big dumps.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _save_dump_sync, dump, dump_dir, progress)

    return dump


def _save_dump_sync(dump: dict, dump_dir: pathlib.Path,
                    progress: bool = True) -> None:
    dump_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = dump_dir / f"all_events_combined_{ts}.json"
    out.write_text(json.dumps(dump, indent=2, default=str))
    if progress:
        size_mb = out.stat().st_size / (1024 * 1024)
        print(f"[novig.async] wrote {out}  ({size_mb:.2f} MB)")


if __name__ == "__main__":
    # CLI smoke test: scrape one league and print event count.
    import sys
    leagues = tuple(sys.argv[1:]) if len(sys.argv) > 1 else ("MLB",)
    dump = asyncio.run(FetchAllLeaguesAsync(
        leagues=leagues, save=False, progress=True))
    print(f"DONE: {len(dump)} events scraped")
