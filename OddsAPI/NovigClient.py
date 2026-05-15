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
from typing import Any, Optional

import requests

try:
    from Creds import NOVIG_AUTH_TOKEN
except ImportError:
    NOVIG_AUTH_TOKEN = ""


NOVIG_GRAPHQL_URL = "https://api.novig.us/v1/graphql"
NOVIG_NBX_BASE = "https://api.novig.us/nbx/v1"

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
