#!/usr/bin/env python3
"""
Novig Exchange - Async Module

Mirrors prophetx_async.py: aiohttp-based versions of the read-only Novig
GraphQL queries needed for the LiquidityWidget dump pipeline. Re-uses the
GraphQL field fragments and dump-entry shaper from NovigClient.py so the
two paths stay schema-aligned.

The sync NovigClient remains the source of truth for the orderbook
(`/book/batch`). This file covers the async listing + per-event-markets
queries plus the async parlay pricer and SGP implication scanner.
"""

import asyncio
import json
import pathlib
import random
from datetime import datetime
from typing import Callable, Optional, Sequence

import aiohttp

from NovigClient import (
    NOVIG_GRAPHQL_URL,
    NOVIG_NBX_BASE,
    NOVIG_DUMP_DIR,
    NovigError,
    NovigQueries,
    SGP_IMPLICATION_PAIRS,
    _EVENT_FIELDS,
    _GAME_FIELDS,
    _MARKET_FIELDS,
    _event_to_dump_entry,
    _find_outcome,
    fmt_american,
    summarize_parlay_quote,
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


# ============================================================================
# ASYNC NBX PARLAY PRICER + SGP IMPLICATION SCANNER
# ============================================================================
# Async ports of NovigClient.price_parlay and scan_sgp_implications. The sync
# versions block on `requests` / a ThreadPoolExecutor; these run as coroutines
# on the caller's event loop, so the scanner can sit alongside the rest of the
# widget's qasync work without a dedicated QThread. Every pure helper (task
# building, _find_outcome, summarize_parlay_quote, the result-row dict) is
# reused verbatim from NovigClient — only the I/O is swapped for aiohttp, so
# the two paths stay behaviourally identical.


async def price_parlay_async(session: aiohttp.ClientSession,
                             outcome_ids: list[str],
                             boost_id: Optional[str] = None) -> dict:
    """Async port of NovigClient.price_parlay.

    Quote an SGP / parlay via the NBX parlay pricer. Read-only — returns the
    quote object the site would offer; does NOT place a wager. Raises
    NovigError on transport failure or a non-2xx response.
    """
    url = f"{NOVIG_NBX_BASE}/parlay/request"
    payload = {
        "outcomes": [{"id": oid} for oid in outcome_ids],
        "boostId": boost_id,
    }
    try:
        async with session.post(url, json=payload, headers=_headers(),
                                timeout=REQUEST_TIMEOUT) as r:
            # Server returns 201 Created with the quote wrapped in a list.
            if r.status not in (200, 201):
                text = await r.text()
                raise NovigError(f"NBX HTTP {r.status}: {text[:500]}")
            body = await r.json()
    except aiohttp.ClientError as e:
        raise NovigError(f"transport: {e}")
    if isinstance(body, list):
        if not body:
            raise NovigError("Empty parlay quote response")
        return body[0]
    return body


# App version + screen string mirror what the Novig web app sends. They look
# like analytics fields, but the backend rejects executions missing them. Pin
# to the version captured at implementation time; bump if Novig changes their
# wire contract.
_NOVIG_APP_VERSION = "1.1.194"
_NOVIG_DEFAULT_SCREEN = "Home - Baseball"


async def place_parlay_async(session: aiohttp.ClientSession,
                             saved_parlay_id: str,
                             wager: float,
                             *,
                             currency: str = "CASH",
                             current_screen: str = _NOVIG_DEFAULT_SCREEN,
                             ) -> dict:
    """Execute a previously-quoted Novig parlay.

    Two-step flow (mirrors the web app):
      1. price_parlay_async returns a quote with `id`
      2. place_parlay_async takes that id as `saved_parlay_id`, plus the
         actual stake, and POSTs to /nbx/v1/parlay/execute

    On a Filled order the response includes the real `wager`, `price`,
    settled wallet balance, and per-leg outcomes. Raises NovigError on
    transport failure or any non-2xx response.
    """
    url = f"{NOVIG_NBX_BASE}/parlay/execute"
    payload = {
        "savedParlayId": saved_parlay_id,
        "wager": f"{wager:.5f}",
        "currency": currency,
        "partnerId": None,
        "appVersion": _NOVIG_APP_VERSION,
        "featuredParlayId": None,
        "currentScreen": current_screen,
    }
    try:
        async with session.post(url, json=payload, headers=_headers(),
                                timeout=REQUEST_TIMEOUT) as r:
            if r.status not in (200, 201):
                text = await r.text()
                raise NovigError(f"NBX execute HTTP {r.status}: {text[:500]}")
            body = await r.json()
    except aiohttp.ClientError as e:
        raise NovigError(f"transport: {e}")
    _harvest_wallet_id(body)
    if isinstance(body, dict):
        # Parlay execute also nests {trader: {wallets: [...]}} and
        # {wallet: {id: ...}} — let the harvester walk the obvious slot.
        _harvest_wallet_id(body.get("trader") or {})
    return body


# Cached walletId, populated opportunistically from any response that
# includes it (parlay execute, /orders, /wallets fetch). Lets single-bet
# placement work without forcing the user to copy a UUID into Creds.py.
_NOVIG_WALLET_ID_CACHE: Optional[str] = None


def _harvest_wallet_id(payload) -> None:
    """Walk a Novig response and stash any walletId we see. Cheap; runs
    on every success path."""
    global _NOVIG_WALLET_ID_CACHE
    if _NOVIG_WALLET_ID_CACHE:
        return
    if not isinstance(payload, dict):
        return
    wid = payload.get("walletId")
    if isinstance(wid, str) and wid:
        _NOVIG_WALLET_ID_CACHE = wid
        return
    wallet = payload.get("wallet")
    if isinstance(wallet, dict):
        inner = wallet.get("id")
        if isinstance(inner, str) and inner:
            _NOVIG_WALLET_ID_CACHE = inner


def get_cached_wallet_id() -> Optional[str]:
    """Return the harvested walletId, or None if we haven't seen one yet.
    Callers needing it for single-bet placement either fetch via a parlay
    quote first or pull NOVIG_WALLET_ID out of Creds.py."""
    if _NOVIG_WALLET_ID_CACHE:
        return _NOVIG_WALLET_ID_CACHE
    try:
        from Creds import NOVIG_WALLET_ID as _wid
        return _wid or None
    except ImportError:
        return None


def _novig_geo_token() -> Optional[str]:
    """Pull the latest geolocation transaction id from Creds.py. Novig
    rotates these via a third-party SDK on the site; there's no clean way
    to refresh them headlessly, so the user grabs one from DevTools and
    pastes it into Creds.NOVIG_GEO_TX_ID periodically."""
    try:
        from Creds import NOVIG_GEO_TX_ID as _g
        return _g or None
    except ImportError:
        return None


async def place_order_async(session: aiohttp.ClientSession,
                            market_id: str,
                            outcome_id: str,
                            price: float,
                            qty_centi: int,
                            *,
                            is_bid: bool = True,
                            wallet_id: Optional[str] = None,
                            geo_tx_id: Optional[str] = None,
                            tif: str = "IOC",
                            currency: str = "CASH") -> dict:
    """Place a single order against the Novig NBX orderbook.

    Args:
        market_id: target market UUID
        outcome_id: side being taken (Yes/No, Team-A/Team-B, Over/Under, ...)
        price: decimal probability (0 < p < 1) — matches the row's price
        qty_centi: qty in centi-contracts. For a $stake-dollar bet at price p,
            use round((stake / p) * 100). At fill the trader pays
            qty_centi/100 * price dollars.
        is_bid: True buys the outcome; False sells (only matters for binary
            markets where you take the opposite side).
        wallet_id: explicit walletId; falls back to harvested cache / Creds.
        geo_tx_id: explicit geolocation transaction id; falls back to Creds.
        tif: time-in-force. IOC = immediate-or-cancel (market take, default);
            GTC keeps the order resting on the book.

    Returns the parsed order object. Raises NovigError on transport failure,
    missing wallet/geo credentials, or any non-2xx response.
    """
    import uuid
    wid = wallet_id or get_cached_wallet_id()
    if not wid:
        raise NovigError(
            "No Novig walletId available. Place a parlay first (harvests the "
            "id) or set NOVIG_WALLET_ID in Creds.py.")
    gtx = geo_tx_id or _novig_geo_token()
    if not gtx:
        raise NovigError(
            "No Novig geolocationTransactionId. Grab the latest from a live "
            "/orders request in DevTools and set NOVIG_GEO_TX_ID in Creds.py.")

    url = f"{NOVIG_NBX_BASE}/orders"
    # uuid4 instead of uuid7 — server doesn't appear to enforce v7, and the
    # stdlib lacks uuid7 prior to 3.13. Order id is idempotency token only.
    payload = {
        "id": str(uuid.uuid4()),
        "appVersion": _NOVIG_APP_VERSION,
        "currency": currency,
        "entryPromotionClaimId": None,
        "geolocationTransactionId": gtx,
        "isBid": bool(is_bid),
        "marketId": market_id,
        "outcomeId": outcome_id,
        "partnerId": None,
        "price": float(price),
        "qty": int(qty_centi),
        "tif": tif,
        "timeToLiveMs": None,
        "type": "PLACE",
        "walletId": wid,
    }
    try:
        async with session.post(url, json=payload, headers=_headers(),
                                timeout=REQUEST_TIMEOUT) as r:
            text = await r.text()
            if r.status not in (200, 201):
                raise NovigError(f"NBX orders HTTP {r.status}: {text[:500]}")
            body = json.loads(text) if text else {}
    except aiohttp.ClientError as e:
        raise NovigError(f"transport: {e}")
    _harvest_wallet_id(body)
    return body


def stake_to_qty_centi(stake_usd: float, price: float) -> int:
    """Translate a dollar stake at a given decimal-probability price into
    the centi-contract qty Novig's orders endpoint expects.

    At fill, the trader pays (qty/100)*price dollars and wins (qty/100)
    dollars on a winning outcome. So for a target stake s at price p:
        qty_centi = round((s / p) * 100)
    """
    if price <= 0:
        raise ValueError("price must be > 0")
    return max(1, round((stake_usd / price) * 100))


async def scan_sgp_implications_async(
        session: aiohttp.ClientSession,
        event_id: str,
        *,
        pairs: Optional[list] = None,
        strike: float = 0.5,
        concurrency: int = 4,
        throttle_s: float = 0.25,
        jitter_s: float = 0.20,
        progress_cb: Optional[Callable[[int, int], None]] = None
        ) -> list[dict]:
    """Async port of NovigClient.scan_sgp_implications, for ONE event.

    Quotes every (dominant, implied) SGP pair per player and flags rows where
    the SWISH pricer prices the parlay below the deterministic implication
    floor — i.e. combined < P(dominant leg), which means the parlay strictly
    dominates the standalone dominant wager.

    Concurrency is an asyncio.Semaphore: at most `concurrency` quotes are
    in flight, and each holds its slot through a `throttle_s + uniform(0,
    jitter_s)` cooldown, so the aggregate request rate stays in
    human-browsing territory (the same throttle the sync scanner applies).

    progress_cb, when supplied, is called progress_cb(done, total): once with
    (0, total) before the first quote, then after each quote completes. It
    runs on the caller's event loop, so a Qt slot/signal is safe to use.

    Returns the result rows sorted by delta_vs_dominant (most negative — most
    mispriced — first), identical in shape to the sync scanner's output.
    """
    if pairs is None:
        pairs = SGP_IMPLICATION_PAIRS

    ev = await get_event_markets_async(session, event_id,
                                       only_available=False)
    if not ev:
        if progress_cb:
            progress_cb(0, 0)
        return []
    mkts = NovigQueries.flatten_markets(ev)

    # Index player-prop markets by playerId -> {market_type: market}, at the
    # target strike only (deterministic implications are all on the 0.5 line).
    by_player_type: dict = {}
    for m in mkts:
        pid = m.get("playerId")
        if not pid or m.get("strike") != strike:
            continue
        by_player_type.setdefault(pid, {})[m.get("type")] = m

    # Build the quote task list up front.
    tasks: list[dict] = []
    for _pid, type_map in by_player_type.items():
        for dom_t, dom_side, imp_t, imp_side in pairs:
            dom_mkt = type_map.get(dom_t)
            imp_mkt = type_map.get(imp_t)
            if not dom_mkt or not imp_mkt:
                continue
            dom_out = _find_outcome(dom_mkt, dom_side, strike)
            imp_out = _find_outcome(imp_mkt, imp_side, strike)
            if not dom_out or not imp_out:
                continue
            dom_p = dom_out.get("last") or dom_out.get("available")
            imp_p = imp_out.get("last") or imp_out.get("available")
            if dom_p is None or imp_p is None:
                continue
            try:
                dom_p = float(dom_p)
                imp_p = float(imp_p)
            except (TypeError, ValueError):
                continue
            tasks.append({
                "dom_t": dom_t, "dom_side": dom_side,
                "imp_t": imp_t, "imp_side": imp_side,
                "dom_mkt": dom_mkt, "dom_out": dom_out, "dom_p": dom_p,
                "imp_out": imp_out, "imp_p": imp_p,
            })

    if progress_cb:
        progress_cb(0, len(tasks))
    if not tasks:
        return []

    sem = asyncio.Semaphore(concurrency)

    async def _run(t: dict) -> Optional[dict]:
        async with sem:
            try:
                quote = await price_parlay_async(
                    session, [t["dom_out"]["id"], t["imp_out"]["id"]])
            except NovigError:
                return None
            finally:
                # Hold the semaphore slot through the cooldown so the
                # aggregate request rate stays bounded regardless of how
                # many quotes complete back-to-back.
                await asyncio.sleep(
                    throttle_s + random.uniform(0.0, jitter_s))
        s = summarize_parlay_quote(quote)
        combined = s["combined_price"]
        if combined is None:
            return None
        dom_p, imp_p = t["dom_p"], t["imp_p"]
        naive = dom_p * imp_p
        name = (t["dom_mkt"].get("description") or "").rsplit(
            f" {strike} ", 1)[0]
        return {
            "player": name,
            "dominant_type": t["dom_t"], "dominant_side": t["dom_side"],
            "implied_type": t["imp_t"], "implied_side": t["imp_side"],
            "dominant_price": dom_p, "implied_price": imp_p,
            "naive_price": naive, "combined_price": combined,
            "delta_vs_dominant": combined - dom_p,
            "delta_vs_naive": combined - naive,
            "american_dominant": fmt_american(dom_p),
            "american_implied": fmt_american(imp_p),
            "american_combined": fmt_american(combined),
            "mispriced": combined + 1e-6 < dom_p,
            "dominant_outcome_id": t["dom_out"]["id"],
            "implied_outcome_id": t["imp_out"]["id"],
        }

    results: list[dict] = []
    done = 0
    total = len(tasks)
    for fut in asyncio.as_completed([_run(t) for t in tasks]):
        r = await fut
        done += 1
        if progress_cb:
            progress_cb(done, total)
        if r is not None:
            results.append(r)
    results.sort(key=lambda row: row["delta_vs_dominant"])
    return results


if __name__ == "__main__":
    # CLI smoke test. Two modes:
    #   python novig_async.py                       -> scrape MLB events
    #   python novig_async.py NBA NHL                -> scrape those leagues
    #   python novig_async.py scan <event_id>        -> async SGP scan
    import sys
    args = sys.argv[1:]

    if args and args[0] == "scan":
        if len(args) < 2:
            print("usage: python novig_async.py scan <event_id>")
            sys.exit(1)
        scan_event_id = args[1]

        async def _scan_smoke() -> list[dict]:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as s:
                def _prog(done: int, total: int) -> None:
                    print(f"\r  quoting {done}/{total}", end="", flush=True)
                rows = await scan_sgp_implications_async(
                    s, scan_event_id, progress_cb=_prog)
                print()
                return rows

        scan_rows = asyncio.run(_scan_smoke())
        print(f"\n{len(scan_rows)} SGP pairs scanned "
              f"(sorted by delta_vs_dominant, most negative first)")
        for row in scan_rows:
            flag = "  <<< MISPRICED" if row["mispriced"] else ""
            print(f"  {row['player'][:22]:22s} "
                  f"{row['dominant_type'][:3]:3s}=>{row['implied_type'][:4]:4s} "
                  f"dom {row['american_dominant']:>6s} "
                  f"swish {row['american_combined']:>6s} "
                  f"vsDom {row['delta_vs_dominant']:>+8.4f}{flag}")
        sys.exit(0)

    leagues = tuple(args) if args else ("MLB",)
    dump = asyncio.run(FetchAllLeaguesAsync(
        leagues=leagues, save=False, progress=True))
    print(f"DONE: {len(dump)} events scraped")
