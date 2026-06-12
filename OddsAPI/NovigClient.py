#!/usr/bin/env python3
"""
Novig Exchange Client (read-only, exploratory)

Novig fronts a Hasura GraphQL API at https://api.novig.us/v1/graphql plus a
handful of REST endpoints (e.g. live-event-ticker). Auth is an Auth0 RS256
bearer JWT with Hasura claims (x-hasura-default-role=user).

Current scope:
  - Low-level POST to /v1/graphql with bearer auth.
  - Schema introspection -> novig_schema.json so subsequent selections can be
    built against the real field names instead of guessing from the captured
    fragments.

Once introspection is on disk, higher-level helpers (list events, fetch
markets) will be added on top.
"""

import asyncio
import json
import os
import pathlib
import random
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Optional

import requests
import orjson  # C-level JSON: ~6-10x faster than stdlib + eats bytes directly,
              # so the 38MB dump parse stops holding the GIL long enough to
              # stutter the ticker (see PERF_DIAGNOSTICS.md, [PERF-DIAG]).

try:
    from Creds import NOVIG_AUTH_TOKEN
except ImportError:
    NOVIG_AUTH_TOKEN = ""


NOVIG_GRAPHQL_URL = "https://api.novig.us/v1/graphql"
NOVIG_NBX_BASE = "https://api.novig.us/nbx/v1"

# Where scrape_all_mlb() writes dumps. Mirrors the layout of
# prophetx_dumps/ so the widget's stale-fallback machinery can consume
# Novig data uniformly.
NOVIG_DUMP_DIR = pathlib.Path(__file__).parent / "novig_dumps"

# Slim companion to all_events_combined_latest.json: per-event metadata only
# (no market/outcome subtrees). The 48MB full dump parses in ~122ms (GIL-held)
# but the startup match-map build only needs event identity to pair PX↔NV — the
# markets are 99% of the bytes and are needed only for the handful of events the
# user actually opens (lazy-hydrated then). This index is ~0.5MB / ~2ms to parse,
# so reading it instead of the full dump removes the startup stutter. The full
# dump is still written for the lazy per-event hydration path. (see
# PERF_DIAGNOSTICS.md / project memory on the LiquidityWidget dump parse)
NOVIG_EVENTS_INDEX_NAME = "events_index_latest.json"


def write_events_index(dump: dict, dump_dir: pathlib.Path) -> None:
    """Write the slim event-metadata index alongside the full combined dump.
    Mirrors the dump's {event_id: {...}} keying so a reader can pair PX↔NV
    on identity without materializing any market subtrees. Atomic temp+replace
    so a concurrent match-map read never sees a half-written file."""
    index = {eid: {"event_metadata": (entry or {}).get("event_metadata")}
             for eid, entry in dump.items()}
    dump_dir.mkdir(parents=True, exist_ok=True)
    out = dump_dir / NOVIG_EVENTS_INDEX_NAME
    tmp = dump_dir / (NOVIG_EVENTS_INDEX_NAME + ".tmp")
    tmp.write_text(json.dumps(index, default=str))
    tmp.replace(out)


# Per-event sidecar files: <dump_dir>/events/<event_id>.json, each holding one
# event's full dump entry (~100KB). The widget's lazy market hydration
# (_hydrateEventPairMarkets) only ever needs ONE event's entry, but used to get
# it by parsing the whole ~38MB combined dump on the main thread (~175ms
# GIL-held stall on first paired-event open). Reading the single sidecar is
# <1ms. The combined dump is still written for any whole-dump consumers.
NOVIG_EVENT_SIDECAR_DIRNAME = "events"


def write_event_sidecars(dump: dict, dump_dir: pathlib.Path) -> None:
    """Write each event's dump entry to its own sidecar file and prune
    sidecars for events no longer in the dump. Event ids are UUIDs, so
    they're filesystem-safe as-is. Atomic per-file temp+replace so a
    concurrent hydration read never sees a half-written entry."""
    side_dir = dump_dir / NOVIG_EVENT_SIDECAR_DIRNAME
    side_dir.mkdir(parents=True, exist_ok=True)
    for eid, entry in dump.items():
        out = side_dir / f"{eid}.json"
        tmp = side_dir / f"{eid}.json.tmp"
        tmp.write_text(json.dumps(entry, default=str))
        tmp.replace(out)
    keep = {f"{eid}.json" for eid in dump}
    for p in side_dir.glob("*.json"):
        if p.name not in keep:
            try:
                p.unlink()
            except OSError:
                pass

# qty values on NBX orderbook entries are in cents/centi-units. Display = qty/100.
# Verified against site UI:
#   COIN bid qty=950000 price=0.822 -> 9500 display, opposite takeable 1691 COIN
#   CASH bid qty= 95000 price=0.822 ->  950 display, opposite takeable 169.10 CASH
# CASH (blue chip, sweeps/redeemable) and COIN (yellow chip, play money) are
# separate orderbooks with independent liquidity.
NBX_QTY_SCALE = 100.0


class NovigError(RuntimeError):
    pass


class NovigClient:
    def __init__(self, bearer: Optional[str] = None, timeout: float = 30.0):
        token = bearer or NOVIG_AUTH_TOKEN
        if not token:
            raise NovigError(
                "No Novig bearer token. Set NOVIG_AUTH_TOKEN in Creds.py or "
                "pass bearer=... to NovigClient()."
            )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": "https://novig.com",
            "Referer": "https://novig.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) NovigClient/0.1",
        })

    def gql(self, query: str, variables: Optional[dict] = None,
            operation_name: Optional[str] = None) -> dict:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        if operation_name is not None:
            payload["operationName"] = operation_name
        r = self.session.post(NOVIG_GRAPHQL_URL, json=payload, timeout=self.timeout)
        if r.status_code != 200:
            raise NovigError(f"HTTP {r.status_code}: {r.text[:500]}")
        body = r.json()
        if body.get("errors"):
            raise NovigError(f"GraphQL errors: {json.dumps(body['errors'])[:1000]}")
        return body.get("data", {})

    # ---- NBX REST (orderbook engine) ----------------------------------------

    def get_market_books(self, market_ids: list[str],
                         currency: str = "CASH") -> list[dict]:
        """Fetch live orderbooks for one or more markets via NBX REST.

        Response shape (per market):
            {
              "market":  {id, description, isConsensus, strike, type,
                          player?, outcomes: [{id, index, description, marketId}]},
              "ladders": {<outcomeId>: {"bids": [order, ...], "asks": [order, ...]}}
            }
        Each order: {id, price, qty, originalQty, timestamp, status, tif,
                     outcomeId, marketId, inverted, isBid, currency}
        """
        if not market_ids:
            return []

        # The NBX origin (CloudFront) rejects long request URIs with 414 — a
        # ~132-id batch (query string ~5.2KB) already trips it, which bites on
        # outlier events like an NBA-finals slate (500+ markets). Chunk by a
        # conservative URL-byte budget so NO caller can 414, then aggregate.
        # Each id is a 36-char UUID + a 3-byte encoded comma; keep the query
        # string well under the limit. Callers no longer need to pre-chunk
        # (NovigMarketBookWorker / _fetch_books_for_event still may; their
        # smaller batches just pass through as a single request here).
        _MAX_QS_BYTES = 3500
        if len(market_ids) > 1:
            batches: list[list[str]] = []
            cur: list[str] = []
            cur_len = 0
            for mid in market_ids:
                add = len(mid) + 3  # id + encoded "%2C" separator
                if cur and cur_len + add > _MAX_QS_BYTES:
                    batches.append(cur)
                    cur, cur_len = [], 0
                cur.append(mid)
                cur_len += add
            if cur:
                batches.append(cur)
            if len(batches) > 1:
                out: list[dict] = []
                for b in batches:
                    out.extend(self.get_market_books(b, currency=currency))
                return out

        url = f"{NOVIG_NBX_BASE}/markets/book/batch"
        params = {"marketIds": ",".join(market_ids), "currency": currency}
        # NBX rejects requests with Content-Type set on a GET; build a clean
        # header set that only carries auth + accept.
        headers = {
            "Accept": "application/json",
            "Authorization": self.session.headers["Authorization"],
            "Origin": "https://novig.com",
            "Referer": "https://novig.com/",
        }
        r = requests.get(url, params=params, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise NovigError(
                f"NBX HTTP {r.status_code} for {r.url}\n  body: {r.text[:500]}"
            )
        return orjson.loads(r.content)  # C-level parse off the GIL faster than r.json()

    def price_parlay(self, outcome_ids: list[str],
                     boost_id: Optional[str] = None) -> dict:
        """Quote a parlay (SGP or cross-game) via the NBX parlay pricer.

        Posts to /nbx/v1/parlay/request with body shape:
            {"outcomes": [{"id": <outcomeId>}, ...], "boostId": <id|null>}

        Response is a parlay quote object: top-level `price` is the combined
        implied probability (multiplied across legs with SWISH correlation
        adjustments for true SGPs), plus per-leg prices under `legs[]`.
        Quote fields traderId/walletId/wager are null — this does NOT place
        a wager; it returns the price the site would offer.

        Currency (CASH vs COIN) is inferred from the bearer token's active
        wallet, not from the request body.

        TODO (future session): automated placement. The /parlay/request POST
        only quotes — actual wager submission is a separate endpoint (likely
        PATCH /nbx/v1/parlay/request/{id} with {wager, walletId} or a sibling
        /parlay/place route). Capture that request from the site's Network
        tab once, then add a `place_parlay(quote_id, stake)` method that
        reuses the quote returned here. Tie it to the scanner so mispriced
        rows above a delta threshold can auto-fire, but keep a hard stake
        cap and a per-day spend ceiling — auto-placement is exactly the
        behavior that gets accounts flagged fastest.
        """
        url = f"{NOVIG_NBX_BASE}/parlay/request"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self.session.headers["Authorization"],
            "Origin": "https://novig.com",
            "Referer": "https://novig.com/",
        }
        payload = {
            "outcomes": [{"id": oid} for oid in outcome_ids],
            "boostId": boost_id,
        }
        r = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        # Server returns 201 Created with the quote object wrapped in a list.
        if r.status_code not in (200, 201):
            raise NovigError(
                f"NBX HTTP {r.status_code} for {r.url}\n  body: {r.text[:500]}"
            )
        body = r.json()
        if isinstance(body, list):
            if not body:
                raise NovigError("Empty parlay quote response")
            return body[0]
        return body


def summarize_parlay_quote(quote: dict) -> dict:
    """Flatten a parlay/request response into a compact display dict.

    Returns:
      {
        "parlay_id": str,
        "status": str,                 # e.g. "Unfilled"
        "combined_price": float,       # implied prob of full parlay
        "combined_american": str,      # American odds string
        "unboosted_price": float|None,
        "naive_price": float,          # product of leg prices (no correlation)
        "correlation_edge": float,     # combined - naive (positive = SWISH gives you better than independent)
        "legs": [
            {"outcome_id": ..., "price": ..., "american": ...,
             "description": ..., "market_description": ...},
            ...
        ],
      }
    """
    def _f(x):
        try: return float(x)
        except (TypeError, ValueError): return None

    combined = _f(quote.get("price"))
    legs_out = []
    naive = 1.0
    for leg in quote.get("legs") or []:
        lp = _f(leg.get("price"))
        if lp is not None:
            naive *= lp
        out = leg.get("outcome") or {}
        mkt = out.get("market") or {}
        legs_out.append({
            "outcome_id": leg.get("outcomeId"),
            "price": lp,
            "american": fmt_american(lp),
            "description": out.get("description"),
            "market_description": mkt.get("description"),
            "vendor": leg.get("vendor"),
        })
    return {
        "parlay_id": quote.get("id"),
        "status": quote.get("status"),
        "combined_price": combined,
        "combined_american": fmt_american(combined),
        "unboosted_price": _f(quote.get("unboostedPrice")),
        "naive_price": naive if legs_out else None,
        "correlation_edge": (combined - naive) if (combined is not None and legs_out) else None,
        "legs": legs_out,
    }


def prob_to_american(p: Optional[float]) -> Optional[int]:
    """Convert an implied probability (0..1) to American moneyline odds.
    Returns None for invalid inputs. Sign convention: favorites negative,
    underdogs positive. Matches Novig UI (e.g. 0.178 -> +462, 0.822 -> -462)."""
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if not (0.0 < p < 1.0):
        return None
    if p < 0.5:
        return int(round((1.0 - p) / p * 100))
    return -int(round(p / (1.0 - p) * 100))


def fmt_american(p: Optional[float]) -> str:
    a = prob_to_american(p)
    if a is None:
        return ""
    return f"+{a}" if a > 0 else f"{a}"


def summarize_book(book_entry: dict) -> dict:
    """Convert one /book/batch entry into a per-outcome best-bid/ask summary.

    For binary markets (the common case on Novig), an order resting on one
    outcome is the same order from the opposite outcome's perspective:
    a bid on Under at price P with qty Q  ==  an ask on Over at price (1-P)
    with cross-size (1-P)*Q. We fold those implied prices directly into the
    opposite outcome's ask field so the display reads naturally.

    Per outcome:
      best_bid_price / best_bid_size  — best price/size to LAY this side
                                         (you receive premium up front)
      best_ask_price / best_ask_size  — best price/size to BACK this side
                                         (the price a taker pays)
                                         Includes liquidity implied by the
                                         opposite outcome's bids.

    All sizes are in displayed currency units (qty / NBX_QTY_SCALE), where
    ask side size is the stake required to fully take the level, not the qty.
    """
    market = book_entry.get("market") or {}
    ladders = book_entry.get("ladders") or {}
    outcomes = {o["id"]: o for o in (market.get("outcomes") or [])}
    outcome_ids = list(outcomes.keys())

    def _best_bid(orders: list):
        """Return (price, total_qty_at_top) or (None, 0)."""
        if not orders:
            return (None, 0.0)
        top = max(float(o["price"]) for o in orders)
        qty = sum(float(o["qty"]) for o in orders if float(o["price"]) == top)
        return (top, qty)

    def _best_ask(orders: list):
        if not orders:
            return (None, 0.0)
        top = min(float(o["price"]) for o in orders)
        qty = sum(float(o["qty"]) for o in orders if float(o["price"]) == top)
        return (top, qty)

    out: dict = {"market_id": market.get("id"),
                 "description": market.get("description"),
                 "type": market.get("type"),
                 "strike": market.get("strike"),
                 "outcomes": {}}

    # First pass: literal best bid/ask per outcome
    per_outcome: dict = {}
    for oid in outcome_ids:
        lad = ladders.get(oid) or {}
        bids = lad.get("bids") or []
        asks = lad.get("asks") or []
        bb_p, bb_q = _best_bid(bids)
        ba_p, ba_q = _best_ask(asks)
        per_outcome[oid] = {
            "literal_bid": (bb_p, bb_q),
            "literal_ask": (ba_p, ba_q),
            "bid_levels": len(bids),
            "ask_levels": len(asks),
        }

    # Second pass: fold implied prices. For binary markets only — multi-way
    # markets (rare on Novig) skip the implication step.
    binary = len(outcome_ids) == 2

    for oid in outcome_ids:
        info = per_outcome[oid]
        bb_p, bb_q = info["literal_bid"]
        ba_p, ba_q = info["literal_ask"]

        if binary:
            other = next(o for o in outcome_ids if o != oid)
            o_bb_p, o_bb_q = per_outcome[other]["literal_bid"]
            o_ba_p, o_ba_q = per_outcome[other]["literal_ask"]
            # opposite's bid implies an ask on this side at (1 - p), size = qty * (1-p)
            if o_bb_p is not None:
                implied_ask_price = 1.0 - o_bb_p
                implied_ask_size = o_bb_q * implied_ask_price
                if ba_p is None or implied_ask_price < ba_p:
                    ba_p, ba_q = implied_ask_price, implied_ask_size
            # opposite's ask implies a bid on this side at (1 - p)
            if o_ba_p is not None:
                implied_bid_price = 1.0 - o_ba_p
                implied_bid_size = o_ba_q * implied_bid_price
                if bb_p is None or implied_bid_price > bb_p:
                    bb_p, bb_q = implied_bid_price, implied_bid_size

        out["outcomes"][oid] = {
            "description": (outcomes.get(oid) or {}).get("description"),
            "best_bid_price": round(bb_p, 6) if bb_p is not None else None,
            "best_bid_size": round(bb_q / NBX_QTY_SCALE, 2) if bb_p is not None else 0.0,
            "best_ask_price": round(ba_p, 6) if ba_p is not None else None,
            "best_ask_size": round(ba_q / NBX_QTY_SCALE, 2) if ba_p is not None else 0.0,
            "bid_levels": info["bid_levels"],
            "ask_levels": info["ask_levels"],
        }
    return out


# ---------------------------------------------------------------------------
# High-level query helpers
# ---------------------------------------------------------------------------
# Field selections are built against the introspected schema (see
# novig_schema.json). Tables we use:
#   event   — wagerable container (game, period, prop bucket); forms a tree
#             via parent_event / events[]
#   market  — SPREAD/TOTAL/MONEY/CUSTOM; carries strike, is_consensus, volume
#   outcome — order-book side; last (price), available (size), altLast/altAvailable
#   game    — live state (scores, situation) attached to top-level event
#
# Filter shape replicates the captured EventMarkets_Query:
#   status = "OPEN" AND (is_consensus = true OR outcomes.available IS NOT NULL)
# i.e. consensus aggregate OR a real orderbook side with liquidity.


_EVENT_FIELDS = """
  id
  type
  league
  status
  description
  scheduled_start
  is_visible_pregame
  is_visible_live
  is_status_locked
  parent_event_id
  game_id
"""

_GAME_FIELDS = """
  id
  league
  sport
  status
  scheduled_start
  period
  time_remaining
  home_score
  away_score
  homeTeam { id name symbol primary_color }
  awayTeam { id name symbol primary_color }
"""

_OUTCOME_FIELDS = """
  id
  description
  type
  index
  status
  last
  available
  altLast
  altAvailable
  competitorId
  competitor { id name symbol }
"""

_MARKET_FIELDS = f"""
  id
  type
  status
  description
  strike
  is_consensus
  volume
  league
  competitorId
  playerId
  competitor {{ id name symbol }}
  player {{ id }}
  market_detail {{ question }}
  outcomes {{ {_OUTCOME_FIELDS} }}
"""


# ============================================================================
# Wallet + open-positions fetchers (async, used by LiquidityWidget)
# ----------------------------------------------------------------------------
# NV exposes account state via two Hasura GraphQL operations the web app
# already calls — we reuse the exact query/variable shapes so server-side
# validators see traffic indistinguishable from novig.com.
# ============================================================================

_NV_BALANCE_QUERY = """
query WalletBalance_Query($userId: uuid) {
  user(where: {id: {_eq: $userId}}) {
    id
    trader {
      id
      trader_wallets: wallets(where: {type: {_eq: "Trader"}, currency: {_eq: "CASH"}}) {
        id balance __typename
      }
      coin_wallets: wallets(where: {type: {_eq: "Trader"}, currency: {_eq: "COIN"}}) {
        id balance __typename
      }
      bonus_wallets: wallets(where: {type: {_eq: "Bonus"}}) {
        id balance __typename
      }
      __typename
    }
    withdrawal_hold
    __typename
  }
}
""".strip()

_NV_POSITIONS_QUERY = """
query ActivePortfolioOrders_Query($trader_id: uuid!, $item_sort: jsonb!,
                                   $item_where: jsonb!, $offset: Int!, $limit: Int!) {
  ActivePortfolioOrders_Query(
    args: {trader_id: $trader_id, item_sort: $item_sort,
           item_where: $item_where, offset: $offset, limit: $limit}
  ) {
    id timeToLiveMs qty originalQty created_at updated_at status price
    isBid tif currency market outcome fills entry_promotion_name __typename
  }
}
""".strip()

_NV_REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {NOVIG_AUTH_TOKEN}",
    "Origin": "https://novig.com",
    "Referer": "https://novig.com/",
}


async def _nv_post_gql_async(session, operation_name: str,
                             query: str, variables: dict) -> dict:
    payload = {"operationName": operation_name,
               "query": query, "variables": variables}
    async with session.post(NOVIG_GRAPHQL_URL, headers=_NV_REQUEST_HEADERS,
                            data=json.dumps(payload)) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_nv_balance(session, user_id: str) -> dict:
    """Return {'cash': float|None, 'coin': float|None, 'bonus': float|None,
    'error': str|None}. user_id is the NOVIG_USER_ID uuid."""
    try:
        body = await _nv_post_gql_async(
            session, "WalletBalance_Query", _NV_BALANCE_QUERY,
            {"userId": user_id})
    except Exception as e:
        return {"cash": None, "coin": None, "bonus": None, "error": str(e)}
    users = ((body or {}).get("data") or {}).get("user") or []
    if not users:
        errs = (body or {}).get("errors")
        msg = (errs[0].get("message") if errs else "empty user response")
        return {"cash": None, "coin": None, "bonus": None, "error": msg}
    trader = (users[0] or {}).get("trader") or {}
    tw = trader.get("trader_wallets") or []
    cw = trader.get("coin_wallets") or []
    bw = trader.get("bonus_wallets") or []
    # Stash the CASH trader walletId in novig_async's cache so single-bet
    # placement (place_order_async) can find it without forcing the user
    # to place a parlay first. The balance query is the earliest call
    # that ships this id back; before this hook, _NOVIG_WALLET_ID_CACHE
    # was only ever populated by parlay-execute/orders responses, which
    # don't fire until *after* a placement attempt — hence the "No Novig
    # walletId available" error on the very first single-bet Place click.
    if tw and tw[0].get("id"):
        try:
            from novig_async import _harvest_wallet_id
            _harvest_wallet_id({"walletId": tw[0]["id"]})
        except ImportError:
            pass
    return {
        "cash": float(tw[0].get("balance") or 0) if tw else None,
        "coin": float(cw[0].get("balance") or 0) if cw else None,
        "bonus": float(bw[0].get("balance") or 0) if bw else None,
        "error": None,
    }


async def fetch_nv_open_positions(session, trader_id: str,
                                  auth_id: str) -> list:
    """Return the raw ActivePortfolioOrders_Query rows; caller normalizes.
    `trader_id` is NOVIG_TRADER_ID, `auth_id` is the google-oauth2|...
    subject from NOVIG_AUTH_ID."""
    pos_vars = {
        "trader_id": trader_id,
        "item_sort": {"created_at": "desc"},
        "item_where": {
            "_and": [
                {"status": {"_neq": "REJECTED"}},
                {"trader": {"user": {"auth_id": {"_eq": auth_id}}}},
                {"currency": {"_eq": "CASH"}},
                {"_or": [
                    {"market": {"status": {"_eq": "OPEN"}}},
                    {"market": {"status": {"_eq": "OPEN_LIVE"}}},
                ]},
                {"status_filters": []},
            ]
        },
        "offset": 0,
        "limit": 50,
    }
    body = await _nv_post_gql_async(
        session, "ActivePortfolioOrders_Query",
        _NV_POSITIONS_QUERY, pos_vars)
    return (((body or {}).get("data") or {})
            .get("ActivePortfolioOrders_Query") or [])


def nv_decimal_to_american(p) -> str:
    """Convert NV's decimal price (0..1 probability) to american odds."""
    try:
        p = float(p)
    except Exception:
        return ""
    if not (0 < p < 1):
        return ""
    if p >= 0.5:
        return f"-{round(100 * p / (1 - p))}"
    return f"+{round(100 * (1 - p) / p)}"


class NovigQueries:
    """High-level read queries layered on top of NovigClient.gql()."""

    def __init__(self, client: "NovigClient"):
        self.client = client

    def list_events(self, league: Optional[str] = "MLB",
                    status_in: tuple[str, ...] = ("OPEN_PREGAME", "OPEN_INGAME"),
                    visible_only: bool = True,
                    limit: int = 200) -> list[dict]:
        """List top-level events. Filters out child/sub events.

        Pass league=None to fetch every league in one round-trip (used
        by scrape_all_leagues to avoid maintaining a hard-coded enum
        of Novig's league codes and silently dropping tennis/UFC/PGA/
        most soccer comps the way the old per-league loop did)."""
        where: dict = {
            "status": {"_in": list(status_in)},
            "parent_event_id": {"_is_null": True},  # top-level only
        }
        if league is not None:
            where["league"] = {"_eq": league}
        if visible_only:
            # PREGAME events use is_visible_pregame; INGAME use is_visible_live.
            where["_or"] = [
                {"is_visible_pregame": {"_eq": True}},
                {"is_visible_live": {"_eq": True}},
            ]
        query = f"""
        query ListEvents($where: event_bool_exp!, $limit: Int!) {{
          event(where: $where, limit: $limit, order_by: {{scheduled_start: asc}}) {{
            {_EVENT_FIELDS}
            game {{ {_GAME_FIELDS} }}
            markets_aggregate {{ aggregate {{ sum {{ volume }} }} }}
          }}
        }}
        """
        data = self.client.gql(query, {"where": where, "limit": limit},
                               operation_name="ListEvents")
        return data.get("event", [])

    def get_event_markets(self, event_id: str,
                          only_available: bool = True,
                          tree_depth: int = 3) -> Optional[dict]:
        """Fetch one event with its markets + outcomes, recursively walking
        sub-events to tree_depth levels. Mirrors EventMarkets_Query but
        extended to include child events (periods, props, etc.).

        only_available=True applies the captured filter: status=OPEN AND
        (is_consensus OR has an outcome with non-null available liquidity).
        """
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
            market_where = {}

        # Build nested events { markets, events { markets, ... } } selection
        # to the requested depth. Markets at each level use the same filter.
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
        data = self.client.gql(
            query,
            {"eventId": event_id, "where": market_where},
            operation_name="EventMarkets",
        )
        events = data.get("event", [])
        return events[0] if events else None

    @staticmethod
    def flatten_markets(event_node: dict) -> list[dict]:
        """Walk an event tree (as returned by get_event_markets) and return a
        flat list of all markets across the root event and every descendant.
        Each market gets an injected '_event' dict with id/type/description
        so you know which sub-event it belongs to."""
        out: list[dict] = []
        def walk(node: dict) -> None:
            if not node:
                return
            ev_meta = {k: node.get(k) for k in ("id", "type", "description", "status")}
            for m in node.get("markets") or []:
                m = dict(m)
                m["_event"] = ev_meta
                out.append(m)
            for child in node.get("events") or []:
                walk(child)
        walk(event_node)
        return out

    def get_mlb_pregame(self, limit: int = 100) -> list[dict]:
        return self.list_events(league="MLB", status_in=("OPEN_PREGAME",), limit=limit)

    def get_mlb_live(self, limit: int = 50) -> list[dict]:
        return self.list_events(league="MLB", status_in=("OPEN_INGAME",), limit=limit)

    # ---------------------------------------------------------------------
    # Dump scraper
    # ---------------------------------------------------------------------
    # Produces a JSON dump in a shape parallel to ProphetX's
    # all_markets_combined_*.json, so the LiquidityWidget's existing load
    # / stale-fallback / async-refresh machinery can consume Novig data
    # with the same code path. See _event_to_dump_entry below for the
    # per-event layout.

    def scrape_all_mlb(self, *,
                       with_books: bool = False,
                       currency: str = "CASH",
                       max_workers: int = 4,
                       throttle_s: float = 0.25,
                       jitter_s: float = 0.20,
                       only_available: bool = False,
                       save: bool = True,
                       dump_dir: Optional[pathlib.Path] = None,
                       progress: bool = True) -> dict:
        """Scrape all current MLB pregame + live events into a single
        combined dump.

        Args:
            with_books: also fetch full NBX /book/batch ladders per
                event. Adds one batch RTT per ~20 markets, so a full
                slate with books can take a few minutes. Default off
                because callers can lazy-fetch.
            currency: "CASH" or "COIN" — only relevant when with_books.
            only_available: passed to get_event_markets. False = all
                markets even empty ones; True = website's
                consensus-OR-has-liquidity filter.
            save: if True, overwrites <dump_dir>/all_events_combined_latest.json.
            dump_dir: override write location (default NOVIG_DUMP_DIR).
        """
        return self.scrape_all_leagues(
            leagues=("MLB",),
            with_books=with_books, currency=currency,
            max_workers=max_workers, throttle_s=throttle_s, jitter_s=jitter_s,
            only_available=only_available, save=save, dump_dir=dump_dir,
            progress=progress,
        )

    def scrape_all_leagues(self, *,
                           leagues: Optional[tuple[str, ...]] = None,
                           with_books: bool = False,
                           currency: str = "CASH",
                           max_workers: int = 4,
                           throttle_s: float = 0.25,
                           jitter_s: float = 0.20,
                           only_available: bool = False,
                           save: bool = True,
                           dump_dir: Optional[pathlib.Path] = None,
                           progress: bool = True) -> dict:
        """Scrape every Novig event into a single combined dump.

        leagues=None (the default) fetches across ALL leagues Novig
        currently exposes in two round-trips (one pregame, one ingame)
        — tennis, UFC, PGA, every soccer comp, etc. Pass an explicit
        tuple like ("MLB", "NBA") to scope to a subset.

        Output dump shape is identical to scrape_all_mlb
        (event_metadata.tournament carries the league code), so
        LiquidityWidget consumes it unchanged."""
        dump_dir = dump_dir or NOVIG_DUMP_DIR

        events: list[dict] = []
        if leagues is None:
            if progress:
                print("[novig.scrape] listing ALL leagues (one shot)...")
            try:
                # Bumped limits because the no-filter response covers
                # every sport on the slate — easily 200+ matches on a
                # tennis-heavy afternoon between ATP + WTA + Challenger.
                events += self.list_events(
                    league=None, status_in=("OPEN_PREGAME",), limit=1000)
                events += self.list_events(
                    league=None, status_in=("OPEN_INGAME",), limit=500)
            except NovigError as e:
                if progress:
                    print(f"  all-leagues listing failed: {e}")
        else:
            for lg in leagues:
                if progress:
                    print(f"[novig.scrape] listing {lg} events...")
                try:
                    events += self.list_events(
                        league=lg, status_in=("OPEN_PREGAME",), limit=200)
                    events += self.list_events(
                        league=lg, status_in=("OPEN_INGAME",), limit=100)
                except NovigError as e:
                    if progress:
                        print(f"  {lg} listing failed: {e}")
        seen_ids: set[str] = set()
        unique_events: list[dict] = []
        for ev in events:
            eid = ev.get("id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                unique_events.append(ev)

        if progress:
            print(f"[novig.scrape] {len(unique_events)} events "
                  f"(workers={max_workers}, "
                  f"throttle={throttle_s}+jitter≤{jitter_s}s)")

        dump: dict[str, dict] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(self._fetch_one_event_for_dump, ev["id"],
                              only_available, throttle_s, jitter_s): ev["id"]
                    for ev in unique_events}
            for i, fut in enumerate(as_completed(futs), 1):
                eid, entries = fut.result()
                if not entries:
                    if progress:
                        print(f"  [{i}/{len(unique_events)}] {eid} (empty)")
                    continue
                # One listed event can explode into several matchable
                # entries (tennis/golf tournament -> per-match children).
                for sub_id, entry in entries:
                    dump[sub_id] = entry
                if progress:
                    head = entries[0][1]
                    extra = (f" +{len(entries) - 1} more"
                             if len(entries) > 1 else "")
                    n_mkts = len((head.get("data") or {}).get("markets") or [])
                    print(f"  [{i}/{len(unique_events)}] "
                          f"{head['event_metadata']['name']:30s} "
                          f"({n_mkts} markets){extra}")

        if with_books:
            if progress:
                print(f"[novig.scrape] fetching books (currency={currency})...")
            for j, (eid, entry) in enumerate(dump.items(), 1):
                entry["books"] = self._fetch_books_for_event(
                    entry, currency=currency,
                    throttle_s=throttle_s, jitter_s=jitter_s)
                if progress:
                    print(f"  [{j}/{len(dump)}] "
                          f"{entry['event_metadata']['name']:30s} "
                          f"({len(entry['books'])} markets booked)")

        elapsed = time.time() - t0
        if progress:
            print(f"[novig.scrape] done in {elapsed:.1f}s — "
                  f"{len(dump)} events captured")

        if save and dump:
            dump_dir.mkdir(parents=True, exist_ok=True)
            # Single overwritten file (atomic temp+replace), not archived — see
            # _save_dump_sync in novig_async for the why. ([PERF-DIAG])
            out = dump_dir / "all_events_combined_latest.json"
            tmp = dump_dir / "all_events_combined_latest.json.tmp"
            tmp.write_text(json.dumps(dump, indent=2, default=str))
            tmp.replace(out)
            # Slim metadata-only companion for the startup match-map build.
            write_events_index(dump, dump_dir)
            # Per-event sidecars for the widget's lazy market hydration.
            write_event_sidecars(dump, dump_dir)
            if progress:
                size_mb = out.stat().st_size / (1024 * 1024)
                print(f"[novig.scrape] wrote {out}  ({size_mb:.2f} MB)")
        return dump

    def _fetch_one_event_for_dump(self, event_id: str,
                                  only_available: bool,
                                  throttle_s: float, jitter_s: float
                                  ) -> tuple[str, list[tuple[str, dict]]]:
        """Worker for scrape_all_leagues. Returns (event_id, entries),
        where entries is a list of (entry_id, dump_entry) — usually one,
        but several for tournament containers that expand into per-match
        children. Always sleeps post-request to throttle the rate."""
        try:
            ev = self.get_event_markets(event_id, only_available=only_available)
        except NovigError:
            ev = None
        finally:
            time.sleep(throttle_s + random.uniform(0.0, jitter_s))
        if not ev:
            return event_id, []
        return event_id, _event_to_dump_entries(ev)

    def _fetch_books_for_event(self, entry: dict, *,
                               currency: str,
                               batch_size: int = 20,
                               throttle_s: float = 0.25,
                               jitter_s: float = 0.20) -> dict:
        """Optional second pass — fetch /book/batch for an event's
        markets and return {market_id: book_entry}. Skips markets that
        are clearly empty (non-consensus, no `available` price)."""
        markets = (entry.get("data") or {}).get("markets") or []
        candidate_ids: list[str] = []
        for m in markets:
            if m.get("status") != "OPEN":
                continue
            if m.get("is_consensus"):
                candidate_ids.append(m["id"])
                continue
            outcomes = m.get("outcomes") or []
            if any(o.get("available") is not None for o in outcomes):
                candidate_ids.append(m["id"])

        books_out: dict[str, dict] = {}
        for i in range(0, len(candidate_ids), batch_size):
            batch = candidate_ids[i:i + batch_size]
            try:
                resp = self.client.get_market_books(batch, currency=currency)
            except NovigError:
                resp = []
            for b in resp:
                mid = (b.get("market") or {}).get("id")
                if mid:
                    books_out[mid] = b
            time.sleep(throttle_s + random.uniform(0.0, jitter_s))
        return books_out

    @staticmethod
    def load_latest_dump(dump_dir: Optional[pathlib.Path] = None
                         ) -> Optional[dict]:
        """Load the most recent novig dump file. Returns None if none."""
        dump_dir = dump_dir or NOVIG_DUMP_DIR
        if not dump_dir.exists():
            return None
        files = sorted(dump_dir.glob("all_events_combined_*.json"),
                       key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        # orjson on raw bytes: the dump is ~38MB; stdlib json.loads(read_text())
        # held the GIL ~500ms here and stuttered the ticker. read_bytes() also
        # skips a full-file UTF-8 decode. ([PERF-DIAG] — see PERF_DIAGNOSTICS.md)
        return orjson.loads(files[-1].read_bytes())

    @staticmethod
    def load_event_entry(event_id: str,
                         dump_dir: Optional[pathlib.Path] = None
                         ) -> Optional[dict]:
        """Load ONE event's dump entry from its sidecar file (~100KB / <1ms)
        instead of parsing the full combined dump. Returns None when the
        sidecar is absent (dump written before sidecars existed, or the event
        isn't in the latest dump) — callers fall back to the full-dump parse."""
        dump_dir = dump_dir or NOVIG_DUMP_DIR
        p = dump_dir / NOVIG_EVENT_SIDECAR_DIRNAME / f"{event_id}.json"
        try:
            return orjson.loads(p.read_bytes())
        except (FileNotFoundError, ValueError, OSError):
            return None

    @staticmethod
    def load_events_index(dump_dir: Optional[pathlib.Path] = None
                          ) -> Optional[dict]:
        """Load the slim event-metadata index (events_index_latest.json).

        Returns a {event_id: {"event_metadata": {...}}} dict — the same shape
        as the full dump but with no `data.markets`, so it's a drop-in for the
        match-map build's event-level pairing. ~0.5MB / ~2ms vs the full dump's
        ~48MB / ~122ms GIL-held parse.

        Falls back to deriving the index from the full dump when the index file
        is absent (e.g. an old dump written before this companion existed, or
        the first run after upgrade), so the caller never has to special-case
        a missing index. Returns None only when there's no data at all."""
        dump_dir = dump_dir or NOVIG_DUMP_DIR
        idx = dump_dir / NOVIG_EVENTS_INDEX_NAME
        if idx.exists():
            try:
                return orjson.loads(idx.read_bytes())
            except Exception:
                pass  # fall through to full-dump derivation
        full = NovigQueries.load_latest_dump(dump_dir)
        if not full:
            return None
        return {eid: {"event_metadata": (e or {}).get("event_metadata")}
                for eid, e in full.items()}


# League code -> sport label, used as the sport default when a node has
# no `game` block (true for tennis/golf child matches, whose game is
# null). Keeps the dump's event_metadata.sport meaningful for display;
# matching keys off the league code regardless (see canonical_sport in
# exchange_market_keys.py), so a wrong default here can't break pairing.
_LEAGUE_SPORT: dict[str, str] = {
    "MLB": "Baseball", "NCAABSB": "Baseball",
    "NBA": "Basketball", "WNBA": "Basketball", "NCAAB": "Basketball", "NCAAWB": "Basketball",
    "NHL": "Ice Hockey", "Olympics Hockey Men": "Ice Hockey",
    "NFL": "American Football", "NCAAF": "American Football", "NCAA_FB": "American Football",
    "ATP": "Tennis", "WTA": "Tennis",
    "PGA": "Golf", "TGL": "Golf",
    "UFC": "Mixed Martial Arts",
    "MLS": "Soccer", "EPL": "Soccer",
    "Bundesliga": "Soccer", "Champions League": "Soccer", "Europa League": "Soccer",
    "La Liga": "Soccer", "Ligue 1": "Soccer", "Serie A": "Soccer",
    "FIFA World Cup": "Soccer",
    "Boxing": "Boxing", "WBC": "Boxing",
    "Counter Strike 2": "Esports", "Dota 2": "Esports", "League of Legends": "Esports",
    "ENTERTAINMENT": "Entertainment",
}


def _event_to_dump_entry(event_node: dict,
                         markets_override: Optional[list] = None) -> dict:
    """Build the per-event dump entry. Mirrors ProphetX's
    {event_metadata, data:{markets}} shape so the widget's loader can
    consume both sources via one code path.

    markets_override: when given, use exactly these markets instead of
    flattening the node's subtree. Used to emit a tournament-level
    futures entry from a container's *direct* markets without absorbing
    its per-match child events (which become their own entries)."""
    game = event_node.get("game") or {}
    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    league = event_node.get("league") or game.get("league") or ""
    # No game block (tennis/golf child matches) -> participant names live
    # in the description ("<Away> @ <Home> <round>"). Parse them so the
    # matcher has something to pair on; leave symbols None (no team table
    # for individual sports).
    away_name = away.get("name")
    home_name = home.get("name")
    if not (home_name and away_name):
        a, h = _parse_at_names(event_node.get("description") or "")
        away_name = away_name or a
        home_name = home_name or h
    markets = (markets_override if markets_override is not None
               else NovigQueries.flatten_markets(event_node))
    # Per-event volume = sum of each market's `volume` field (USD traded).
    # Novig has no single event-level total on the GraphQL surface, but every
    # market carries `volume`; summing them is the figure novig.com shows on
    # the event card and is what downstream stake-sort / $0-event filtering
    # rely on. (Previously hardcoded 0.0, which made live games look untraded
    # and got them filtered out of the dropdown.)
    total_volume = 0.0
    for m in markets:
        try:
            total_volume += float(m.get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
    # Listing path (NovigQueries.list_events) ships no market subtree — only
    # a `markets_aggregate` sum(volume). Fall back to it so dropdown events
    # carry their traded volume even though their markets are hydrated lazily.
    if total_volume == 0.0:
        agg = (((event_node.get("markets_aggregate") or {})
                .get("aggregate") or {}).get("sum") or {}).get("volume")
        try:
            total_volume = float(agg or 0.0)
        except (TypeError, ValueError):
            total_volume = 0.0
    meta = {
        "id": event_node.get("id") or "",
        "name": event_node.get("description") or "",
        "startTime": event_node.get("scheduled_start") or "",
        "status": event_node.get("status") or "",
        # Novig event taxonomy: "Game" (normal matchup, has a game block),
        # "Future" (outright/futures — Championship Winner, MVP, etc., no
        # game block, markets typed CHAMPIONSHIP_WINNER/*_WINNER), or
        # "Tournament" (tennis/golf container expanded into child matches).
        # Surfaced so the widget can categorize futures separately from games.
        "event_type": event_node.get("type") or "",
        "tournament": league,
        "sport": game.get("sport") or _LEAGUE_SPORT.get(league) or "Baseball",
        "stake": total_volume,
        "home_team_symbol": home.get("symbol"),
        "home_team_name": home_name,
        "away_team_symbol": away.get("symbol"),
        "away_team_name": away_name,
        "game_status": game.get("status"),
        "game_period": game.get("period"),
        "home_score": game.get("home_score"),
        "away_score": game.get("away_score"),
    }
    return {
        "event_metadata": meta,
        "data": {"markets": markets},
    }


def _parse_at_names(desc: str) -> tuple[Optional[str], Optional[str]]:
    """Split a Novig '<Away> @ <Home>' description into (away, home).
    Used for individual-sport child matches whose game block is null.
    Returns (None, None) when no separator is found."""
    if not desc:
        return (None, None)
    for sep in (" @ ", " at ", " vs. ", " vs "):
        if sep in desc:
            left, right = desc.split(sep, 1)
            return (left.strip() or None, right.strip() or None)
    return (None, None)


def _event_to_dump_entries(event_node: dict) -> list[tuple[str, dict]]:
    """Expand one listed event node into 0..N matchable dump entries.

    Most events (MLB/NBA/UFC games, futures) are a single matchable unit:
    the whole subtree (game + prop sub-events) collapses into one entry,
    preserving the prior _event_to_dump_entry behavior.

    Tennis/golf are different: the listed top-level node is a *tournament
    container* (type=Tournament, no direct markets) whose `events[]`
    children are the individual matches. For those we recurse and emit
    one entry per child Game so each match pairs against its ProphetX
    counterpart — the user's chosen unit of matching.

    Settled/voided nodes are dropped. The LISTING is already filtered to
    OPEN_PREGAME/OPEN_INGAME, but a tournament container stays open for
    its whole run while its `events[]` children span every completed
    round — without this guard a single ATP container exploded into
    hundreds of FINAL matches (~68% of the dump), bloating the match map
    and leaving stale candidates for the event matcher to trip on.
    """
    if not event_node:
        return []
    is_tournament = (event_node.get("type") or "").lower() == "tournament"
    status = (event_node.get("status") or "").upper()
    if not is_tournament and status in ("FINAL", "CANCELED", "CANCELLED"):
        return []
    children = event_node.get("events") or []
    direct_markets = event_node.get("markets") or []

    if is_tournament and children:
        out: list[tuple[str, dict]] = []
        # Keep any tournament-level futures/outright markets as their own
        # entry (e.g. "Championship Winner") so they aren't dropped.
        if direct_markets:
            out.append((str(event_node.get("id") or ""),
                        _event_to_dump_entry(event_node,
                                             markets_override=direct_markets)))
        for child in children:
            out.extend(_event_to_dump_entries(child))
        return out

    return [(str(event_node.get("id") or ""), _event_to_dump_entry(event_node))]


# ---------------------------------------------------------------------------
# SGP correlation-floor scanner
# ---------------------------------------------------------------------------
# The SWISH parlay pricer occasionally violates the deterministic implication
# floor: for two events A and B where A => B (B always happens whenever A does),
# the parlay price P(A ∧ B) must equal P(A). If SWISH returns a parlay price
# *below* P(A_leg), the parlay strictly dominates the single A wager — same
# winning condition, larger payout.
#
# MLB deterministic implications among the common props (all on Over/Under 0.5):
#
#   OVER side:  HR ≥ 1  =>  RBI ≥ 1, R ≥ 1, H ≥ 1
#               (a HR drives the batter in, scores them, and counts as a hit)
#
#   UNDER side (contrapositive):
#               Under_RBI  =>  Under_HR
#               Under_Runs =>  Under_HR
#               Under_Hits =>  Under_HR
#               (if no RBI/run/hit, then no HR either)
#
# Each tuple: (dominant_type, dominant_side, implied_type, implied_side).
# "Dominant" = the leg that *implies* the other. The parlay's correlation
# floor is P(dominant). SWISH violates the floor when combined < P(dominant).

SGP_IMPLICATION_PAIRS = [
    # Over side: HR is dominant (HR => X)
    ("HOME_RUNS", "Over",  "RBIS",  "Over"),
    ("HOME_RUNS", "Over",  "RUNS",  "Over"),
    ("HOME_RUNS", "Over",  "HITS",  "Over"),
    # Under side: the non-HR leg is dominant (Under_X => Under_HR)
    ("RBIS",      "Under", "HOME_RUNS", "Under"),
    ("RUNS",      "Under", "HOME_RUNS", "Under"),
    ("HITS",      "Under", "HOME_RUNS", "Under"),
]


def _find_outcome(market: dict, side: str, strike: float = 0.5) -> Optional[dict]:
    """Find the Over/Under outcome on a market at the given strike."""
    if market.get("strike") != strike:
        return None
    prefix = side.lower()
    for o in market.get("outcomes") or []:
        d = (o.get("description") or "").lower()
        if d.startswith(prefix):
            return o
    return None


def scan_sgp_implications(client: "NovigClient", event_id: str,
                          pairs: list[tuple[str, str, str, str]] = SGP_IMPLICATION_PAIRS,
                          strike: float = 0.5,
                          max_workers: int = 4,
                          throttle_s: float = 0.25,
                          jitter_s: float = 0.20) -> list[dict]:
    """Quote every (dominant, implied) SGP pair per player for one event.

    Concurrency: `max_workers` threads run quote requests in parallel. Each
    worker sleeps `throttle_s + uniform(0, jitter_s)` after its request so the
    aggregate rate stays in human-browsing territory rather than firehose. With
    the defaults (4 workers, ~0.35s mean per worker) the effective request rate
    is ~11 req/s — comparable to a user opening one parlay-builder leg per
    ~90ms. Tune down for stealthier scans, up only if you've cleared it.

    Each pair is (dominant_type, dominant_side, implied_type, implied_side)
    where dominant => implied, so the parlay floor is P(dominant_leg).
    A row is `mispriced` when combined < dominant_price.
    """
    import random as _random
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    queries = NovigQueries(client)
    ev = queries.get_event_markets(event_id, only_available=False)
    if not ev:
        return []
    mkts = NovigQueries.flatten_markets(ev)

    by_player_type: dict = {}
    for m in mkts:
        pid = m.get("playerId")
        if not pid or m.get("strike") != strike:
            continue
        by_player_type.setdefault(pid, {})[m.get("type")] = m

    # Build the task list up front so the request fan-out is cheap.
    tasks: list[dict] = []
    for pid, type_map in by_player_type.items():
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
                dom_p = float(dom_p); imp_p = float(imp_p)
            except (TypeError, ValueError):
                continue
            tasks.append({
                "dom_t": dom_t, "dom_side": dom_side,
                "imp_t": imp_t, "imp_side": imp_side,
                "dom_mkt": dom_mkt, "dom_out": dom_out, "dom_p": dom_p,
                "imp_out": imp_out, "imp_p": imp_p,
            })

    def _run(t: dict) -> Optional[dict]:
        try:
            quote = client.price_parlay([t["dom_out"]["id"], t["imp_out"]["id"]])
        except NovigError:
            return None
        finally:
            # Per-worker throttle with jitter — keeps the aggregate rate
            # sane regardless of how many workers complete back-to-back.
            _time.sleep(throttle_s + _random.uniform(0.0, jitter_s))
        s = summarize_parlay_quote(quote)
        combined = s["combined_price"]
        if combined is None:
            return None
        dom_p, imp_p = t["dom_p"], t["imp_p"]
        naive = dom_p * imp_p
        name = (t["dom_mkt"].get("description") or "").rsplit(f" {strike} ", 1)[0]
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
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed(ex.submit(_run, t) for t in tasks):
            r = fut.result()
            if r is not None:
                results.append(r)
    results.sort(key=lambda r: r["delta_vs_dominant"])
    return results


def _main_cli():
    # Wrapped in a function (rather than running directly inside the
    # `if __name__ == "__main__":` block) so it can be called AFTER
    # the NovigGeoHarvester class + geo_harvester singleton are
    # defined further down the file. --refresh-geo would otherwise
    # NameError on geo_harvester.
    import argparse, sys

    ap = argparse.ArgumentParser(description="Novig client smoke test / CLI")
    ap.add_argument("--list", action="store_true",
                    help="List MLB pregame + live events")
    ap.add_argument("--event", metavar="ID",
                    help="Fetch one event's markets + outcomes by UUID")
    ap.add_argument("--league", default="MLB")
    ap.add_argument("--only-available", action="store_true",
                    help="Apply the website's filter (consensus OR has liquidity). "
                         "Default is to return ALL markets, including empty/no-bid props.")
    ap.add_argument("--book", metavar="MARKET_ID", action="append",
                    help="Fetch live orderbook(s) via NBX REST. Repeat to batch.")
    ap.add_argument("--currency", default="CASH", choices=["CASH", "COIN"],
                    help="CASH = real-money orderbook, COIN = sweepstakes play-money. "
                         "Separate ladders, separate liquidity.")
    ap.add_argument("--parlay", metavar="OUTCOME_ID", action="append",
                    help="Quote a parlay. Pass --parlay once per leg (outcome UUID). "
                         "Single-game legs are priced by SWISH with correlation.")
    ap.add_argument("--boost-id", default=None,
                    help="Optional boost token id to apply to the parlay quote.")
    ap.add_argument("--scan-sgp", metavar="EVENT_ID",
                    help="Scan all deterministic-implication SGP pairs for an event "
                         "(HR=>RBI, HR=>Runs, HR=>Hits). Flags parlays where SWISH "
                         "prices below the implication floor (P(parlay) < P(dominant leg)).")
    ap.add_argument("--mispriced-only", action="store_true",
                    help="With --scan-sgp, only print mispriced rows.")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel quote workers for --scan-sgp (default 4). "
                         "Keep low to stay in human-traffic territory.")
    ap.add_argument("--throttle", type=float, default=0.25,
                    help="Per-worker post-request sleep in seconds (default 0.25). "
                         "Each worker also adds up to --jitter random extra delay.")
    ap.add_argument("--jitter", type=float, default=0.20,
                    help="Max additional random per-worker delay (default 0.20).")
    ap.add_argument("--scrape-dump", action="store_true",
                    help="Scrape all MLB pregame + live events into "
                         "novig_dumps/all_events_combined_<ts>.json. Mirrors "
                         "ProphetX's combined-dump shape so the widget can "
                         "load both sources via one code path.")
    ap.add_argument("--refresh-geo", action="store_true",
                    help="Drive a fresh Novig geo_tx via Selenium + "
                         "Tampermonkey userscript and write it to the "
                         "cache. Pair with --headless for background "
                         "use. Bypasses all other CLI flags.")
    ap.add_argument("--headless", action="store_true",
                    help="Run --refresh-geo without a visible browser "
                         "window.")
    ap.add_argument("--with-books", action="store_true",
                    help="With --scrape-dump, also fetch /book/batch ladders "
                         "per event (slower; widget can lazy-fetch otherwise).")
    args = ap.parse_args()

    # --refresh-geo short-circuits — no GraphQL client needed, just
    # drive the Tampermonkey-backed harvester.
    if args.refresh_geo:
        ok = asyncio.run(
            geo_harvester.refresh_geo_tx(headless=args.headless))
        print(f"refresh: {'OK' if ok else 'FAIL'}")
        sys.exit(0 if ok else 1)

    client = NovigClient()
    q = NovigQueries(client)

    def _label(ev: dict) -> str:
        g = ev.get("game") or {}
        away = (g.get("awayTeam") or {}).get("symbol")
        home = (g.get("homeTeam") or {}).get("symbol")
        if away and home:
            return f"{away} @ {home}"
        return ev.get("description") or f"<{ev.get('type','?')}>"

    if args.scrape_dump:
        # leagues=None → every Novig league in one round-trip
        # (tennis, UFC, PGA, all soccer comps, etc). The --league flag
        # is intentionally ignored here; use scrape_all_leagues(
        # leagues=(...)) directly from code if you need to scope.
        q.scrape_all_leagues(
            leagues=None,
            with_books=args.with_books,
            currency=args.currency,
            max_workers=args.workers,
            throttle_s=args.throttle,
            jitter_s=args.jitter,
            only_available=args.only_available,
        )
        sys.exit(0)

    if args.scan_sgp:
        import time as _t
        t0 = _t.time()
        rows = scan_sgp_implications(
            client, args.scan_sgp,
            max_workers=args.workers,
            throttle_s=args.throttle,
            jitter_s=args.jitter,
        )
        elapsed = _t.time() - t0
        print(f"\n(scan finished in {elapsed:.1f}s with {args.workers} workers, "
              f"throttle={args.throttle}+jitter≤{args.jitter}s)")
        if args.mispriced_only:
            rows = [r for r in rows if r["mispriced"]]
        print(f"\nScanned {len(rows)} SGP pairs (sorted by delta_vs_dominant, most negative first)")
        print(f"{'Player':22s} {'Pair':18s} {'Dom':>8s} {'Imp':>8s} "
              f"{'Synth':>8s} {'Naive':>9s} {'SWISH':>9s} "
              f"{'vsDom':>9s} {'vsNaive':>10s}  flag")
        print("-" * 125)
        for r in rows:
            side = "O" if r["dominant_side"].lower().startswith("o") else "U"
            pair = f"{side}:{r['dominant_type'][:3]}=>{r['implied_type'][:4]}"
            # Synth = the American odds the bettor effectively gets via the parlay.
            # Same winning condition as the dominant leg (since dominant => implied),
            # but priced at the parlay's better implied prob when mispriced.
            flag = "<<< MISPRICED" if r["mispriced"] else ""
            print(f"{r['player'][:22]:22s} {pair:18s} "
                  f"{r['american_dominant']:>8s} {r['american_implied']:>8s} "
                  f"{r['american_combined']:>8s} "
                  f"{r['naive_price']:>9.4f} {r['combined_price']:>9.4f} "
                  f"{r['delta_vs_dominant']:>+9.4f} {r['delta_vs_naive']:>+10.4f}  {flag}")
        sys.exit(0)

    if args.parlay:
        quote = client.price_parlay(args.parlay, boost_id=args.boost_id)
        s = summarize_parlay_quote(quote)
        print(f"\nParlay {s['parlay_id']}  status={s['status']}")
        print(f"  Combined: {s['combined_american']} ({s['combined_price']:.5f})")
        if s["naive_price"]:
            edge = s["correlation_edge"]
            sign = "+" if edge >= 0 else ""
            print(f"  Naive product (independent legs): {s['naive_price']:.5f}  "
                  f"correlation edge: {sign}{edge:.5f}")
        if s["unboosted_price"] is not None:
            print(f"  Unboosted: {s['unboosted_price']:.5f}")
        print(f"  Legs ({len(s['legs'])}):")
        for leg in s["legs"]:
            mkt = leg["market_description"] or ""
            print(f"    {leg['american']:>6s} ({leg['price']:.4f})  "
                  f"{leg['description']:30s}  [{mkt}]  vendor={leg['vendor']}")
        sys.exit(0)

    if args.book:
        books = client.get_market_books(args.book, currency=args.currency)
        for b in books:
            summary = summarize_book(b)
            mkt = b.get("market", {})
            player = (mkt.get("player") or {}).get("fullName") or ""
            print(f"\n{mkt.get('description','?')}  [{mkt.get('type')}] "
                  f"strike={mkt.get('strike')}  {player}")
            # Back = take an ask on this side (best_ask_*); Lay = take a bid (best_bid_*).
            def _fmt(price, size):
                if price is None:
                    return "--"
                return f"{fmt_american(price):>5s} ({price:.3f})  @{size:>8.2f}"
            for oid, info in summary["outcomes"].items():
                back = _fmt(info["best_ask_price"], info["best_ask_size"])
                lay  = _fmt(info["best_bid_price"], info["best_bid_size"])
                print(f"  {info['description']:14s}  BACK {back:32s}   LAY {lay}")
        sys.exit(0)

    if args.event:
        ev = q.get_event_markets(args.event, only_available=args.only_available)
        if not ev:
            print(f"No event found for id={args.event}")
            sys.exit(1)
        markets = NovigQueries.flatten_markets(ev)
        print(f"\nEvent: {_label(ev)}  ({ev['status']}, type={ev['type']})")
        print(f"Total markets across tree: {len(markets)}  "
              f"(filter={'website' if args.only_available else 'all'})")

        # Split: game-level vs player props. Group props by player+market_type.
        game_markets = [m for m in markets if not m.get("playerId")]
        prop_markets = [m for m in markets if m.get("playerId")]

        def _fmt_outcomes(outs: list) -> str:
            # last = last traded implied prob; available = best resting price on this side.
            # Show both as American(prob) pairs.
            def _pp(p):
                if p is None:
                    return "--"
                am = fmt_american(p)
                return f"{am}({p:.3f})" if am else f"({p:.3f})"
            parts = []
            for o in (outs or []):
                desc = o.get("description") or o.get("type") or "?"
                last = o.get("last")
                avail = o.get("available")
                parts.append(f"{desc} last={_pp(last)} avail={_pp(avail)}")
            return "  ".join(parts)

        print(f"\n  -- GAME MARKETS ({len(game_markets)}) --")
        for m in sorted(game_markets, key=lambda x: (x["type"], x.get("strike") or 0)):
            tag = "C" if m.get("is_consensus") else "B"
            strike = m.get("strike")
            print(f"    [{tag}] {m['type']:10s} strike={strike}  {_fmt_outcomes(m.get('outcomes'))}")

        if prop_markets:
            # group by description prefix (player name) → market type
            from collections import defaultdict
            by_player: dict = defaultdict(list)
            for m in prop_markets:
                # description is e.g. "Vladimir Guerrero Jr. 0.5 HOME_RUNS"
                desc = m.get("description") or ""
                # strip trailing " {strike} {TYPE}" to get player name
                name = desc
                for suffix in (f" {m.get('strike')} {m.get('type')}",):
                    if desc.endswith(suffix):
                        name = desc[: -len(suffix)]
                        break
                by_player[name].append(m)
            print(f"\n  -- PLAYER PROPS ({len(prop_markets)} markets, {len(by_player)} players) --")
            for player_name in sorted(by_player):
                pmkts = by_player[player_name]
                print(f"\n    {player_name}  ({len(pmkts)} markets)")
                for m in sorted(pmkts, key=lambda x: (x["type"], x.get("strike") or 0)):
                    tag = "C" if m.get("is_consensus") else "B"
                    print(f"      [{tag}] {m['type']:20s} {m.get('strike')}  {_fmt_outcomes(m.get('outcomes'))}")
        sys.exit(0)

    # default: list MLB events
    pregame = q.list_events(league=args.league, status_in=("OPEN_PREGAME",), limit=50)
    live = q.list_events(league=args.league, status_in=("OPEN_INGAME",), limit=50)
    print(f"\n{args.league} PREGAME ({len(pregame)}):")
    for ev in pregame[:25]:
        print(f"  {ev['scheduled_start']}  {_label(ev):40s}  id={ev['id']}")
    print(f"\n{args.league} LIVE ({len(live)}):")
    for ev in live:
        g = ev.get("game") or {}
        score = f"{g.get('away_score','-')}-{g.get('home_score','-')}"
        print(f"  {_label(ev):20s} {score:6s} {g.get('period','')} id={ev['id']}")


# ---------------------------------------------------------------------------
# Schema introspection (kept for reference; uncomment to re-run if Novig's
# GraphQL schema changes and you need to regenerate novig_schema.json).
#
# To re-enable:
#   1. Add `import pathlib` to the top of the file.
#   2. Uncomment the block below.
#   3. Add `--introspect` arg + handler back into the CLI section:
#        ap.add_argument("--introspect", action="store_true",
#                        help="Re-run schema introspection -> novig_schema.json")
#        if args.introspect:
#            print(f"Introspecting Novig schema -> {SCHEMA_PATH}")
#            _summarize_schema(NovigClient().introspect())
#            sys.exit(0)
# ---------------------------------------------------------------------------
#
# SCHEMA_PATH = pathlib.Path(__file__).parent / "novig_schema.json"
#
# INTROSPECTION_QUERY = """
# query IntrospectionQuery {
#   __schema {
#     queryType { name }
#     mutationType { name }
#     subscriptionType { name }
#     types { ...FullType }
#   }
# }
# fragment FullType on __Type {
#   kind name description
#   fields(includeDeprecated: true) {
#     name description
#     args { ...InputValue }
#     type { ...TypeRef }
#     isDeprecated deprecationReason
#   }
#   inputFields { ...InputValue }
#   interfaces { ...TypeRef }
#   enumValues(includeDeprecated: true) {
#     name description isDeprecated deprecationReason
#   }
#   possibleTypes { ...TypeRef }
# }
# fragment InputValue on __InputValue {
#   name description type { ...TypeRef } defaultValue
# }
# fragment TypeRef on __Type {
#   kind name
#   ofType { kind name
#     ofType { kind name
#       ofType { kind name
#         ofType { kind name
#           ofType { kind name
#             ofType { kind name
#               ofType { kind name } } } } } } }
# }
# """
#
# # Add as a method on NovigClient:
# def introspect(self, save_to=SCHEMA_PATH):
#     data = self.gql(INTROSPECTION_QUERY, operation_name="IntrospectionQuery")
#     save_to.write_text(json.dumps(data, indent=2))
#     return data
#
# def _summarize_schema(data):
#     schema = data.get("__schema", {})
#     types = schema.get("types", [])
#     user_types = [t for t in types if t.get("name") and not t["name"].startswith("__")]
#     by_kind = {}
#     for t in user_types:
#         by_kind.setdefault(t["kind"], []).append(t["name"])
#     print(f"\nSchema summary ({len(user_types)} user-facing types):")
#     for kind, names in sorted(by_kind.items()):
#         print(f"  {kind:12s} {len(names):4d}  e.g. {', '.join(sorted(names)[:6])}")
#     query_type_name = (schema.get("queryType") or {}).get("name")
#     qt = next((t for t in types if t.get("name") == query_type_name), None)
#     if qt and qt.get("fields"):
#         names = sorted(f["name"] for f in qt["fields"])
#         print(f"\nQuery root '{query_type_name}' exposes {len(names)} fields.")
#         print("  First 40:", ", ".join(names[:40]))
#     for needle in ("event", "market", "outcome", "game", "league"):
#         hits = [t["name"] for t in user_types if needle.lower() in t["name"].lower()]
#         if hits:
#             print(f"\nTypes matching '{needle}': {', '.join(sorted(hits)[:20])}")


# ===========================================================================
# Geo harvester
# ---------------------------------------------------------------------------
# Merged from the former novig_auth.py (cache + HTTP listener for the
# Tampermonkey userscript) and novig_auto_refresh.py (Selenium-driven
# refresh that places a small COIN bet to force a fresh PREWAGER
# attestation). Both files used to be standalone; this class is the
# single integration point now consumed by novig_async and EffortOdds.
# ===========================================================================


class NovigGeoHarvester:
    """One stop for Novig's PREWAGER geolocationTransactionId.

    Two halves:
      Cache + listener:
        - in-memory + on-disk JSON store of the latest tx
        - HTTP listener on 127.0.0.1:54321 that the Tampermonkey
          userscript (OddsAPI/novig_userscript.js) POSTs to whenever
          the GeoComply SDK mints or surfaces a tx
        - source-confidence ranking + overwrite protection so a noisy
          json-parse capture can't clobber a fresh proto-setter
      Refresh driver (on-demand, called when a /orders POST returns
      400 "Geolocation validation has expired"):
        - rsync-snapshot the user's live Firefox profile (so we don't
          fight the running browser's profile lock)
        - launch Firefox via Selenium against the snapshot
        - place a small COIN bet — the /orders POST forces the SDK to
          mint a fresh PREWAGER attestation, which the always-present
          userscript captures and POSTs back to our listener

    See the long comment above _place_coin_bet for *why* the refresh
    path bottoms out in a userscript instead of inline JS injection.
    """

    # --- cache / listener config ------------------------------------------
    _CACHE_PATH = pathlib.Path(__file__).parent / "novig_geo_cache.json"
    _LISTEN_HOST = "127.0.0.1"
    _LISTEN_PORT = 54321

    _SOURCE_CONFIDENCE = {
        # DEMOTED (2026-06-02): the proto-setter trap was ranked highest on
        # the theory it sees the value "assigned onto the outbound payload",
        # but live evidence shows the opposite — its captured value is
        # REJECTED by /orders ("Geolocation validation has expired"), and the
        # trap frequently fails to arm at all (armed=False). When it wins it
        # clobbers the good token and the placed bet 400s. Kept as a weak
        # last-resort signal only; it can no longer pass the freshness gate
        # (>=8) or overwrite a real capture.
        "proto-setter": 2,
        # SPA's actual outbound /orders body — gold standard. The GeoComply
        # geolocation RESULT (captured via json-parse) carries the identical
        # value and is elevated to this tier in _source_confidence().
        "fetch-req": 8, "xhr-req": 8,
        # SDK events / direct return values, ranked by reason code below.
        "sdk-emit": 7, "sdk-request": 7,
        # GeoComply engine response bodies.
        "fetch-resp": 6, "resp-json": 6, "xhr-resp": 5,
        "ws-msg": 4,
        "sdk-walk": 2,
        # Wide net — catches historical / cross-currency tokens too.
        "json-parse": 1,
        "plain": 5,  # back-compat with v1 userscript's plain-text body
    }
    # How long a high-confidence cache "protects" itself against lower-
    # confidence overwrites. PREWAGER tokens last hours; this just stops
    # a noisy json-parse capture (e.g. a cross-currency token landing
    # seconds later) from clobbering a fresh proto-setter.
    _OVERWRITE_PROTECTION_S = 120

    # --- refresh-driver config --------------------------------------------
    # NOTE: hard-coded Firefox profile path. If you ever delete the
    # profile, switch Firefox installs, or run this on a different box,
    # update this constant — _snapshot_profile() raises RuntimeError
    # ("live FF profile not found") if the path is missing. Tampermonkey
    # + novig_userscript.js MUST be installed and enabled inside this
    # profile; the snapshot copies it verbatim, so what's in your real
    # Firefox is what runs in the headless Selenium session.
    # To find your current path: `ls ~/.mozilla/firefox/ | grep default`.
    _LIVE_PROFILE = pathlib.Path(os.path.expanduser(
        "~/.mozilla/firefox/lumoar1n.default-release"))
    _SNAPSHOT = pathlib.Path(__file__).parent / "novig_ff_snapshot"
    # The harvester userscript, injected directly via Selenium during the
    # driven refresh. Tampermonkey does NOT reliably run userscripts under
    # a Marionette/geckodriver-launched Firefox (verified: none of its
    # hooks — proto-setter trap, fetch/JSON.parse wraps — are present on
    # the page), so we can't depend on the profile's extension to harvest.
    # Because refresh_geo_tx drives the bet itself, the fresh geolocation
    # is minted AFTER page load, so post-load injection still installs the
    # proto-setter trap + response hooks before our bet triggers it.
    _USERSCRIPT = pathlib.Path(__file__).parent / "novig_userscript.js"
    _NOVIG_URL = "https://novig.com/"
    _RSYNC_EXCLUDES = (
        "lock", ".parentlock", "parent.lock",
        "cache2/", "startupCache/", "safebrowsing/", "gmp-*/",
        "minidumps/", "crashes/", "datareporting/",
        "saved-telemetry-pings/", "shader-cache/", "cookies.sqlite-shm",
    )

    # --- in-page JS used by the refresh driver ----------------------------
    _DETECT_CURRENCY_JS = r"""
    const all = document.querySelectorAll(
        'div[style*="translateX(0px)"], '
        + 'div[style*="translateX(-18px)"]');
    for (const el of all) {
        if (!el.querySelector('[data-expoimage="true"]')) continue;
        return /translateX\(-/.test(el.getAttribute('style') || '')
            ? 'coin' : 'cash';
    }
    return null;
    """
    _TAG_COIN_TOGGLE_JS = r"""
    const all = document.querySelectorAll('div[style*="translateX(0px)"]');
    for (const el of all) {
        if (!el.querySelector('[data-expoimage="true"]')) continue;
        let cur = el.parentElement;
        for (let i = 0; i < 6 && cur; i++) {
            if (cur.getAttribute && cur.getAttribute('tabindex') === '0') {
                cur.setAttribute('data-novig-coin-toggle', '1');
                return true;
            }
            cur = cur.parentElement;
        }
    }
    return false;
    """

    def __init__(self):
        self._geo_cache: Optional[str] = None
        self._geo_meta: dict = {"source": None, "ts": None}
        self._lock = threading.Lock()
        self._refresh_done = threading.Event()
        self._refresh_done.set()
        self._callbacks: list = []
        self._listener_started = False

    # ====================================================================
    # Cache + listener half
    # ====================================================================

    def _log(self, msg: str) -> None:
        print(f"[novig_geo] {msg}", flush=True)

    def on_geo_refresh(self, cb: Callable[[], None]) -> None:
        """Register a callback fired (from the listener thread) whenever
        a fresh tx is received. Qt-side handlers must marshal to the
        main thread via a queued signal."""
        self._callbacks.append(cb)

    def _fire_callbacks(self) -> None:
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception as e:
                self._log(f"callback failed: {e}")

    def _load_from_disk(self) -> None:
        """Populate in-memory cache + meta from disk on first read."""
        if not self._CACHE_PATH.exists():
            return
        try:
            blob = json.loads(self._CACHE_PATH.read_text())
            v = blob.get("geo_tx_id")
            if isinstance(v, str) and v:
                self._geo_cache = v
                self._geo_meta = {
                    "source": blob.get("source"),
                    "ts": blob.get("received_at"),
                }
        except Exception:
            pass

    def get_cached_geo_tx(self) -> Optional[str]:
        """In-memory > disk > None. novig_async falls back to Creds when
        this returns None."""
        if self._geo_cache:
            return self._geo_cache
        self._load_from_disk()
        return self._geo_cache

    def get_cached_geo_meta(self) -> dict:
        """Return {tx, source, ts, age_s} or {} if no token cached.
        Used by UI to surface freshness."""
        if not self._geo_cache:
            self._load_from_disk()
        if not self._geo_cache:
            return {}
        ts = self._geo_meta.get("ts")
        age = int(time.time() - ts) if isinstance(ts, (int, float)) else None
        return {
            "tx": self._geo_cache,
            "source": self._geo_meta.get("source"),
            "ts": ts,
            "age_s": age,
        }

    def invalidate_geo_tx(self) -> None:
        """Drop the in-memory cache so the next read consults disk."""
        with self._lock:
            self._geo_cache = None

    def clear_geo_cache(self) -> None:
        """Purge the geo_tx everywhere — in-memory, metadata, and the disk
        file. Called once at app startup: a PREWAGER geolocationTransactionId
        is session-bound and a token persisted from a previous run is always
        stale, so dropping it forces the next place to mint a fresh one
        (400 → refresh_geo_tx) instead of replaying a token /orders rejects."""
        with self._lock:
            self._geo_cache = None
            self._geo_meta = {"source": None, "ts": None}
            try:
                if self._CACHE_PATH.exists():
                    self._CACHE_PATH.unlink()
                    self._log("cleared cached geo_tx (startup)")
            except Exception as e:
                self._log(f"clear_geo_cache failed: {e}")

    def _source_confidence(self, src: Optional[str]) -> int:
        if not src:
            return 0
        # Bonus tier: PREWAGER reason code (=2) — what /orders actually
        # validates. SDK-emit/request tags carry ":rc=2".
        if "rc=2" in src:
            return 10
        head = src.split("|", 1)[0].split(":", 1)[0]
        # A json-parse capture carrying a GeoComply PREWAGER geolocation
        # RESULT — shape {"transactionId":...,"success":...,"errorMessage":
        # ...,"geolocateInSession":...} — is the exact value the SPA sends to
        # /orders as geolocationTransactionId. Proven canonical: the driven
        # COIN bet places successfully with it. Elevate to the gold tier so
        # the freshness gate accepts it and proto-setter can't overwrite it.
        # Generic json-parse (page-hydration / cross-currency tokens) stays a
        # low wide-net catch, still guarded by the ts>=start_ts freshness gate.
        if (head == "json-parse" and "transactionId" in src
                and ("geolocateIn" in src or "errorMessage" in src)):
            return 9
        return self._SOURCE_CONFIDENCE.get(head, 1)

    def _set_geo_tx(self, geo_tx: str,
                    source: Optional[str] = None) -> None:
        """Stash a freshly-received tx in cache + disk + fire callbacks.
        Refuses to overwrite a fresher, higher-confidence cache so that
        a noisy json-parse (e.g. cross-currency token or historical
        /orders hydration) doesn't clobber a recent proto-setter or
        fetch-req capture."""
        now = int(time.time())
        new_conf = self._source_confidence(source)
        with self._lock:
            if geo_tx == self._geo_cache:
                # Refresh the timestamp on a duplicate so we know the
                # harvester is still alive. Keep the BETTER source label.
                old_conf = self._source_confidence(
                    self._geo_meta.get("source"))
                keep_src = (self._geo_meta.get("source")
                            if old_conf >= new_conf else source)
                self._geo_meta = {"source": keep_src, "ts": now}
                self._refresh_done.set()
                return
            old_conf = self._source_confidence(
                self._geo_meta.get("source"))
            old_ts = self._geo_meta.get("ts") or 0
            if (old_conf > new_conf
                    and (now - old_ts) < self._OVERWRITE_PROTECTION_S):
                self._log(
                    f"suppressing overwrite: new source={source!r} "
                    f"(conf={new_conf}) won't replace current "
                    f"source={self._geo_meta.get('source')!r} "
                    f"(conf={old_conf}, age={now - old_ts}s)")
                self._refresh_done.set()
                return
            self._geo_cache = geo_tx
            self._geo_meta = {"source": source, "ts": now}
        try:
            self._CACHE_PATH.write_text(json.dumps({
                "geo_tx_id": geo_tx,
                "received_at": now,
                "source": source,
            }, indent=2))
        except Exception as e:
            self._log(f"persist failed: {e}")
        self._refresh_done.set()
        self._fire_callbacks()
        self._log(f"received fresh geo_tx ({len(geo_tx)} chars, "
                  f"source={source})")

    def _parse_post_body(self, raw: bytes) -> tuple[Optional[str],
                                                    Optional[str]]:
        """Accept either a plain-text token body or a JSON envelope
        from the harvester. Returns (tx, source) or (None, None)."""
        if not raw:
            return None, None
        body = raw.decode("utf-8", "ignore").strip()
        if not body:
            return None, None
        # JSON envelope path
        if body[0] == "{":
            try:
                j = json.loads(body)
                tx = j.get("tx")
                source = j.get("source")
                if isinstance(tx, str) and tx:
                    return tx, (source if isinstance(source, str)
                                else None)
            except Exception:
                pass
        # Plain-text path (back-compat with v1 userscript)
        if 8 <= len(body) <= 256 and "\n" not in body:
            return body, "plain"
        return None, None

    def start_geo_listener(self, host: Optional[str] = None,
                           port: Optional[int] = None) -> None:
        """Start the local HTTP listener the harvester POSTs to.
        Idempotent — safe to call multiple times. Listener runs on a
        daemon thread so it doesn't block Qt shutdown."""
        if self._listener_started:
            return
        self._listener_started = True
        host = host or self._LISTEN_HOST
        port = port or self._LISTEN_PORT

        from http.server import BaseHTTPRequestHandler, HTTPServer
        outer = self

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type")

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                tx, source = outer._parse_post_body(raw)
                if tx:
                    outer._set_geo_tx(tx, source)
                else:
                    outer._log(f"POST body parsed to no tx ({length}B)")
                self.send_response(204)
                _cors(self)
                self.end_headers()

            def do_GET(self):
                if self.path.rstrip("/") in ("/status",
                                              "/geo/status"):
                    meta = outer.get_cached_geo_meta()
                    body = json.dumps({
                        "have": bool(meta),
                        "age_s": meta.get("age_s"),
                        "source": meta.get("source"),
                        "ts": meta.get("ts"),
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/json")
                    self.send_header("Content-Length",
                                     str(len(body)))
                    _cors(self)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                _cors(self)
                self.end_headers()

            def do_OPTIONS(self):
                self.send_response(204)
                _cors(self)
                self.end_headers()

            def log_message(self, *args, **kwargs):
                pass  # mute access log noise

        def _run():
            try:
                srv = HTTPServer((host, port), _Handler)
                outer._log(f"listening for geo_tx on "
                           f"http://{host}:{port}/geo "
                           f"(status: http://{host}:{port}/status)")
                srv.serve_forever()
            except Exception as e:
                outer._log(f"listener failed: {e}")

        threading.Thread(target=_run, name="novig-geo-listener",
                         daemon=True).start()

    async def force_refresh_geo_async(self,
                                      timeout: float = 20.0) -> bool:
        """Wait until a fresh tx lands in the cache (via harvester POST)
        or until `timeout` elapses. Returns True on fresh tx, False on
        timeout. Caller surfaces the original 400 to the UI on timeout.

        This is the *listener-only* wait — it does NOT drive a browser.
        Use refresh_geo_tx() for the full Selenium-driven refresh."""
        self.invalidate_geo_tx()
        self._refresh_done.clear()
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self._refresh_done.wait,
                                         timeout)
        if not ok:
            self._log("no fresh geo_tx within timeout — ensure "
                      "novig.com is open in your real Firefox with the "
                      "userscript harvester installed")
        return ok

    # ====================================================================
    # Refresh-driver half
    # ====================================================================

    def _snapshot_profile(self, force: bool = False) -> pathlib.Path:
        """rsync the live profile to a snapshot. Reuses existing
        snapshot if <5 min old. Safe to run while Firefox is open."""
        if not self._LIVE_PROFILE.exists():
            raise RuntimeError(
                f"live FF profile not found at {self._LIVE_PROFILE}")
        if self._SNAPSHOT.exists() and not force:
            if time.time() - self._SNAPSHOT.stat().st_mtime < 300:
                self._log(f"reusing snapshot (age "
                          f"{int(time.time() - self._SNAPSHOT.stat().st_mtime)}s)")
                return self._SNAPSHOT
        self._log(f"rsync {self._LIVE_PROFILE} -> {self._SNAPSHOT}")
        self._SNAPSHOT.mkdir(exist_ok=True)
        cmd = ["rsync", "-a", "--delete"]
        for ex in self._RSYNC_EXCLUDES:
            cmd.append(f"--exclude={ex}")
        cmd += [str(self._LIVE_PROFILE) + "/",
                str(self._SNAPSHOT) + "/"]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode not in (0, 23, 24):
            raise RuntimeError(f"rsync failed rc={r.returncode}: "
                               f"{r.stderr[:400]}")
        self._log(f"snapshot ready in {time.time() - t0:.1f}s")
        for fn in ("lock", ".parentlock", "parent.lock"):
            p = self._SNAPSHOT / fn
            if p.exists() or p.is_symlink():
                try: p.unlink()
                except Exception: pass
        return self._SNAPSHOT

    def _launch_firefox(self, headless: bool = False):
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        opts = Options()
        opts.add_argument("-profile")
        opts.add_argument(str(self._SNAPSHOT))
        opts.set_preference("dom.webdriver.enabled", False)
        opts.set_preference("permissions.default.geo", 1)
        if headless:
            opts.add_argument("--headless")
            opts.add_argument("--width=1920")
            opts.add_argument("--height=1080")
        self._log(f"launching Firefox (headless={headless}) ...")
        driver = webdriver.Firefox(options=opts)
        driver.set_page_load_timeout(60)
        if not headless:
            try: driver.set_window_size(1920, 1080)
            except Exception: pass
        return driver

    def _inject_harvester(self, driver) -> bool:
        """Inject the harvester userscript into the live page via
        Selenium. Stands in for Tampermonkey (which doesn't run under
        geckodriver). Idempotent within a page: the script's own hooks
        guard against double-wrapping. Must be called AFTER the page is
        ready but BEFORE we click the outcome, so the proto-setter trap
        and Response/JSON.parse/XHR hooks are armed before our bet mints
        the fresh PREWAGER geolocationTransactionId."""
        try:
            js = self._USERSCRIPT.read_text()
        except Exception as e:
            self._log(f"could not read userscript for injection: {e}")
            return False
        try:
            driver.execute_script(js)
            # Confirm the proto-setter trap actually landed — it's the
            # highest-confidence capture path and the clearest signal the
            # injection took.
            armed = driver.execute_script(
                "return !!Object.getOwnPropertyDescriptor("
                "Object.prototype, 'geolocationTransactionId');")
            self._log(f"harvester injected (proto-setter trap "
                      f"armed={bool(armed)})")
            return True
        except Exception as e:
            self._log(f"harvester injection failed: {e}")
            return False

    # --- Why we use the userscript+listener pattern instead of an
    #     inline Selenium-injected hook -----------------------------------
    #
    # The capture mechanism that works for /orders is a setter trap on
    # Object.prototype.geolocationTransactionId — the SPA assigns the
    # freshly-minted PREWAGER tx onto its outbound payload object, and
    # the trap fires at the moment of assignment (source label
    # "proto-setter", highest confidence in _SOURCE_CONFIDENCE).
    #
    # That trap MUST be installed at document-start, before any SPA
    # code touches Object.prototype or caches a reference to
    # window.fetch. From Selenium-driven Firefox we cannot do that:
    #   - driver.execute_script only runs once the document is loaded;
    #     by then the GeoComply SDK and the React bundle have already
    #     grabbed their fetch references and (in our experiments) the
    #     SPA's own Object.prototype writes have already happened.
    #   - Firefox-Marionette has no equivalent of Chrome DevTools'
    #     Page.addScriptToEvaluateOnNewDocument. Selenium 4's BiDi
    #     script.add_preload_script has spotty Firefox support and is
    #     not reliable here.
    #   - When we tried to inject the same hook post-load, two failure
    #     modes appeared: (1) wrapping fetch with `orig.apply(this,
    #     args)` throws "'fetch' called on an object that does not
    #     implement interface Window" and breaks every subsequent SPA
    #     network call; (2) even after binding fetch correctly, our
    #     Object.prototype defineProperty races against Tampermonkey's
    #     already-installed setter — defineProperty either silently
    #     no-ops (if TM's setter was non-configurable) or replaces
    #     TM's, but by then the SPA has captured its own references
    #     and the setter never fires.
    #
    # So the architecture is: novig_userscript.js (installed in the
    # user's real Firefox profile via Tampermonkey) runs at
    # document-start every time novig.com loads, regardless of whether
    # the load is user-driven or Selenium-driven (because we launch
    # Selenium against a SNAPSHOT of that same profile, so the
    # extension + its userscript come along). When it sees a tx, it
    # POSTs to http://localhost:54321/geo. start_geo_listener caches
    # it. refresh_geo_tx polls that cache until a high-confidence,
    # post-start_ts capture lands.

    def _place_coin_bet(self, driver, amount: str = "10",
                        cta_timeout: int = 120,
                        place_bet: bool = True) -> dict:
        # place_bet=False: drive only as far as the geolocation completing
        # (CTA flips "Retry Geolocation" → "Place Order"), then STOP without
        # clicking. The GeoComply geo result — and thus the geo_tx — is minted
        # during the "verifying" phase, before the click, so the token is
        # already captured. Not clicking leaves it UNCONSUMED so the real
        # aiohttp bet can spend it (PREWAGER: geolocate, then attach to a
        # wager). place_bet=True keeps the old behaviour (places a COIN bet,
        # which spends the token — only useful for proving the SPA path).
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import (
            TimeoutException, ElementClickInterceptedException,
            ElementNotInteractableException)
        log = []
        t0 = time.time()
        def push(m): log.append(f"+{int((time.time()-t0)*1000)}ms {m}")
        def fail(step):
            return {"ok": False, "step": step, "detail": log}

        def safe_click(el, label):
            """Selenium click first (trusted-event, required for
            RN-Web Pressables). If a sticky overlay (ticker tape,
            toast, etc.) intercepts at the original coords, scroll the
            element to the viewport center and retry. As a last
            resort, dispatch a JS MouseEvent — works on Pressables
            that don't require trust."""
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', "
                "inline:'center'});", el)
            time.sleep(0.15)
            try:
                el.click()
                push(f"clicked {label} (selenium)")
                return True
            except (ElementClickInterceptedException,
                    ElementNotInteractableException) as e:
                push(f"  {label}: intercepted/non-interactable, "
                     f"retrying ({type(e).__name__})")
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});",
                    el)
                time.sleep(0.2)
                el.click()
                push(f"clicked {label} (selenium-retry)")
                return True
            except (ElementClickInterceptedException,
                    ElementNotInteractableException):
                pass
            try:
                driver.execute_script(r"""
                    const el = arguments[0];
                    const r = el.getBoundingClientRect();
                    const cx = r.left + r.width/2;
                    const cy = r.top + r.height/2;
                    for (const t of ['mousedown','mouseup','click']) {
                        el.dispatchEvent(new MouseEvent(t, {
                            bubbles: true, cancelable: true,
                            view: window,
                            clientX: cx, clientY: cy, button: 0,
                        }));
                    }
                """, el)
                push(f"clicked {label} (js-dispatch fallback)")
                return True
            except Exception as e:
                push(f"  {label}: js-dispatch failed: {e}")
                return False

        # 1. Switch to COIN if needed
        cur = driver.execute_script("return (function(){"
                                     + self._DETECT_CURRENCY_JS + "})()")
        if cur == "cash":
            if not driver.execute_script(
                    "return (function(){"
                    + self._TAG_COIN_TOGGLE_JS + "})()"):
                return fail("find-coin-toggle")
            toggle_el = driver.find_element(
                By.CSS_SELECTOR, '[data-novig-coin-toggle="1"]')
            if not safe_click(toggle_el, "COIN toggle"):
                return fail("click-coin-toggle")
            try:
                WebDriverWait(driver, 5).until(
                    lambda d: d.execute_script(
                        "return (function(){"
                        + self._DETECT_CURRENCY_JS + "})()") == "coin")
                push("COIN mode confirmed")
            except TimeoutException:
                return fail("switch-to-coin")
        elif cur == "coin":
            push("already in COIN mode")

        # 2. Pick first MONEY outcome inside a NON-LIVE event card,
        # directly on /events. Clicking opens the bet slip drawer
        # with the CTA already mounted; no detail-page navigation.
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'div[data-testid^="outcome-"]')))
        except TimeoutException:
            return fail("find-outcome")

        pick_js = r"""
        const cards = document.querySelectorAll('[data-testid^="event-card-"]');
        let chosen = null;
        let chosenTid = null;
        let chosenCardTitle = null;
        for (const card of cards) {
            const txt = (card.innerText || '').toLowerCase();
            if (txt.includes('live')) continue;
            const outcomes = card.querySelectorAll(
                'div[data-testid^="outcome-"]');
            if (!outcomes.length) continue;
            let money = null;
            for (const o of outcomes) {
                const tid = o.getAttribute('data-testid') || '';
                if (tid.includes('MONEY')) { money = o; break; }
            }
            chosen = money || outcomes[0];
            chosenTid = chosen.getAttribute('data-testid');
            chosenCardTitle = (card.innerText || '').split('\n')[0];
            chosen.setAttribute('data-novig-pick', '1');
            break;
        }
        return chosen
            ? {tid: chosenTid, title: chosenCardTitle}
            : null;
        """
        pick = driver.execute_script("return (function(){"
                                      + pick_js + "})()")
        if not pick:
            return fail("no-non-live-outcome")
        push(f"picked outcome: {pick.get('tid')!r} in card: "
             f"{(pick.get('title') or '')[:60]!r}")
        chosen = driver.find_element(
            By.CSS_SELECTOR, '[data-novig-pick="1"]')
        if not safe_click(chosen, "outcome on /events"):
            return fail("click-outcome")

        # 2b. Wait for the slip drawer to mount. Three slip facts the
        # rest of this function depends on (discovered via a DOM dump
        # that has since been removed):
        #   - no `orderslip-cta` testid exists on /events; the CTA is
        #     a bare <div> identified only by its visible text
        #   - the geo-verifying signal is a top-level
        #     <div role="progressbar"> (NOT a child of the CTA),
        #     paired with the CTA text starting with "Retry"
        #   - the amount field is NOT an <input> — it's a focusable
        #     <div tabindex="0" data-testid="order-amount-input-<UUID>">
        #     React-Native-Web shim, typed via ActionChains.send_keys
        try:
            WebDriverWait(driver, 5).until(
                lambda d: d.execute_script(
                    "return document.querySelectorAll('[role=\"dialog\"], "
                    "[data-testid*=\"slip\"], [data-testid*=\"order\"]')"
                    ".length > 0"))
            push("slip drawer mounted")
        except TimeoutException:
            push("slip drawer not detected in 5s — continuing anyway")

        # 3. Locate slip CTA by text ("Retry Geolocation" while
        # verifying, "Place Order" once ready). Tag the clickable
        # ancestor for stable referencing across re-renders.
        tag_cta_js = r"""
        const RX = /^(retry\s*geolocation|place\s*order)/i;
        let hit = null;
        for (const el of document.querySelectorAll('div,span,button')) {
            const t = (el.innerText || '').trim();
            if (!t || t.length > 40) continue;
            if (!RX.test(t)) continue;
            if (!hit || el.contains(hit) === false) hit = el;
        }
        if (!hit) return null;
        let target = hit;
        let cur = hit;
        for (let i = 0; i < 8 && cur; i++) {
            const ti = cur.getAttribute && cur.getAttribute('tabindex');
            const role = cur.getAttribute && cur.getAttribute('role');
            if (ti === '0' || role === 'button'
                    || (cur.getAttribute
                        && cur.getAttribute('data-focusable') === 'true')) {
                target = cur;
                break;
            }
            cur = cur.parentElement;
        }
        target.setAttribute('data-novig-cta', '1');
        hit.setAttribute('data-novig-cta-text', '1');
        return {text: hit.innerText.trim()};
        """
        try:
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script(
                    "return (function(){" + tag_cta_js + "})()")
                is not None)
        except TimeoutException:
            return fail("find-cta-text")
        push("slip CTA found by text")

        def cta_state():
            """Returns (state, text). state='verifying' iff any
            visible role=progressbar is up OR the CTA still reads
            "Retry"; else 'ready'."""
            info = driver.execute_script(
                "return (function(){" + tag_cta_js + "})()")
            if not info:
                return None, None
            txt = (info.get("text") or "").strip()
            verifying = driver.execute_script(r"""
                const bars = document.querySelectorAll('[role="progressbar"]');
                for (const b of bars) {
                    const r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return true;
                }
                return false;
            """)
            if verifying or txt.lower().startswith("retry"):
                return "verifying", txt
            return "ready", txt

        def wait_cta_ready(label):
            deadline = time.time() + cta_timeout
            last_state = None
            last_log = 0.0
            while time.time() < deadline:
                st, txt = cta_state()
                if st == "ready":
                    push(f"{label}: CTA ready ({txt[:50]!r})")
                    return True
                now = time.time()
                if now - last_log >= 1.0 and (st, txt) != last_state:
                    push(f"  {label}: state={st!r} "
                         f"text={(txt or '')[:50]!r}")
                    last_state = (st, txt)
                    last_log = now
                time.sleep(0.25)
            st, txt = cta_state()
            push(f"{label} timeout: state={st!r} "
                 f"text={(txt or '')[:60]!r}")
            return False

        # 4. Type amount into the slip's shim input.
        amt_sel = '[data-testid^="order-amount-input-"]'
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, amt_sel)))
        except TimeoutException:
            push("no amount input within 10s — "
                 "proceeding with slip default")
        else:
            try:
                from selenium.webdriver.common.action_chains import (
                    ActionChains)
                amt_el = driver.find_element(By.CSS_SELECTOR, amt_sel)
                if safe_click(amt_el, "amount field"):
                    time.sleep(0.15)
                    ActionChains(driver).send_keys(
                        str(amount)).perform()
                    shown = driver.execute_script(r"""
                        const el = document.querySelector(arguments[0]);
                        if (!el) return null;
                        const t = el.querySelector('[data-testid="Text"]');
                        return t ? (t.innerText || '').trim() : null;
                    """, amt_sel)
                    push(f"typed {amount} (shim shows {shown!r})")
                else:
                    push("amount field click failed — slip default")
            except Exception as e:
                push(f"amount entry failed: {e}")

        # 5. Conditional wait — up to cta_timeout (default 120s) for
        # the spinner to clear AND the CTA text to flip past "Retry".
        if not wait_cta_ready("post-amount"):
            return fail("cta-never-ready")

        # 6. Geolocation has completed (CTA is "Place Order"), so the geo_tx
        # is already minted + captured. In geolocate-only mode, stop here so
        # the token stays unconsumed for the real bet.
        if not place_bet:
            push("geolocated — skipping Place Order (token left unconsumed)")
            return {"ok": True, "step": "geolocated", "detail": log,
                    "elapsed_ms": int((time.time() - t0) * 1000)}

        # 7. Click Place Order on the tagged CTA.
        cta_el = driver.find_element(
            By.CSS_SELECTOR, '[data-novig-cta="1"]')
        if not safe_click(cta_el, "Place Order"):
            return fail("click-place")

        return {"ok": True, "step": "placed", "detail": log,
                "elapsed_ms": int((time.time() - t0) * 1000)}

    async def refresh_geo_tx(self, timeout_s: float = 30.0,
                             headless: bool = False,
                             amount: str = "10",
                             place_bet: bool = True) -> bool:
        """Drive the SPA through geolocation so the (Selenium-injected)
        harvester captures a fresh PREWAGER geo_tx and POSTs it to the
        local listener. Returns True iff a high-confidence, post-start_ts
        capture lands in the cache within `timeout_s`.

        place_bet=False (preferred): stop once geolocation completes, WITHOUT
        placing the COIN bet, so the harvested token stays unconsumed and the
        real aiohttp bet can spend it. place_bet=True (default, legacy): also
        clicks Place Order — spends the token on a COIN bet, so the harvested
        token is single-use-consumed and gets rejected on replay.

        Note: Tampermonkey does NOT run under geckodriver, so we inject
        the harvester ourselves (see _inject_harvester) after page load
        and before driving the slip."""
        self.start_geo_listener()
        # Snapshot BEFORE invalidating: invalidate_geo_tx() clears
        # in-mem only, then the next get_cached_geo_meta() reloads off
        # disk, which would resurrect a stale token from a prior run
        # with its old source label intact. Comparing ts >= start_ts
        # is the real freshness gate.
        start_ts = int(time.time())
        self.invalidate_geo_tx()
        loop = asyncio.get_running_loop()

        def is_fresh_high_conf(meta: dict) -> bool:
            if not meta:
                return False
            ts = meta.get("ts") or 0
            if ts < start_ts:
                return False
            # Accept any capture whose confidence is at least that of the
            # SPA's actual outbound /orders body (conf 8): xhr-req/fetch-req
            # (8), the GeoComply geolocation RESULT via json-parse (elevated
            # to 9 in _source_confidence), and any rc=2 PREWAGER tag (10).
            # The SPA POSTs /orders via XHR, not fetch, so xhr-req — not
            # fetch-req — is what fires; all carry the identical accepted
            # token. proto-setter (now 2) is intentionally excluded — its
            # value is rejected by /orders.
            src = meta.get("source") or ""
            return self._source_confidence(src) >= 8

        def wait_for_page_ready(driver, timeout: float = 15.0) -> bool:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            try:
                WebDriverWait(driver, timeout).until(
                    lambda d: d.execute_script(
                        "return document.readyState")
                    in ("interactive", "complete"))
                WebDriverWait(driver, timeout).until(
                    lambda d: d.find_elements(
                        By.CSS_SELECTOR,
                        'div[data-testid^="outcome-"], '
                        '[data-testid^="event-card-"]'))
                return True
            except Exception as e:
                self._log(f"page-ready wait failed: {e}")
                return False

        def blocking():
            self._snapshot_profile()
            driver = self._launch_firefox(headless=headless)
            try:
                self._log(f"navigating to {self._NOVIG_URL}")
                driver.get(self._NOVIG_URL)
                if not wait_for_page_ready(driver):
                    self._log("page never became ready; aborting")
                    return False
                # Tampermonkey won't run under geckodriver, so inject the
                # harvester ourselves before triggering the geolocation.
                self._inject_harvester(driver)
                self._log(f"driving slip to geolocation "
                          f"(amount={amount}, place_bet={place_bet}) ...")
                r = self._place_coin_bet(driver, amount=amount,
                                         place_bet=place_bet)
                self._log(f"place: ok={r['ok']} step={r['step']!r} "
                          f"elapsed_ms={r.get('elapsed_ms')}")
                for line in r["detail"]:
                    self._log(f"  {line}")
                if not r["ok"]:
                    return False
                self._log(f"awaiting fresh geo_tx (start_ts={start_ts}, "
                          f"timeout={timeout_s}s) ...")
                deadline = time.time() + timeout_s
                last_log = 0.0
                while time.time() < deadline:
                    meta = self.get_cached_geo_meta() or {}
                    if is_fresh_high_conf(meta):
                        self._log(f"captured fresh geo_tx via "
                                  f"{meta.get('source')!r} "
                                  f"(age={meta.get('age_s')}s, "
                                  f"ts={meta.get('ts')})")
                        return True
                    now = time.time()
                    if now - last_log >= 2.0:
                        self._log(f"  waiting... cache source="
                                  f"{meta.get('source')!r} "
                                  f"ts={meta.get('ts')} "
                                  f"(start_ts={start_ts})")
                        last_log = now
                    time.sleep(0.5)
                meta = self.get_cached_geo_meta() or {}
                self._log(f"timed out: final source="
                          f"{meta.get('source')!r} "
                          f"ts={meta.get('ts')} "
                          f"(start_ts={start_ts})")
                return False
            finally:
                try: driver.quit()
                except Exception: pass

        return await loop.run_in_executor(None, blocking)


# Module-level singleton — keeps the single-cache, single-listener
# semantics the old module-level novig_auth had. Callers should import
# this rather than constructing their own NovigGeoHarvester unless
# they need test isolation.
geo_harvester = NovigGeoHarvester()


if __name__ == "__main__":
    _main_cli()