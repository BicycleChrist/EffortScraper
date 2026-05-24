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

import json
import pathlib
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Optional

import requests

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
        return r.json()

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

    def list_events(self, league: str = "MLB",
                    status_in: tuple[str, ...] = ("OPEN_PREGAME", "OPEN_INGAME"),
                    visible_only: bool = True,
                    limit: int = 200) -> list[dict]:
        """List top-level events for a league. Filters out child/sub events."""
        where: dict = {
            "league": {"_eq": league},
            "status": {"_in": list(status_in)},
            "parent_event_id": {"_is_null": True},  # top-level only
        }
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
            save: if True, writes <dump_dir>/all_events_combined_<ts>.json.
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
                           leagues: tuple[str, ...] = (
                               "MLB", "NBA", "NHL", "NFL",
                               "NCAAF", "NCAAB", "WNBA", "EPL"),
                           with_books: bool = False,
                           currency: str = "CASH",
                           max_workers: int = 4,
                           throttle_s: float = 0.25,
                           jitter_s: float = 0.20,
                           only_available: bool = False,
                           save: bool = True,
                           dump_dir: Optional[pathlib.Path] = None,
                           progress: bool = True) -> dict:
        """Same as scrape_all_mlb but iterates a list of leagues. Output
        dump shape is identical (event_metadata.tournament carries the
        league code), so LiquidityWidget consumes it unchanged."""
        dump_dir = dump_dir or NOVIG_DUMP_DIR

        events: list[dict] = []
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
                eid, entry = fut.result()
                if entry is None:
                    if progress:
                        print(f"  [{i}/{len(unique_events)}] {eid} (empty)")
                    continue
                dump[eid] = entry
                if progress:
                    n_mkts = len((entry.get("data") or {}).get("markets") or [])
                    print(f"  [{i}/{len(unique_events)}] "
                          f"{entry['event_metadata']['name']:30s} "
                          f"({n_mkts} markets)")

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
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = dump_dir / f"all_events_combined_{ts}.json"
            out.write_text(json.dumps(dump, indent=2, default=str))
            if progress:
                size_mb = out.stat().st_size / (1024 * 1024)
                print(f"[novig.scrape] wrote {out}  ({size_mb:.2f} MB)")
        return dump

    def _fetch_one_event_for_dump(self, event_id: str,
                                  only_available: bool,
                                  throttle_s: float, jitter_s: float
                                  ) -> tuple[str, Optional[dict]]:
        """Worker for scrape_all_mlb. Returns (event_id, dump_entry|None).
        Always sleeps post-request to throttle the aggregate rate."""
        try:
            ev = self.get_event_markets(event_id, only_available=only_available)
        except NovigError:
            ev = None
        finally:
            time.sleep(throttle_s + random.uniform(0.0, jitter_s))
        if not ev:
            return event_id, None
        return event_id, _event_to_dump_entry(ev)

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
        return json.loads(files[-1].read_text())


def _event_to_dump_entry(event_node: dict) -> dict:
    """Build the per-event dump entry. Mirrors ProphetX's
    {event_metadata, data:{markets}} shape so the widget's loader can
    consume both sources via one code path."""
    game = event_node.get("game") or {}
    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    meta = {
        "id": event_node.get("id") or "",
        "name": event_node.get("description") or "",
        "startTime": event_node.get("scheduled_start") or "",
        "status": event_node.get("status") or "",
        "tournament": event_node.get("league") or game.get("league") or "",
        "sport": game.get("sport") or "Baseball",
        # Novig has no per-event total volume on the GraphQL surface;
        # 0 keeps stake-sort downstream sane.
        "stake": 0.0,
        "home_team_symbol": home.get("symbol"),
        "home_team_name": home.get("name"),
        "away_team_symbol": away.get("symbol"),
        "away_team_name": away.get("name"),
        "game_status": game.get("status"),
        "game_period": game.get("period"),
        "home_score": game.get("home_score"),
        "away_score": game.get("away_score"),
    }
    return {
        "event_metadata": meta,
        "data": {"markets": NovigQueries.flatten_markets(event_node)},
    }


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


if __name__ == "__main__":
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
    ap.add_argument("--with-books", action="store_true",
                    help="With --scrape-dump, also fetch /book/batch ladders "
                         "per event (slower; widget can lazy-fetch otherwise).")
    args = ap.parse_args()

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
        q.scrape_all_mlb(
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
