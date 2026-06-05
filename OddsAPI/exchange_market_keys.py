#!/usr/bin/env python3
"""
Exchange Market Keys — source-agnostic orderbook schema + cross-source
matcher.

Two responsibilities in one file:

1. **Schema + adapters.** A five-level hierarchy
   (Event -> Market -> Line -> Side -> Order) plus canonical type/subtype
   tokens. ProphetX and Novig payloads are translated into this shape so
   downstream UI/analytics code consumes a single structure regardless
   of which book the data came from.

2. **Cross-source matcher.** Pairs NormalizedEvents, NormalizedMarkets,
   and NormalizedLines across exchanges (initially ProphetX <-> Novig
   for MLB) so the LiquidityWidget can show both books for the same
   logical market side by side.

Five-level hierarchy:
    Event  ->  Market  ->  Line  ->  Side  ->  Order

- Event   : a game / contest / future on a specific exchange.
- Market  : the *displayed* unit (e.g. "Total Runs", "Moneyline",
            "Aaron Judge Home Runs"). One Market can contain multiple
            strikes / lines.
- Line    : a single strike of that market. Moneyline markets have
            exactly one Line with strike=None. Spread/Total/PlayerProp
            markets carry many lines.
- Side    : one of the two opposing outcomes at that line
            (Over/Under, team_a/team_b, Yes/No).
- Order   : one resting price level on that side.

Adapters:
    from_prophetx_market(market_dict, event_meta) -> NormalizedMarket
        ProphetX bundles strikes natively under `marketLines[]`. One
        ProphetX market maps to one NormalizedMarket directly.

    from_prophetx_event(event_dump) -> NormalizedEvent
        Walks every market in a ProphetX event dump and produces a
        single NormalizedEvent.

    from_novig_event(event_node, books=None, currency="CASH")
                                                -> NormalizedEvent
        Novig stores each strike as an independent market UUID. This
        adapter aggregates same-type markets back into one
        NormalizedMarket with N NormalizedLines so the renderer sees
        the same multi-strike ladder shape as ProphetX.

The normalizer is network-side-effect free. Callers pass already-fetched
payloads (a ProphetX dump entry, a Novig get_event_markets() response,
optionally a list of /book/batch results keyed by market_id).

Schema invariants:
- `prob` is implied probability in (0, 1).
- `american` is signed int (favorites negative, dogs positive).
- `size_usd` is the displayed dollar size.
    * ProphetX:   raw `value` (already USD)
    * Novig:      qty / 100  (NBX qty scale; CASH and COIN both use 100)
- Within a Side, orders sort by American odds descending — matching the
  existing LiquidityWidget convention (most-generous payout first).
- Within a Market, lines sort by strike descending. Renderers that want
  the ProphetX-style "over: descending strikes, under: ascending strikes"
  display can reverse the line list for the Under half at render time.
- Moneyline markets ALWAYS have strike=None on their single line,
  regardless of what the source payload contains.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Optional


# ---------------------------------------------------------------------------
# Odds conversion (no NovigClient import — keeps this module standalone)
# ---------------------------------------------------------------------------

def prob_to_american(p: Optional[float]) -> Optional[int]:
    """Implied prob in (0,1) -> signed American int. None for invalid."""
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


def american_to_prob(a: Optional[int]) -> Optional[float]:
    """Signed American int -> implied prob in (0,1). None for invalid."""
    if a is None:
        return None
    try:
        a = int(a)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if a > 0:
        return 100.0 / (a + 100.0)
    return (-a) / ((-a) + 100.0)


# ---------------------------------------------------------------------------
# Enums (string constants — keep stable; downstream code compares against them)
# ---------------------------------------------------------------------------

SOURCE_PROPHETX = "prophetx"
SOURCE_NOVIG_CASH = "novig:CASH"
SOURCE_NOVIG_COIN = "novig:COIN"

MTYPE_MONEYLINE = "MONEYLINE"
MTYPE_SPREAD = "SPREAD"
MTYPE_TOTAL = "TOTAL"
MTYPE_PLAYER_PROP = "PLAYER_PROP"
MTYPE_OTHER = "OTHER"

SIDE_OVER = "over"
SIDE_UNDER = "under"
SIDE_YES = "yes"
SIDE_NO = "no"
SIDE_TEAM = "team"
SIDE_UNKNOWN = "unknown"


# Canonical prop subtype tokens used across sources. Both adapters map
# their native naming into these so the matcher can compare directly.
PROP_HOME_RUNS = "HOME_RUNS"
PROP_HITS = "HITS"
PROP_RBIS = "RBIS"
PROP_RUNS = "RUNS"
PROP_TOTAL_BASES = "TOTAL_BASES"
PROP_HRR = "HITS_RUNS_RBIS"            # Novig: HITS_RUNS_RBIS, PX: "Hits + Runs + RBIs"
PROP_PITCHER_STRIKEOUTS = "PITCHER_STRIKEOUTS"
PROP_PITCHER_OUTS = "PITCHER_OUTS"
PROP_PITCHER_HITS_ALLOWED = "HITS_ALLOWED"
PROP_PITCHER_EARNED_RUNS = "EARNED_RUNS"
PROP_STOLEN_BASES = "STOLEN_BASES"
PROP_STRIKEOUTS = "STRIKEOUTS"          # batter side
PROP_WALKS = "WALKS"

# NBA / basketball props
PROP_POINTS = "POINTS"
PROP_REBOUNDS = "REBOUNDS"
PROP_ASSISTS = "ASSISTS"
PROP_THREES = "THREE_POINTERS_MADE"
PROP_STEALS = "STEALS"
PROP_BLOCKS = "BLOCKS"
PROP_TURNOVERS = "TURNOVERS"
PROP_STEALS_BLOCKS = "STEALS_BLOCKS"
PROP_PRA = "POINTS_REBOUNDS_ASSISTS"
PROP_PR = "POINTS_REBOUNDS"
PROP_PA = "POINTS_ASSISTS"
PROP_RA = "REBOUNDS_ASSISTS"
PROP_DOUBLE_DOUBLE = "DOUBLE_DOUBLE"
PROP_TRIPLE_DOUBLE = "TRIPLE_DOUBLE"
PROP_FIRST_BASKET = "FIRST_BASKET"

# NHL / hockey props
PROP_GOALS = "GOALS"
PROP_HOCKEY_ASSISTS = "HOCKEY_ASSISTS"
PROP_SHOTS_ON_GOAL = "SHOTS_ON_GOAL"
PROP_SAVES = "SAVES"
PROP_POWER_PLAY_POINTS = "POWER_PLAY_POINTS"

# NFL / football props
PROP_PASS_YDS = "PASSING_YARDS"
PROP_PASS_TDS = "PASSING_TOUCHDOWNS"
PROP_RUSH_YDS = "RUSHING_YARDS"
PROP_REC_YDS = "RECEIVING_YARDS"
PROP_RECEPTIONS = "RECEPTIONS"
PROP_ANYTIME_TD = "ANYTIME_TOUCHDOWN"

# ProphetX `categoryName` -> canonical prop subtype. Anything not in the
# map falls through as MTYPE_OTHER or MTYPE_PLAYER_PROP with raw subtype.
_PROPHETX_PROP_CATEGORY_MAP = {
    "home runs": PROP_HOME_RUNS,
    "hits": PROP_HITS,
    "rbis": PROP_RBIS,
    "runs": PROP_RUNS,
    "total bases": PROP_TOTAL_BASES,
    "hits + runs + rbis": PROP_HRR,
    "strikeouts": PROP_PITCHER_STRIKEOUTS,   # ProphetX bundles pitcher Ks
    "pitcher outs": PROP_PITCHER_OUTS,
    "hits allowed": PROP_PITCHER_HITS_ALLOWED,
    "earned runs": PROP_PITCHER_EARNED_RUNS,
    "stolen bases": PROP_STOLEN_BASES,
    "walks": PROP_WALKS,
    # NBA — ProphetX uses short abbreviated category names
    "points":           PROP_POINTS,
    "rebounds":         PROP_REBOUNDS,
    "assists":          PROP_ASSISTS,
    "threes":           PROP_THREES,
    "steals":           PROP_STEALS,
    "blocks":           PROP_BLOCKS,
    "turnovers":        PROP_TURNOVERS,
    "stl + blk":        PROP_STEALS_BLOCKS,
    "pts + reb + ast":  PROP_PRA,
    "pts + reb":        PROP_PR,
    "pts + ast":        PROP_PA,
    "reb + ast":        PROP_RA,
    "double-double":    PROP_DOUBLE_DOUBLE,
    "triple-double":    PROP_TRIPLE_DOUBLE,
    "first basket":     PROP_FIRST_BASKET,
    # NHL
    "goals":            PROP_GOALS,
    "shots on goal":    PROP_SHOTS_ON_GOAL,
    "saves":            PROP_SAVES,
    "power play points": PROP_POWER_PLAY_POINTS,
    # NFL
    "passing yards":     PROP_PASS_YDS,
    "passing touchdowns": PROP_PASS_TDS,
    "rushing yards":     PROP_RUSH_YDS,
    "receiving yards":   PROP_REC_YDS,
    "receptions":        PROP_RECEPTIONS,
    "anytime touchdown": PROP_ANYTIME_TD,
    "anytime td":        PROP_ANYTIME_TD,
}

# Fallback map keyed by the noun phrase that follows " Total " in a
# ProphetX market name. The v1 /markets endpoint omits `categoryName`
# and `subType`, so the v2 classifier path (which relies on those
# fields) collapses every player prop into MTYPE_TOTAL and they
# collide with the real game total. With this map we recover the
# subtype from the market name itself.
#
# Differences from _PROPHETX_PROP_CATEGORY_MAP (which keys off
# categoryName):
#   - "Total Bases"      → name trailing is "Bases"
#   - "Strikeouts"       → name trailing is "Pitching Strikeouts" or
#                          "Batting Strikeouts" (PX names disambiguate
#                          batter vs pitcher in the noun phrase even
#                          though categoryName collapsed both to
#                          "Strikeouts")
#   - "Earned Runs"      → name trailing is "Earned Runs Allowed"
#   - "Pitcher Outs"     → name trailing is "Outs Recorded"
_PROPHETX_PROP_NAME_SUFFIX_MAP = {
    # MLB batter
    "home runs":             PROP_HOME_RUNS,
    "hits":                  PROP_HITS,
    "rbis":                  PROP_RBIS,
    "runs":                  PROP_RUNS,
    "bases":                 PROP_TOTAL_BASES,
    "hits, runs & rbis":     PROP_HRR,
    "batting strikeouts":    PROP_STRIKEOUTS,
    "walks":                 PROP_WALKS,
    "stolen bases":          PROP_STOLEN_BASES,
    # MLB pitcher
    "pitching strikeouts":   PROP_PITCHER_STRIKEOUTS,
    "outs recorded":         PROP_PITCHER_OUTS,
    "earned runs allowed":   PROP_PITCHER_EARNED_RUNS,
    "hits allowed":          PROP_PITCHER_HITS_ALLOWED,
    # NBA — same trailing tokens as the categoryName variant
    "points":                PROP_POINTS,
    "rebounds":              PROP_REBOUNDS,
    "assists":               PROP_ASSISTS,
    "threes":                PROP_THREES,
    "steals":                PROP_STEALS,
    "blocks":                PROP_BLOCKS,
    "turnovers":             PROP_TURNOVERS,
    # NHL
    "goals":                 PROP_GOALS,
    "shots on goal":         PROP_SHOTS_ON_GOAL,
    "saves":                 PROP_SAVES,
    # NFL — most NFL props don't use " Total " phrasing, so a small
    # set; passing/rushing/receiving yards do follow the pattern.
    "passing yards":         PROP_PASS_YDS,
    "passing touchdowns":    PROP_PASS_TDS,
    "rushing yards":         PROP_RUSH_YDS,
    "receiving yards":       PROP_REC_YDS,
    "receptions":            PROP_RECEPTIONS,
}

# Novig `type` enum -> canonical prop subtype. Novig already uses
# upper-snake-case tokens, mostly aligned with our canonical set.
_NOVIG_PROP_TYPE_MAP = {
    "HOME_RUNS": PROP_HOME_RUNS,
    "HITS": PROP_HITS,
    "RBIS": PROP_RBIS,
    "RUNS": PROP_RUNS,
    "TOTAL_BASES": PROP_TOTAL_BASES,
    "HITS_RUNS_RBIS": PROP_HRR,
    "PITCHER_STRIKEOUTS": PROP_PITCHER_STRIKEOUTS,
    "PITCHER_OUTS": PROP_PITCHER_OUTS,
    "HITS_ALLOWED": PROP_PITCHER_HITS_ALLOWED,
    "EARNED_RUNS": PROP_PITCHER_EARNED_RUNS,
    "STOLEN_BASES": PROP_STOLEN_BASES,
    "STRIKEOUTS": PROP_STRIKEOUTS,
    "WALKS": PROP_WALKS,
    # NBA
    "POINTS":                    PROP_POINTS,
    "REBOUNDS":                  PROP_REBOUNDS,
    "ASSISTS":                   PROP_ASSISTS,
    "THREE_POINTERS_MADE":       PROP_THREES,
    "STEALS":                    PROP_STEALS,
    "BLOCKS":                    PROP_BLOCKS,
    "TURNOVERS":                 PROP_TURNOVERS,
    "STEALS_BLOCKS":             PROP_STEALS_BLOCKS,
    "POINTS_REBOUNDS_ASSISTS":   PROP_PRA,
    "POINTS_REBOUNDS":           PROP_PR,
    "POINTS_ASSISTS":            PROP_PA,
    "REBOUNDS_ASSISTS":          PROP_RA,
    "DOUBLE_DOUBLE":             PROP_DOUBLE_DOUBLE,
    "TRIPLE_DOUBLE":             PROP_TRIPLE_DOUBLE,
    "FIRST_BASKET":              PROP_FIRST_BASKET,
    # NHL
    "GOALS":                     PROP_GOALS,
    "HOCKEY_ASSISTS":            PROP_HOCKEY_ASSISTS,
    "SHOTS_ON_GOAL":             PROP_SHOTS_ON_GOAL,
    "SAVES":                     PROP_SAVES,
    "POWER_PLAY_POINTS":         PROP_POWER_PLAY_POINTS,
    # NFL
    "PASSING_YARDS":             PROP_PASS_YDS,
    "PASSING_TOUCHDOWNS":        PROP_PASS_TDS,
    "RUSHING_YARDS":             PROP_RUSH_YDS,
    "RECEIVING_YARDS":           PROP_REC_YDS,
    "RECEPTIONS":                PROP_RECEPTIONS,
    "ANYTIME_TOUCHDOWN":         PROP_ANYTIME_TD,
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class NormalizedOrder:
    prob: float
    american: int
    size_usd: float
    raw: Any = None


@dataclass(slots=True)
class NormalizedSide:
    label: str                      # display string ("Over 1.5", "PHI", etc.)
    side_type: str                  # SIDE_* enum
    orders: list[NormalizedOrder] = field(default_factory=list)
    total_size_usd: float = 0.0


@dataclass(slots=True)
class NormalizedLine:
    strike: Optional[float]         # None for moneyline
    line_label: str                 # "11.5", "-3.5", "" for moneyline
    source_line_id: str             # unique within parent Market for tracing
    sides: list[NormalizedSide] = field(default_factory=list)
    total_liquidity_usd: float = 0.0


@dataclass(slots=True)
class NormalizedMarket:
    source: str                     # SOURCE_* enum
    source_market_id: str           # primary key on the source side
    event_id: str
    event_name: str
    market_name: str                # human-readable parent name ("Total Runs")
    market_type: str                # MTYPE_* enum
    market_subtype: Optional[str]   # finer tag ("HOME_RUNS", "spread", ...)
    player_name: Optional[str]
    lines: list[NormalizedLine] = field(default_factory=list)
    total_liquidity_usd: float = 0.0
    raw: Any = None


@dataclass(slots=True)
class NormalizedEvent:
    source: str
    source_event_id: str
    event_name: str
    sport: Optional[str] = None
    league: Optional[str] = None
    scheduled_start: Optional[str] = None
    markets: list[NormalizedMarket] = field(default_factory=list)
    raw: Any = None


# ---------------------------------------------------------------------------
# Side-label helpers
# ---------------------------------------------------------------------------

# A trailing American-odds suffix on a ProphetX order's display name
# ("Edas Butvilas +160"). Stripped to recover the side identity alone.
#
# Must NOT match spread strikes like "OKC -1.5" or "OKC +12.5" — those
# are the side's actual strike, not a price suffix, and stripping them
# causes the dual-source renderer to mislabel underdog spread rows (the
# two sides of one PX marketLine have opposite-signed strikes, so the
# renderer needs the per-side strike preserved on s.label).
#
# American odds are always integers with |value| >= 100 and an explicit
# sign — that's enough to disambiguate from spread/total fractions.
_TRAILING_PRICE_RE = re.compile(r"\s*[+-]\d{3,}\s*$")

# ProphetX wraps the strike in descriptive text: "Fixed total 0.5",
# "Alternate spread -3.5". Extract the trailing signed decimal.
_STRIKE_FROM_TEXT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*$")


def _parse_strike(text: Optional[Any]) -> Optional[float]:
    """Pull a numeric strike out of a descriptive line label.
    Returns None when no number is present."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
    s = str(text).strip()
    if not s:
        return None
    # Fast path: whole string is a number
    try:
        return float(s)
    except ValueError:
        pass
    m = _STRIKE_FROM_TEXT_RE.search(s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def classify_side(label: str) -> str:
    """Return SIDE_* enum for a human side label (case-insensitive)."""
    if not label:
        return SIDE_UNKNOWN
    ll = label.strip().lower()
    if ll.startswith("over"):
        return SIDE_OVER
    if ll.startswith("under"):
        return SIDE_UNDER
    if ll == "yes":
        return SIDE_YES
    if ll == "no":
        return SIDE_NO
    return SIDE_TEAM


def strip_price_from_label(label: str) -> str:
    if not label:
        return label
    return _TRAILING_PRICE_RE.sub("", label).strip()


def _sort_side_orders(orders: list[NormalizedOrder]) -> list[NormalizedOrder]:
    """Sort orders within a side by American odds descending. This matches
    the existing LiquidityWidget convention: best-paying offer at top of
    the side's block."""
    return sorted(orders, key=lambda o: o.american, reverse=True)


def _sort_lines(lines: list[NormalizedLine]) -> list[NormalizedLine]:
    """Sort lines within a market by strike descending. Moneyline (no
    strike) sorts last."""
    def key(ln: NormalizedLine) -> tuple[int, float]:
        if ln.strike is None:
            return (1, 0.0)
        return (0, -ln.strike)
    return sorted(lines, key=key)


# ---------------------------------------------------------------------------
# ProphetX adapter
# ---------------------------------------------------------------------------

# ProphetX `type` token -> MTYPE_*. Anything else is heuristically routed
# (player props live under arbitrary `categoryName`s).
_PROPHETX_TYPE_MAP = {
    "moneyline": MTYPE_MONEYLINE,
    "spread": MTYPE_SPREAD,
    "total": MTYPE_TOTAL,
}


def _prophetx_market_type(market: dict) -> tuple[str, Optional[str]]:
    """Classify a ProphetX market.

    Game lines: type in {moneyline, spread, total} with categoryName
    typically "Game Lines" or "Other".

    Player props: type is `total` but categoryName names the prop bucket
    ("Home Runs", "Hits", "Total Bases", "Hits + Runs + RBIs",
    "Strikeouts"). The categoryName is what carries the semantic content
    here — the `total` type token is misleading.
    """
    raw_type = (market.get("type") or "").lower()
    category = (market.get("categoryName") or "").strip()
    cat_lower = category.lower()
    name = (market.get("name") or "").strip()

    # 1) categoryName match (v2 path). categoryName is the most reliable
    # signal when present.
    prop_subtype = _PROPHETX_PROP_CATEGORY_MAP.get(cat_lower)
    if prop_subtype is not None:
        return MTYPE_PLAYER_PROP, prop_subtype

    # 2) Name-suffix match (v1 fallback). v1 omits categoryName so we
    # derive the subtype from the noun phrase after " Total " in the
    # market name. Without this, every player prop collapses into
    # MTYPE_TOTAL and collides with the game total when matching
    # against Novig.
    #
    # Guard against period-restricted game totals like
    # "1st-5th Inning Total Runs" or "1st Inning Total Runs" — those
    # also contain " Total " but the prefix is a period specifier, not
    # a player name. Treat the market as a player prop only when the
    # prefix doesn't look like one of those.
    sep = " Total "
    sep_idx = name.find(sep)
    if sep_idx > 0:
        prefix_lower = name[:sep_idx].lower()
        is_period_prefix = any(tok in prefix_lower for tok in (
            "inning", "period", "quarter", "half", " set ",
        ))
        is_team_prefix = ":" in prefix_lower  # "CLE: Team Total Runs"
        if not is_period_prefix and not is_team_prefix:
            trailing = name[sep_idx + len(sep):].strip().lower()
            suffix_subtype = _PROPHETX_PROP_NAME_SUFFIX_MAP.get(trailing)
            if suffix_subtype is not None:
                return MTYPE_PLAYER_PROP, suffix_subtype

    # 3) Game-line types — only when neither categoryName nor name
    # suffix indicated a prop.
    mtype = _PROPHETX_TYPE_MAP.get(raw_type)
    if mtype is not None:
        # Use subType, not the bare `type` token, as the game-line
        # subtype. subType encodes the period — "moneyline" vs
        # "first_half_moneyline" — whereas `type` is "moneyline" for
        # both, which would collapse full-game and 1H markets onto a
        # single match key (pairing a 1H market against a full-game
        # one). Fall back to raw_type when subType is absent (v1).
        sub = (market.get("subType") or "").strip().lower() or raw_type
        return mtype, sub

    # 4) Heuristic fallback for unknown prop-looking categories
    if any(k in cat_lower for k in ("player", "prop", "batter", "pitcher", "hitter")):
        return MTYPE_PLAYER_PROP, category or raw_type or None
    return MTYPE_OTHER, raw_type or cat_lower or None


def _prophetx_order_to_normalized(order: dict) -> Optional[NormalizedOrder]:
    american = order.get("odds")
    if american is None:
        return None
    try:
        american = int(american)
    except (TypeError, ValueError):
        return None
    prob = american_to_prob(american)
    if prob is None:
        return None
    size = float(order.get("value") or 0.0)
    return NormalizedOrder(prob=prob, american=american, size_usd=size, raw=order)


def _prophetx_build_sides(side_arrays: list[list[dict]]) -> list[NormalizedSide]:
    """ProphetX `selections` is [[side_a_orders], [side_b_orders]]."""
    sides: list[NormalizedSide] = []
    for side_orders in side_arrays:
        if not side_orders:
            continue
        label_raw = (side_orders[0].get("displayName")
                     or side_orders[0].get("name") or "")
        label = strip_price_from_label(label_raw)
        norm_orders = [_prophetx_order_to_normalized(o) for o in side_orders]
        norm_orders = [o for o in norm_orders if o is not None]
        norm_orders = _sort_side_orders(norm_orders)
        sides.append(NormalizedSide(
            label=label,
            side_type=classify_side(label),
            orders=norm_orders,
            total_size_usd=sum(o.size_usd for o in norm_orders),
        ))
    return sides


def _prophetx_player_name(market: dict, mtype: str) -> Optional[str]:
    """Extract the player name from a ProphetX prop market.

    ProphetX names player props as "<Player> Total <prop noun phrase>":
        "J.T. Realmuto Total Home Runs"
        "Aaron Nola Total Pitching Strikeouts"
        "J.T. Realmuto Total Hits, Runs & RBIs"

    The reliable anchor is the literal " Total " connector — everything
    before it is the player. The connector word "Total" never appears in
    real player names in any league we care about.
    """
    if mtype != MTYPE_PLAYER_PROP:
        return None
    name = (market.get("name") or "").strip()
    if not name:
        return None
    sep = " Total "
    idx = name.find(sep)
    if idx > 0:
        return name[:idx].strip() or None
    # Some compound props could omit "Total" — fall back to category
    # suffix stripping.
    category = (market.get("categoryName") or "").strip()
    if category:
        for suffix in (f" {category}", f" Total {category}"):
            if name.endswith(suffix):
                return name[: -len(suffix)].strip() or None
    # Last-ditch: drop the last 2 tokens.
    toks = name.split()
    if len(toks) >= 3:
        return " ".join(toks[:-2])
    return None


def from_prophetx_market(market: dict,
                         event_meta: Optional[dict] = None
                         ) -> NormalizedMarket:
    """Normalize one ProphetX market into a NormalizedMarket. Returns one
    Market containing N Lines (1 for moneyline, N for marketLine-bundled
    markets)."""
    event_meta = event_meta or {}
    event_id = str(event_meta.get("id", ""))
    event_name = event_meta.get("name", "")
    market_name = market.get("name", "Unknown Market")
    mtype, subtype = _prophetx_market_type(market)
    player_name = _prophetx_player_name(market, mtype)
    raw_market_id = str(market.get("id", ""))

    lines: list[NormalizedLine] = []

    if "marketLines" in market and market["marketLines"]:
        for ml in market["marketLines"]:
            # Strike: ProphetX wraps the number in descriptive text
            # ("Fixed total 0.5", "Alternate spread -3.5"). Parse the
            # trailing signed decimal out of the line name; fall back to
            # the `line` field if present.
            strike_val = _parse_strike(ml.get("name"))
            if strike_val is None:
                strike_val = _parse_strike(ml.get("line"))
            # Moneyline guard — should never reach here, but if a payload
            # marks itself as moneyline AND ships marketLines, force None.
            if mtype == MTYPE_MONEYLINE:
                strike_val = None

            sides = _prophetx_build_sides(ml.get("selections") or [])
            total = sum(s.total_size_usd for s in sides)
            line_label = ml.get("name") or (str(strike_val) if strike_val is not None else "")
            line_id = str(ml.get("lineID") or ml.get("id") or line_label)
            lines.append(NormalizedLine(
                strike=strike_val,
                line_label=line_label,
                source_line_id=line_id,
                sides=sides,
                total_liquidity_usd=total,
            ))
    else:
        # Simple market (no marketLines). Usually a moneyline (strike
        # None), but ProphetX also ships some over/under totals this way
        # (e.g. "1st Inning Total Runs" with sides "Over 0.5"/"Under 0.5",
        # which classify as MTYPE_OTHER). Recover the strike from an
        # over/under side label so the line isn't rendered strike-less —
        # otherwise the dual renderer buckets it under None and shows a
        # bare "over"/"under" with no number. Moneyline sides carry team
        # names (no parseable number), so this stays None for them.
        sides = _prophetx_build_sides(market.get("selections") or [])
        total = sum(s.total_size_usd for s in sides)
        strike_val: Optional[float] = None
        if mtype != MTYPE_MONEYLINE:
            for s in sides:
                if s.side_type in (SIDE_OVER, SIDE_UNDER):
                    cand = _parse_strike(s.label)
                    if cand is not None:
                        strike_val = cand
                        break
        lines.append(NormalizedLine(
            strike=strike_val,
            line_label="" if strike_val is None else _format_strike(strike_val),
            source_line_id=raw_market_id,
            sides=sides,
            total_liquidity_usd=total,
        ))

    lines = _sort_lines(lines)
    market_total = sum(ln.total_liquidity_usd for ln in lines)
    return NormalizedMarket(
        source=SOURCE_PROPHETX,
        source_market_id=raw_market_id,
        event_id=event_id,
        event_name=event_name,
        market_name=market_name,
        market_type=mtype,
        market_subtype=subtype,
        player_name=player_name,
        lines=lines,
        total_liquidity_usd=market_total,
        raw=market,
    )


def from_prophetx_event(event_dump: dict) -> NormalizedEvent:
    """Normalize a full ProphetX event dump (one entry from
    `all_markets_combined_*.json`). The expected shape is:
        {
          "event_metadata": {id, name, startTime, sport, tournament, ...},
          "data": {"markets": [ ... ]}
        }
    """
    meta = event_dump.get("event_metadata") or {}
    markets_raw = (event_dump.get("data") or {}).get("markets") or []
    markets = [from_prophetx_market(m, meta) for m in markets_raw]
    return NormalizedEvent(
        source=SOURCE_PROPHETX,
        source_event_id=str(meta.get("id", "")),
        event_name=meta.get("name", ""),
        sport=meta.get("sport"),
        league=meta.get("tournament"),
        scheduled_start=meta.get("startTime"),
        markets=markets,
        raw=event_dump,
    )


# ---------------------------------------------------------------------------
# Novig adapter
# ---------------------------------------------------------------------------

# Novig `type` enum -> MTYPE_*. Anything carrying a playerId is forced to
# PLAYER_PROP regardless of `type`.
_NOVIG_GAME_TYPES = {
    "MONEY": MTYPE_MONEYLINE,
    "MONEY_1H": MTYPE_MONEYLINE,
    "SPREAD": MTYPE_SPREAD,
    "SPREAD_1H": MTYPE_SPREAD,
    "TOTAL": MTYPE_TOTAL,
    "TOTAL_1H": MTYPE_TOTAL,
    "TEAM_TOTAL": MTYPE_TOTAL,
    "FIRST_INNING_TOTAL": MTYPE_TOTAL,
    "TOTAL_HOME_RUNS": MTYPE_TOTAL,
}

# NBX qty units -> display dollars.
_NBX_QTY_SCALE = 100.0


def _novig_market_type(market: dict) -> tuple[str, Optional[str]]:
    """Classify a Novig market. For player props (playerId set), the
    subtype is the canonical prop token from _NOVIG_PROP_TYPE_MAP when
    recognized, else the raw Novig type string."""
    raw_type = market.get("type") or ""
    if market.get("playerId"):
        canon = _NOVIG_PROP_TYPE_MAP.get(raw_type)
        return MTYPE_PLAYER_PROP, canon or raw_type
    # Golf round matchups are single-round head-to-heads: Novig types the
    # result market "<ORDINAL>_ROUND_MONEYLINE" (FIRST/.../FOURTH), which
    # is the whole-matchup moneyline and pairs with ProphetX's plain
    # matchup moneyline. Fold to (MONEYLINE, "MONEY"). Tennis set markets
    # use "_SET_" (e.g. 1ST_SET_MONEYLINE), so they're unaffected and stay
    # distinct from the full-match line.
    if raw_type.endswith("_ROUND_MONEYLINE"):
        return MTYPE_MONEYLINE, "MONEY"
    mtype = _NOVIG_GAME_TYPES.get(raw_type)
    if mtype is not None:
        return mtype, raw_type
    return MTYPE_OTHER, raw_type or None


def _novig_player_name(market: dict) -> Optional[str]:
    """Extract player name from a Novig prop market description like
    'Aaron Judge 0.5 HOME_RUNS'."""
    if not market.get("playerId"):
        return None
    desc = market.get("description") or ""
    strike = market.get("strike")
    mtype = market.get("type") or ""
    if strike is not None and mtype:
        suffix = f" {strike} {mtype}"
        if desc.endswith(suffix):
            return desc[: -len(suffix)].strip() or None
    toks = desc.split()
    if len(toks) >= 3:
        return " ".join(toks[:-2])
    return desc or None


def _novig_order_from_book(o: dict, *, market_id: Optional[str] = None,
                           outcome_id: Optional[str] = None,
                           is_bid: Optional[bool] = None
                           ) -> Optional[NormalizedOrder]:
    """One entry from ladders[outcome_id].bids or .asks.

    market_id / outcome_id / is_bid are stamped into the raw dict so the
    bet slip can pass them straight to /nbx/v1/orders without re-walking
    the parent containers. None when called from a context that doesn't
    know them (legacy callers); single-bet placement skips those rows.
    """
    try:
        prob = float(o.get("price"))
        qty = float(o.get("qty") or 0.0)
    except (TypeError, ValueError):
        return None
    if not (0.0 < prob < 1.0):
        return None
    american = prob_to_american(prob)
    if american is None:
        return None
    raw = dict(o)
    if market_id is not None:
        raw["_market_id"] = market_id
    if outcome_id is not None:
        raw["_outcome_id"] = outcome_id
    if is_bid is not None:
        raw["_is_bid"] = is_bid
    return NormalizedOrder(prob=prob, american=american,
                           size_usd=qty / _NBX_QTY_SCALE, raw=raw)


def _novig_outcome_to_top_level_side(outcome: dict) -> NormalizedSide:
    """No-book fallback: emit a one-level side from `available` (best ask)."""
    label = outcome.get("description") or outcome.get("type") or "?"
    orders: list[NormalizedOrder] = []
    for key in ("available", "last"):
        p = outcome.get(key)
        if p is None:
            continue
        try:
            prob = float(p)
        except (TypeError, ValueError):
            continue
        if not (0.0 < prob < 1.0):
            continue
        american = prob_to_american(prob)
        if american is None:
            continue
        orders.append(NormalizedOrder(
            prob=prob, american=american, size_usd=0.0,
            raw={"_from": key, "outcome": outcome},
        ))
        break
    return NormalizedSide(label=label, side_type=classify_side(label),
                          orders=orders, total_size_usd=0.0)


def _novig_outcome_to_side_from_book(outcome: dict, ladder: dict,
                                     market_id: Optional[str] = None
                                     ) -> NormalizedSide:
    label = outcome.get("description") or outcome.get("type") or "?"
    outcome_id = outcome.get("id")
    orders: list[NormalizedOrder] = []
    # `bids` are takers buying this outcome; `asks` are takers selling.
    # Tag orders so the bet slip knows which side to PLACE on.
    for o in (ladder.get("bids") or []):
        no = _novig_order_from_book(o, market_id=market_id,
                                    outcome_id=outcome_id, is_bid=True)
        if no is not None:
            orders.append(no)
    for o in (ladder.get("asks") or []):
        no = _novig_order_from_book(o, market_id=market_id,
                                    outcome_id=outcome_id, is_bid=False)
        if no is not None:
            orders.append(no)
    orders = _sort_side_orders(orders)
    return NormalizedSide(label=label, side_type=classify_side(label),
                          orders=orders,
                          total_size_usd=sum(o.size_usd for o in orders))


def _novig_grouping_key(market: dict) -> tuple[str, Optional[str], Optional[str]]:
    """Two Novig markets belong to the same display Market iff their
    (mtype, subtype, player_id) tuples match. Subtype distinguishes
    MONEY vs MONEY_1H, TEAM_TOTAL vs TOTAL, etc."""
    mtype, subtype = _novig_market_type(market)
    return (mtype, subtype, market.get("playerId"))


def _novig_market_display_name(group_markets: list[dict],
                               mtype: str,
                               subtype: Optional[str]) -> str:
    """Pick a display name for the aggregated Market. For player props,
    use 'PlayerName SUBTYPE'. For game markets, use the subtype with
    title-casing."""
    sample = group_markets[0]
    if sample.get("playerId"):
        player = _novig_player_name(sample) or "?"
        return f"{player} {subtype}" if subtype else player
    # Game-level: pretty-print the subtype.
    if subtype:
        pretty = subtype.replace("_", " ").title()
        return pretty
    return mtype.title()


def _novig_back_side_from_opposite(outcome: dict,
                                   opp_ladder: Optional[dict],
                                   market_id: Optional[str] = None
                                   ) -> NormalizedSide:
    """Build outcome X's takeable BACK ladder from the OPPOSITE outcome
    Y's resting bids.

    On Novig's NBX book each outcome's ladder only ever carries `bids`
    (the `asks` array is always empty). A bid is a resting order from
    someone wanting to BACK that outcome — same side as you, so it can't
    fill your back order. The liquidity that actually fills a back of X
    is a bid on Y: a "bid Y @ p" crosses a "bid X @ (1 - p)" because the
    two stakes sum to the $1 contract. So X's best available back price
    is the complement of Y's best bid, with Y's bid size as the matchable
    depth.

    Reading X's own bids as X's back-odds (the old _novig_outcome_to_
    side_from_book path) inverts the market — both sides print better
    than fair, an impossible sub-100% book. This complement read instead
    matches the outcome-level `available` field (which Novig already
    derives from the opposite side) and the paired ProphetX line.

    Falls back to the no-book top-of-book side when Y's ladder is missing
    or has no usable bids.
    """
    label = outcome.get("description") or outcome.get("type") or "?"
    outcome_id = outcome.get("id")
    orders: list[NormalizedOrder] = []
    for o in ((opp_ladder or {}).get("bids") or []):
        try:
            opp_prob = float(o.get("price"))
            qty = float(o.get("qty") or 0.0)
        except (TypeError, ValueError):
            continue
        prob = 1.0 - opp_prob
        if not (0.0 < prob < 1.0):
            continue
        american = prob_to_american(prob)
        if american is None:
            continue
        # Stamp the order so it reflects THIS outcome (the one being
        # backed) at the complement price — the honest representation of
        # what the displayed row means.
        raw = dict(o)
        raw["price"] = prob
        raw["_opp_price"] = opp_prob
        raw["_market_id"] = market_id
        raw["_outcome_id"] = outcome_id
        raw["_is_bid"] = True
        orders.append(NormalizedOrder(prob=prob, american=american,
                                      size_usd=qty / _NBX_QTY_SCALE, raw=raw))
    if not orders:
        return _novig_outcome_to_top_level_side(outcome)
    orders = _sort_side_orders(orders)
    return NormalizedSide(label=label, side_type=classify_side(label),
                          orders=orders,
                          total_size_usd=sum(o.size_usd for o in orders))


def _novig_line_from_market(market: dict,
                            book_entry: Optional[dict],
                            mtype: str) -> NormalizedLine:
    """Convert a single Novig market (one strike) to one Line."""
    outcomes = market.get("outcomes") or []
    ladders = (book_entry or {}).get("ladders") or {}
    sides: list[NormalizedSide] = []
    if len(outcomes) == 2 and ladders:
        # Binary market with a live book: cross-map each outcome's back
        # ladder from the OPPOSITE outcome's bids (see
        # _novig_back_side_from_opposite). Reading an outcome's own bids
        # inverts the price, so this is required for the displayed odds
        # to match `available` / ProphetX.
        o_a, o_b = outcomes
        lad_a = ladders.get(o_a.get("id"))
        lad_b = ladders.get(o_b.get("id"))
        sides.append(_novig_back_side_from_opposite(
            o_a, lad_b, market_id=market.get("id")))
        sides.append(_novig_back_side_from_opposite(
            o_b, lad_a, market_id=market.get("id")))
    else:
        # Non-binary (3-way, or markets with no live book): fall back to
        # the per-outcome read. Top-of-book `available` is already a
        # correct (opposite-derived) back price, so the no-book path is
        # fine; only the multi-outcome book path lacks a clean complement.
        for o in outcomes:
            ladder = ladders.get(o.get("id"))
            if ladder is not None:
                sides.append(_novig_outcome_to_side_from_book(
                    o, ladder, market_id=market.get("id")))
            else:
                sides.append(_novig_outcome_to_top_level_side(o))
    total = sum(s.total_size_usd for s in sides)

    # Moneyline guard: ignore source strike (Novig MONEY ships strike=0).
    if mtype == MTYPE_MONEYLINE:
        strike_val = None
    else:
        s = market.get("strike")
        try:
            strike_val = float(s) if s is not None else None
        except (TypeError, ValueError):
            strike_val = None

    line_label = "" if strike_val is None else _format_strike(strike_val)
    return NormalizedLine(
        strike=strike_val,
        line_label=line_label,
        source_line_id=str(market.get("id") or ""),
        sides=sides,
        total_liquidity_usd=total,
    )


def _format_strike(s: float) -> str:
    """Render strike as it appears in the existing widget: "11.5", "-3.5",
    integer when exact."""
    if s == int(s):
        return str(int(s))
    return f"{s}"


def from_novig_event(event_node: dict,
                     books: Optional[dict[str, dict]] = None,
                     currency: str = "CASH",
                     league: Optional[str] = None) -> NormalizedEvent:
    """Aggregate a Novig event subtree into a NormalizedEvent.

    Args:
        event_node: a single event node as returned by
            NovigQueries.get_event_markets(). Should carry `markets[]` and
            optionally nested `events[]`. Use `NovigQueries.flatten_markets`
            externally if you want the full subtree flattened first; this
            function only consumes one node + walks its children directly.
        books: optional map of {market_id: book_entry_dict} where each
            book_entry comes from NovigClient.get_market_books(). When a
            market_id is present, its full ladder is used; otherwise the
            outcome top-of-book fallback applies.
        currency: "CASH" or "COIN" — selects the source enum on every
            emitted NormalizedMarket.
        league: optional league override (e.g. "MLB"); falls back to the
            node's `league` field.
    """
    books = books or {}
    source = SOURCE_NOVIG_CASH if currency.upper() == "CASH" else SOURCE_NOVIG_COIN

    # Flatten this event subtree into one list of markets, tagged with
    # their root event for downstream traceability.
    flat_markets: list[dict] = []
    def _walk(node: dict) -> None:
        if not node:
            return
        for m in node.get("markets") or []:
            flat_markets.append(m)
        for child in node.get("events") or []:
            _walk(child)
    _walk(event_node)

    # Group by (mtype, subtype, playerId) -> list of source markets
    groups: dict[tuple, list[dict]] = {}
    for m in flat_markets:
        key = _novig_grouping_key(m)
        groups.setdefault(key, []).append(m)

    markets_out: list[NormalizedMarket] = []
    for (mtype, subtype, player_id), grp in groups.items():
        # Build one Line per source market in the group
        lines = [_novig_line_from_market(m, books.get(m.get("id")), mtype)
                 for m in grp]
        # Moneyline collapse: if mtype is MONEYLINE there should be one
        # Line with strike=None. If a source ships duplicates, keep them
        # all; the renderer can dedupe.
        lines = _sort_lines(lines)
        market_total = sum(ln.total_liquidity_usd for ln in lines)
        sample = grp[0]
        player_name = _novig_player_name(sample) if player_id else None
        display_name = _novig_market_display_name(grp, mtype, subtype)
        # source_market_id at the group level: sort-joined ids so the key
        # is stable across calls
        group_id = "+".join(sorted(str(m.get("id") or "") for m in grp))
        markets_out.append(NormalizedMarket(
            source=source,
            source_market_id=group_id,
            event_id=str(event_node.get("id") or ""),
            event_name=event_node.get("description") or "",
            market_name=display_name,
            market_type=mtype,
            market_subtype=subtype,
            player_name=player_name,
            lines=lines,
            total_liquidity_usd=market_total,
            raw={"group": grp},
        ))

    game = event_node.get("game") or {}
    return NormalizedEvent(
        source=source,
        source_event_id=str(event_node.get("id") or ""),
        event_name=event_node.get("description") or "",
        sport=game.get("sport"),
        league=league or event_node.get("league") or game.get("league"),
        scheduled_start=event_node.get("scheduled_start"),
        markets=markets_out,
        raw=event_node,
    )


# ===========================================================================
# CROSS-SOURCE MATCHER
# ===========================================================================
# Everything below operates on already-normalized inputs (NormalizedEvent
# trees produced by the adapters above). Nothing here calls the network.
#
# Matching rules (MLB-tuned; extensible to other leagues):
#
# EVENT match:
#   - Both events league=MLB (sport=Baseball acceptable as a fallback).
#   - scheduled_start within +/- 2 hours (covers feed time-rounding,
#     rain-delay rescheduling, and timezone bugs).
#   - Team set match: each event's two team symbols (after normalization
#     through MLB_TEAMS) are equal as a set.
#
# MARKET match within paired events:
#   - Game markets: same `market_type` AND same canonical subtype.
#     Game-line subtype canonicalization collapses ProphetX's lowercase
#     ("moneyline", "spread", "total") into the same buckets as Novig's
#     uppercase ("MONEY", "SPREAD", "TOTAL"). 1H/period variants stay
#     separate.
#   - Player props: same canonical prop subtype (HOME_RUNS, HITS, RBIS,
#     ...) AND normalized player_name match.
#
# LINE match within paired markets:
#   - Game markets: same numeric strike. Moneyline has strike=None on
#     both sides and matches that one Line.
#   - Player props: same numeric strike.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# League team tables
# ---------------------------------------------------------------------------
# MLB team symbol -> canonical full name. ProphetX uses the full names
# ("Philadelphia Phillies"); Novig uses the symbols ("PHI"). We
# canonicalize both to symbols for matching.
#
# Each entry: SYMBOL -> (Full Name, Nickname).

MLB_TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona Diamondbacks",       "Diamondbacks"),
    "ATL": ("Atlanta Braves",             "Braves"),
    "BAL": ("Baltimore Orioles",          "Orioles"),
    "BOS": ("Boston Red Sox",             "Red Sox"),
    "CHC": ("Chicago Cubs",               "Cubs"),
    "CHI": ("Chicago Cubs",               "Cubs"),         # Novig sometimes uses CHI
    "CHW": ("Chicago White Sox",          "White Sox"),
    "CIN": ("Cincinnati Reds",            "Reds"),
    "CLE": ("Cleveland Guardians",        "Guardians"),
    "COL": ("Colorado Rockies",           "Rockies"),
    "DET": ("Detroit Tigers",             "Tigers"),
    "HOU": ("Houston Astros",             "Astros"),
    "KC":  ("Kansas City Royals",         "Royals"),
    "LAA": ("Los Angeles Angels",         "Angels"),
    "LAD": ("Los Angeles Dodgers",        "Dodgers"),
    "LA":  ("Los Angeles Dodgers",        "Dodgers"),      # Novig sometimes uses LA
    "MIA": ("Miami Marlins",              "Marlins"),
    "MIL": ("Milwaukee Brewers",          "Brewers"),
    "MIN": ("Minnesota Twins",            "Twins"),
    "NYM": ("New York Mets",              "Mets"),
    "NYY": ("New York Yankees",           "Yankees"),
    "OAK": ("Oakland Athletics",          "Athletics"),
    "ATH": ("Oakland Athletics",          "Athletics"),    # Novig occasionally
    "PHI": ("Philadelphia Phillies",      "Phillies"),
    "PIT": ("Pittsburgh Pirates",         "Pirates"),
    "SD":  ("San Diego Padres",           "Padres"),
    "SF":  ("San Francisco Giants",       "Giants"),
    "SEA": ("Seattle Mariners",           "Mariners"),
    "STL": ("St. Louis Cardinals",        "Cardinals"),
    "TB":  ("Tampa Bay Rays",             "Rays"),
    "TEX": ("Texas Rangers",              "Rangers"),
    "TOR": ("Toronto Blue Jays",          "Blue Jays"),
    "WAS": ("Washington Nationals",       "Nationals"),
    "WSH": ("Washington Nationals",       "Nationals"),    # alt
}

NBA_TEAMS: dict[str, tuple[str, str]] = {
    "ATL": ("Atlanta Hawks",                "Hawks"),
    "BOS": ("Boston Celtics",               "Celtics"),
    "BKN": ("Brooklyn Nets",                "Nets"),
    "BRK": ("Brooklyn Nets",                "Nets"),
    "CHA": ("Charlotte Hornets",            "Hornets"),
    "CHO": ("Charlotte Hornets",            "Hornets"),
    "CHI": ("Chicago Bulls",                "Bulls"),
    "CLE": ("Cleveland Cavaliers",          "Cavaliers"),
    "DAL": ("Dallas Mavericks",             "Mavericks"),
    "DEN": ("Denver Nuggets",               "Nuggets"),
    "DET": ("Detroit Pistons",              "Pistons"),
    "GSW": ("Golden State Warriors",        "Warriors"),
    "GS":  ("Golden State Warriors",        "Warriors"),
    "HOU": ("Houston Rockets",              "Rockets"),
    "IND": ("Indiana Pacers",               "Pacers"),
    "LAC": ("Los Angeles Clippers",         "Clippers"),
    "LAL": ("Los Angeles Lakers",           "Lakers"),
    "MEM": ("Memphis Grizzlies",            "Grizzlies"),
    "MIA": ("Miami Heat",                   "Heat"),
    "MIL": ("Milwaukee Bucks",              "Bucks"),
    "MIN": ("Minnesota Timberwolves",       "Timberwolves"),
    "NOP": ("New Orleans Pelicans",         "Pelicans"),
    "NO":  ("New Orleans Pelicans",         "Pelicans"),
    "NYK": ("New York Knicks",              "Knicks"),
    "NY":  ("New York Knicks",              "Knicks"),
    "OKC": ("Oklahoma City Thunder",        "Thunder"),
    "ORL": ("Orlando Magic",                "Magic"),
    "PHI": ("Philadelphia 76ers",           "76ers"),
    "PHX": ("Phoenix Suns",                 "Suns"),
    "PHO": ("Phoenix Suns",                 "Suns"),
    "POR": ("Portland Trail Blazers",       "Trail Blazers"),
    "SAC": ("Sacramento Kings",             "Kings"),
    "SAS": ("San Antonio Spurs",            "Spurs"),
    "SA":  ("San Antonio Spurs",            "Spurs"),
    "TOR": ("Toronto Raptors",              "Raptors"),
    "UTA": ("Utah Jazz",                    "Jazz"),
    "UTH": ("Utah Jazz",                    "Jazz"),
    "WAS": ("Washington Wizards",           "Wizards"),
    "WSH": ("Washington Wizards",           "Wizards"),
}

NHL_TEAMS: dict[str, tuple[str, str]] = {
    "ANA": ("Anaheim Ducks",                "Ducks"),
    "BOS": ("Boston Bruins",                "Bruins"),
    "BUF": ("Buffalo Sabres",               "Sabres"),
    "CAR": ("Carolina Hurricanes",          "Hurricanes"),
    "CBJ": ("Columbus Blue Jackets",        "Blue Jackets"),
    "CGY": ("Calgary Flames",               "Flames"),
    "CHI": ("Chicago Blackhawks",           "Blackhawks"),
    "COL": ("Colorado Avalanche",           "Avalanche"),
    "DAL": ("Dallas Stars",                 "Stars"),
    "DET": ("Detroit Red Wings",            "Red Wings"),
    "EDM": ("Edmonton Oilers",              "Oilers"),
    "FLA": ("Florida Panthers",             "Panthers"),
    "LAK": ("Los Angeles Kings",            "Kings"),
    "LA":  ("Los Angeles Kings",            "Kings"),
    "MIN": ("Minnesota Wild",               "Wild"),
    "MTL": ("Montreal Canadiens",           "Canadiens"),
    "NJD": ("New Jersey Devils",            "Devils"),
    "NJ":  ("New Jersey Devils",            "Devils"),
    "NSH": ("Nashville Predators",          "Predators"),
    "NYI": ("New York Islanders",           "Islanders"),
    "NYR": ("New York Rangers",             "Rangers"),
    "OTT": ("Ottawa Senators",              "Senators"),
    "PHI": ("Philadelphia Flyers",          "Flyers"),
    "PIT": ("Pittsburgh Penguins",          "Penguins"),
    "SEA": ("Seattle Kraken",               "Kraken"),
    "SJS": ("San Jose Sharks",              "Sharks"),
    "SJ":  ("San Jose Sharks",              "Sharks"),
    "STL": ("St. Louis Blues",              "Blues"),
    "TBL": ("Tampa Bay Lightning",          "Lightning"),
    "TB":  ("Tampa Bay Lightning",          "Lightning"),
    "TOR": ("Toronto Maple Leafs",          "Maple Leafs"),
    "UTA": ("Utah Mammoth",                 "Mammoth"),
    "UTH": ("Utah Mammoth",                 "Mammoth"),
    "VAN": ("Vancouver Canucks",            "Canucks"),
    "VGK": ("Vegas Golden Knights",         "Golden Knights"),
    "VEG": ("Vegas Golden Knights",         "Golden Knights"),
    "WPG": ("Winnipeg Jets",                "Jets"),
    "WSH": ("Washington Capitals",          "Capitals"),
    "WAS": ("Washington Capitals",          "Capitals"),
}

NFL_TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona Cardinals",            "Cardinals"),
    "ATL": ("Atlanta Falcons",              "Falcons"),
    "BAL": ("Baltimore Ravens",             "Ravens"),
    "BUF": ("Buffalo Bills",                "Bills"),
    "CAR": ("Carolina Panthers",            "Panthers"),
    "CHI": ("Chicago Bears",                "Bears"),
    "CIN": ("Cincinnati Bengals",           "Bengals"),
    "CLE": ("Cleveland Browns",             "Browns"),
    "DAL": ("Dallas Cowboys",               "Cowboys"),
    "DEN": ("Denver Broncos",               "Broncos"),
    "DET": ("Detroit Lions",                "Lions"),
    "GB":  ("Green Bay Packers",            "Packers"),
    "GNB": ("Green Bay Packers",            "Packers"),
    "HOU": ("Houston Texans",               "Texans"),
    "IND": ("Indianapolis Colts",           "Colts"),
    "JAX": ("Jacksonville Jaguars",         "Jaguars"),
    "JAC": ("Jacksonville Jaguars",         "Jaguars"),
    "KC":  ("Kansas City Chiefs",           "Chiefs"),
    "KAN": ("Kansas City Chiefs",           "Chiefs"),
    "LV":  ("Las Vegas Raiders",            "Raiders"),
    "LVR": ("Las Vegas Raiders",            "Raiders"),
    "OAK": ("Las Vegas Raiders",            "Raiders"),
    "LAC": ("Los Angeles Chargers",         "Chargers"),
    "LAR": ("Los Angeles Rams",             "Rams"),
    "LA":  ("Los Angeles Rams",             "Rams"),
    "MIA": ("Miami Dolphins",               "Dolphins"),
    "MIN": ("Minnesota Vikings",            "Vikings"),
    "NE":  ("New England Patriots",         "Patriots"),
    "NWE": ("New England Patriots",         "Patriots"),
    "NO":  ("New Orleans Saints",           "Saints"),
    "NOR": ("New Orleans Saints",           "Saints"),
    "NYG": ("New York Giants",              "Giants"),
    "NYJ": ("New York Jets",                "Jets"),
    "PHI": ("Philadelphia Eagles",          "Eagles"),
    "PIT": ("Pittsburgh Steelers",          "Steelers"),
    "SEA": ("Seattle Seahawks",             "Seahawks"),
    "SF":  ("San Francisco 49ers",          "49ers"),
    "SFO": ("San Francisco 49ers",          "49ers"),
    "TB":  ("Tampa Bay Buccaneers",         "Buccaneers"),
    "TAM": ("Tampa Bay Buccaneers",         "Buccaneers"),
    "TEN": ("Tennessee Titans",             "Titans"),
    "WAS": ("Washington Commanders",        "Commanders"),
    "WSH": ("Washington Commanders",        "Commanders"),
}

WNBA_TEAMS: dict[str, tuple[str, str]] = {
    "ATL":  ("Atlanta Dream",               "Dream"),
    "CHI":  ("Chicago Sky",                 "Sky"),
    "CONN": ("Connecticut Sun",             "Sun"),
    "CON":  ("Connecticut Sun",             "Sun"),
    "DAL":  ("Dallas Wings",                "Wings"),
    "GV":   ("Golden State Valkyries",      "Valkyries"),
    "GSV":  ("Golden State Valkyries",      "Valkyries"),
    "IND":  ("Indiana Fever",               "Fever"),
    "LV":   ("Las Vegas Aces",              "Aces"),
    "LVA":  ("Las Vegas Aces",              "Aces"),
    "LA":   ("Los Angeles Sparks",          "Sparks"),
    "LAS":  ("Los Angeles Sparks",          "Sparks"),
    "MIN":  ("Minnesota Lynx",              "Lynx"),
    "NY":   ("New York Liberty",            "Liberty"),
    "NYL":  ("New York Liberty",            "Liberty"),
    "PHX":  ("Phoenix Mercury",             "Mercury"),
    "PHO":  ("Phoenix Mercury",             "Mercury"),
    "POR":  ("Portland Fire",               "Fire"),
    "SEA":  ("Seattle Storm",               "Storm"),
    "TOR":  ("Toronto Tempo",               "Tempo"),
    "WAS":  ("Washington Mystics",          "Mystics"),
    "WSH":  ("Washington Mystics",          "Mystics"),
}

# League code -> team table. Used by team_to_symbol when a league is
# known so we don't get cross-league collisions (e.g. "CLE" maps to
# Cleveland Guardians under MLB, Cleveland Cavaliers under NBA,
# Cleveland Browns under NFL).
LEAGUE_TEAM_TABLES: dict[str, dict[str, tuple[str, str]]] = {
    "MLB":  MLB_TEAMS,
    "NBA":  NBA_TEAMS,
    "NHL":  NHL_TEAMS,
    "NFL":  NFL_TEAMS,
    "WNBA": WNBA_TEAMS,
}

# Reverse lookups built at import time, one pair per league.
_LEAGUE_FULLNAME_TO_SYMBOL: dict[str, dict[str, str]] = {}
_LEAGUE_NICKNAME_TO_SYMBOL: dict[str, dict[str, str]] = {}
for _lg, _tbl in LEAGUE_TEAM_TABLES.items():
    _full_map: dict[str, str] = {}
    _nick_map: dict[str, str] = {}
    for _sym, (_full, _nick) in _tbl.items():
        _full_map.setdefault(_full.lower(), _sym)
        _nick_map.setdefault(_nick.lower(), _sym)
    _LEAGUE_FULLNAME_TO_SYMBOL[_lg] = _full_map
    _LEAGUE_NICKNAME_TO_SYMBOL[_lg] = _nick_map

# Legacy MLB-only aliases kept so any external caller still works.
_MLB_FULLNAME_TO_SYMBOL = _LEAGUE_FULLNAME_TO_SYMBOL["MLB"]
_MLB_NICKNAME_TO_SYMBOL = _LEAGUE_NICKNAME_TO_SYMBOL["MLB"]


# ---------------------------------------------------------------------------
# Person / team normalization
# ---------------------------------------------------------------------------

def normalize_person_name(name: str) -> str:
    """Lowercase, strip diacritics, drop punctuation. Used for player
    name comparisons across sources."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    n = n.lower()
    n = re.sub(r"[^\w\s]", "", n)
    return " ".join(n.split())


def team_to_symbol(name_or_symbol: Optional[str],
                   league: Optional[str] = None) -> Optional[str]:
    """Convert a team name OR symbol to its canonical league symbol.

    Tries full-name, then nickname, then last-word nickname. If `league`
    is provided, only that league's table is consulted (prevents
    cross-league collisions like CLE = Guardians/Cavaliers/Browns). If
    `league` is None, every league table is tried in turn and the first
    hit wins.

    Returns None if the input doesn't resolve in any consulted table.
    """
    if not name_or_symbol:
        return None
    s = name_or_symbol.strip()
    if not s:
        return None

    if league:
        lg = league.upper()
        tbl = LEAGUE_TEAM_TABLES.get(lg)
        if tbl is None:
            return None
        return _team_to_symbol_in_table(
            s, tbl,
            _LEAGUE_FULLNAME_TO_SYMBOL[lg],
            _LEAGUE_NICKNAME_TO_SYMBOL[lg])

    # No league hint — try each table in order. First successful match wins.
    for lg, tbl in LEAGUE_TEAM_TABLES.items():
        sym = _team_to_symbol_in_table(
            s, tbl,
            _LEAGUE_FULLNAME_TO_SYMBOL[lg],
            _LEAGUE_NICKNAME_TO_SYMBOL[lg])
        if sym:
            return sym
    return None


def _team_to_symbol_in_table(s: str,
                             tbl: dict[str, tuple[str, str]],
                             full_map: dict[str, str],
                             nick_map: dict[str, str]) -> Optional[str]:
    """Single-table resolution shared by team_to_symbol's two branches."""
    upper = s.upper()
    if upper in tbl:
        # Canonicalize aliases (e.g. CHI->CHC for MLB, BRK->BKN for NBA)
        full = tbl[upper][0]
        return full_map.get(full.lower(), upper)
    lower = s.lower()
    sym = full_map.get(lower)
    if sym:
        return sym
    sym = nick_map.get(lower)
    if sym:
        return sym
    toks = lower.split()
    if toks:
        if len(toks) >= 2:
            sym = nick_map.get(" ".join(toks[-2:]))
            if sym:
                return sym
        sym = nick_map.get(toks[-1])
        if sym:
            return sym
    return None


# ---------------------------------------------------------------------------
# Cross-source sport canonicalization
# ---------------------------------------------------------------------------
# The two feeds label the same contest differently: ProphetX tags a tennis
# match tournament="French Open (M)" / sport="Tennis"; Novig tags it
# league="ATP". A plain league-string equality (the old gate) can never pair
# them. canonical_sport() collapses both into a shared bucket. Team leagues
# return their own code unchanged, so MLB<->MLB and friends behave exactly as
# before — only non-team sports gain new matching ability.

_TEAM_LEAGUES = frozenset({
    "MLB", "NBA", "NHL", "NFL", "WNBA", "NCAAF", "NCAAB", "NCAABSB",
})

# Canonical buckets whose participants are individual people — matched by
# surname rather than a team-symbol table.
INDIVIDUAL_SPORTS = frozenset({"TENNIS", "GOLF", "MMA"})


def canonical_sport(league: Optional[str], sport: Optional[str],
                    event_name: Optional[str] = None) -> str:
    """Map a (league, sport) pair from either source to a shared bucket.

    Team leagues return their own uppercased code (so existing matching is
    untouched). Non-team sports collapse ATP/WTA->TENNIS, PGA->GOLF,
    UFC->MMA, MLS/EPL/soccer->SOCCER. Unknown inputs fall back to the
    league code, then the sport, so two genuinely-unrelated leagues still
    can't accidentally share a bucket.
    """
    lg = (league or "").upper().strip()
    sp = (sport or "").lower().strip()
    if lg in _TEAM_LEAGUES:
        return lg
    if lg in ("ATP", "WTA") or sp == "tennis":
        return "TENNIS"
    if lg == "PGA" or sp == "golf":
        return "GOLF"
    if lg == "UFC" or sp in ("mma", "mixed martial arts"):
        return "MMA"
    if lg in ("MLS", "EPL") or sp == "soccer":
        return "SOCCER"
    return lg or sp.upper()


# Trailing tournament-stage / round descriptors that Novig appends to the
# home player's name ("Ethan Quinn Round of 128") and that ProphetX appends
# as a parenthetical to golf matchups ("J.T. Poston (Round 4 Matchup)").
# Stripped before surname extraction so they don't pollute the last token.
_STAGE_RX = re.compile(
    r"\b("
    r"round of \d+|"
    r"\d+(?:st|nd|rd|th) qualifying round|"
    r"qualifying(?: round)?|qualification|"
    r"round \d+ matchup|matchup|"
    r"final|semifinal|quarterfinal"
    r")\b.*$"
)


def _person_surname(side: str) -> Optional[str]:
    """Normalize one participant name and return its surname (last token),
    after stripping any trailing tournament-stage descriptor."""
    norm = normalize_person_name(side)
    norm = _STAGE_RX.sub("", norm).strip()
    toks = norm.split()
    return toks[-1] if toks else None


def _extract_person_surnames(event: "NormalizedEvent") -> set[str]:
    """Surname set for an individual-sport event (tennis/golf/MMA).

    Parses '<A> @ <B>' / '<A> at <B>' / '<A> vs. <B>' out of the event
    name and reduces each side to its surname so divergent spellings
    ('Ketlen Souza' / 'K. Souza' / 'Souza') collapse to one key."""
    name = event.event_name or ""
    raw = event.raw if isinstance(event.raw, dict) else {}
    meta = raw.get("event_metadata") if isinstance(raw, dict) else None
    if isinstance(meta, dict):
        name = meta.get("name") or name
    out: set[str] = set()
    for sep in (" at ", " @ ", " vs. ", " vs "):
        if sep in name.lower():
            idx = name.lower().find(sep)
            for side in (name[:idx], name[idx + len(sep):]):
                sn = _person_surname(side)
                if sn:
                    out.add(sn)
            return out
    # No separator — fall back to a single surname (rare; e.g. an outright).
    sn = _person_surname(name)
    return {sn} if sn else set()


def extract_participants(event: "NormalizedEvent",
                         csport: Optional[str] = None) -> set[str]:
    """Unified participant key set for event matching.

    Team sports -> set of team symbols (via extract_team_symbols).
    Individual sports (TENNIS/GOLF/MMA) -> set of player surnames.
    """
    if csport is None:
        csport = canonical_sport(event.league, event.sport, event.event_name)
    if csport in INDIVIDUAL_SPORTS:
        return _extract_person_surnames(event)
    h, a = extract_team_symbols(event)
    return {s for s in (h, a) if s}


def extract_team_symbols(event: "NormalizedEvent"
                         ) -> tuple[Optional[str], Optional[str]]:
    """Extract (home_symbol, away_symbol) from a NormalizedEvent.

    Resolution order (most reliable first):
      1. Novig full team names (game.homeTeam.name / awayTeam.name) ->
         unambiguous because they include the city + nickname.
      2. Novig symbol fields (home_team_symbol / away_team_symbol) ->
         usually fine, but Novig ships ambiguous codes for intra-city
         matchups (both teams marked "LA" for LAD@LAA, both "CHI" for
         CHC@CHW). When symbols collide, fall through to (3).
      3. Parse the event name / description ("<Away> at <Home>" or
         "<Away> @ <Home>") and map each side through team_to_symbol.
    """
    lg = (event.league or "").upper() or None
    raw = event.raw or {}
    meta = raw.get("event_metadata") if isinstance(raw, dict) else None
    if isinstance(meta, dict):
        h_name = meta.get("home_team_name")
        a_name = meta.get("away_team_name")
        if h_name and a_name:
            h_sym = team_to_symbol(h_name, league=lg)
            a_sym = team_to_symbol(a_name, league=lg)
            if h_sym and a_sym and h_sym != a_sym:
                return (h_sym, a_sym)
        h = meta.get("home_team_symbol")
        a = meta.get("away_team_symbol")
        if h and a:
            h_sym = team_to_symbol(h, league=lg) or h
            a_sym = team_to_symbol(a, league=lg) or a
            if h_sym != a_sym:
                return (h_sym, a_sym)
        name = meta.get("name") or event.event_name
        return _parse_name_to_symbols(name, league=lg)
    return _parse_name_to_symbols(event.event_name, league=lg)


def _parse_name_to_symbols(name: str, league: Optional[str] = None
                           ) -> tuple[Optional[str], Optional[str]]:
    """Parse '<Away> at <Home>' or '<Away> @ <Home>' into symbol pair."""
    if not name:
        return (None, None)
    for sep in (" at ", " @ ", " vs ", " vs. "):
        if sep in name.lower():
            idx = name.lower().find(sep)
            left = name[:idx].strip()
            right = name[idx + len(sep):].strip()
            return (team_to_symbol(right, league=league),
                    team_to_symbol(left, league=league))
    return (None, None)


# ---------------------------------------------------------------------------
# Pair schema
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LinePair:
    line_a: NormalizedLine
    line_b: NormalizedLine


@dataclass(slots=True)
class MarketPair:
    market_a: NormalizedMarket
    market_b: NormalizedMarket
    line_pairs: list[LinePair] = field(default_factory=list)


@dataclass(slots=True)
class EventPair:
    event_a: NormalizedEvent
    event_b: NormalizedEvent
    confidence: float
    time_delta_minutes: float
    market_pairs: list[MarketPair] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------

def _event_match_score(ev_a: NormalizedEvent, ev_b: NormalizedEvent,
                       max_delta: timedelta) -> tuple[float, float]:
    """Return (confidence, time_delta_minutes) or (-1, +inf) if no match."""
    # Gate on a canonical sport bucket rather than raw league strings, so
    # the two feeds' divergent labels for the same contest (PX "French
    # Open (M)"/Tennis vs NV "ATP") still pair. Team leagues canonicalize
    # to their own code, so MLB<->MLB etc. is unchanged. An empty bucket
    # on either side means "unknown" — we don't reject on it, deferring to
    # the team/time signals below (preserves prior no-league behavior).
    csport_a = canonical_sport(ev_a.league, ev_a.sport, ev_a.event_name)
    csport_b = canonical_sport(ev_b.league, ev_b.sport, ev_b.event_name)
    if csport_a and csport_b and csport_a != csport_b:
        return (-1.0, float("inf"))

    t_a = _parse_iso(ev_a.scheduled_start)
    t_b = _parse_iso(ev_b.scheduled_start)
    if not t_a or not t_b:
        time_delta_min = float("inf")
        time_conf = 0.5
    else:
        if t_a.tzinfo is None:
            t_a = t_a.replace(tzinfo=timezone.utc)
        if t_b.tzinfo is None:
            t_b = t_b.replace(tzinfo=timezone.utc)
        delta = abs(t_a - t_b)
        if delta > max_delta:
            return (-1.0, delta.total_seconds() / 60.0)
        time_delta_min = delta.total_seconds() / 60.0
        time_conf = max(0.0, 1.0 - (delta.total_seconds() / max_delta.total_seconds()))

    # Participant keys: team symbols for team sports, player surnames for
    # individual sports (tennis/golf/MMA, which have no team table).
    set_a = extract_participants(ev_a, csport_a)
    set_b = extract_participants(ev_b, csport_b)
    if not set_a or not set_b:
        team_conf = 0.0
        if time_delta_min > 30.0:
            return (-1.0, time_delta_min)
    elif set_a == set_b:
        team_conf = 1.0
    elif set_a & set_b:
        team_conf = 0.5
    else:
        return (-1.0, time_delta_min)

    confidence = 0.4 * time_conf + 0.6 * team_conf
    return (confidence, time_delta_min)


def match_events(events_a: list[NormalizedEvent],
                 events_b: list[NormalizedEvent],
                 tolerance_minutes: float = 120.0,
                 min_confidence: float = 0.55) -> list[EventPair]:
    """For each event in `events_a`, find its best match in `events_b`
    (each B-event consumed at most once). Returns sorted by confidence
    descending."""
    max_delta = timedelta(minutes=tolerance_minutes)
    consumed_b: set[int] = set()

    candidates: list[tuple[float, float, int, int]] = []
    for i, ev_a in enumerate(events_a):
        for j, ev_b in enumerate(events_b):
            conf, dmin = _event_match_score(ev_a, ev_b, max_delta)
            if conf < min_confidence:
                continue
            candidates.append((conf, -dmin, i, j))
    candidates.sort(reverse=True)

    pairs: list[EventPair] = []
    consumed_a: set[int] = set()
    for conf, neg_dmin, i, j in candidates:
        if i in consumed_a or j in consumed_b:
            continue
        consumed_a.add(i)
        consumed_b.add(j)
        pair = EventPair(
            event_a=events_a[i], event_b=events_b[j],
            confidence=conf, time_delta_minutes=-neg_dmin,
        )
        _populate_market_pairs(pair)
        pairs.append(pair)
    return pairs


# ---------------------------------------------------------------------------
# Market matching
# ---------------------------------------------------------------------------

def _market_match_key(m: NormalizedMarket) -> Optional[tuple]:
    """Return a tuple usable as a dict key for matching, or None if the
    market can't be reliably matched (e.g. MTYPE_OTHER)."""
    if m.market_type == MTYPE_PLAYER_PROP:
        if not m.player_name or not m.market_subtype:
            return None
        return (MTYPE_PLAYER_PROP, m.market_subtype,
                normalize_person_name(m.player_name))
    if m.market_type in (MTYPE_MONEYLINE, MTYPE_SPREAD, MTYPE_TOTAL):
        sub = _canonical_game_subtype(m.market_subtype)
        return (m.market_type, sub)
    return None


def _canonical_game_subtype(s: Optional[str]) -> str:
    """Collapse source-specific subtype strings into shared tokens.

    ProphetX subType: "moneyline", "spread", "total",
                      "first_half_moneyline", "first_half_spread",
                      "first_half_total".
    Novig type:       "MONEY", "SPREAD", "TOTAL", "MONEY_1H",
                      "SPREAD_1H", "TOTAL_1H", "TEAM_TOTAL",
                      "FIRST_INNING_TOTAL".

    The two sources spell the first-half period differently — ProphetX
    prefixes "first_half_", Novig suffixes "_1H". Both are reconciled to
    a trailing "_1H" so a full-game market and its 1H sibling get
    DISTINCT keys. (The old type-token-only logic collapsed them, so a
    1H market could be paired against a full-game one.) "moneyline" is
    folded to "MONEY" to meet Novig's spelling; every other token
    (TEAM_TOTAL, FIRST_INNING_TOTAL, quarter variants, …) is kept
    verbatim so unrelated markets never share a key.
    """
    if not s:
        return ""
    u = s.upper().replace(" ", "_")
    is_1h = ("1H" in u) or ("FIRST_HALF" in u)
    base = (u.replace("FIRST_HALF_", "")
             .replace("_1H", "")
             .replace("1H_", ""))
    if base == "MONEYLINE":
        base = "MONEY"
    return f"{base}_1H" if is_1h else base


def _populate_market_pairs(pair: EventPair) -> None:
    """Build the MarketPair list for a paired event. Mutates `pair`."""
    by_key_b: dict[tuple, list[NormalizedMarket]] = {}
    for m in pair.event_b.markets:
        k = _market_match_key(m)
        if k is None:
            continue
        by_key_b.setdefault(k, []).append(m)

    for m_a in pair.event_a.markets:
        k = _market_match_key(m_a)
        if k is None:
            continue
        candidates = by_key_b.get(k)
        if not candidates:
            continue
        m_b = candidates.pop(0)
        mpair = MarketPair(market_a=m_a, market_b=m_b)
        _populate_line_pairs(mpair)
        pair.market_pairs.append(mpair)


# ---------------------------------------------------------------------------
# Line matching
# ---------------------------------------------------------------------------

_STRIKE_EPS = 1e-6


def _populate_line_pairs(mpair: MarketPair) -> None:
    by_strike_b: dict[Optional[float], NormalizedLine] = {}
    for ln in mpair.market_b.lines:
        by_strike_b[ln.strike] = ln
    for ln_a in mpair.market_a.lines:
        ln_b = by_strike_b.get(ln_a.strike)
        if ln_b is None and ln_a.strike is not None:
            for s, candidate in by_strike_b.items():
                if s is None:
                    continue
                if abs(s - ln_a.strike) < _STRIKE_EPS:
                    ln_b = candidate
                    break
        if ln_b is None:
            continue
        mpair.line_pairs.append(LinePair(line_a=ln_a, line_b=ln_b))


# ---------------------------------------------------------------------------
# Convenience iterators
# ---------------------------------------------------------------------------

def iter_line_pairs(event_pairs: Iterable[EventPair]
                    ) -> Iterator[tuple[EventPair, MarketPair, LinePair]]:
    """Flat iterator over every (event, market, line) match triple."""
    for ep in event_pairs:
        for mp in ep.market_pairs:
            for lp in mp.line_pairs:
                yield ep, mp, lp


# ---------------------------------------------------------------------------
# Convenience: build NormalizedEvents directly from on-disk dumps
# ---------------------------------------------------------------------------

def load_prophetx_normalized_events(dump_data: dict,
                                    league_filter: Optional[str] = "MLB"
                                    ) -> list[NormalizedEvent]:
    """Convert a ProphetX combined-dump dict into a list of
    NormalizedEvents filtered by tournament. `raw` is preserved on each
    event so extract_team_symbols can read event_metadata."""
    out: list[NormalizedEvent] = []
    for _eid, ev in dump_data.items():
        meta = ev.get("event_metadata") or {}
        if league_filter and meta.get("tournament") != league_filter:
            continue
        nev = from_prophetx_event(ev)
        nev.raw = ev
        out.append(nev)
    return out


def load_novig_normalized_events(dump_data: dict,
                                 league_filter: Optional[str] = "MLB",
                                 currency: str = "CASH"
                                 ) -> list[NormalizedEvent]:
    """Convert a Novig combined-dump dict into a list of NormalizedEvents.
    The dump's flat markets list is wrapped in a synthetic event node so
    `from_novig_event` can be reused without rewriting traversal."""
    out: list[NormalizedEvent] = []
    for _eid, ev in dump_data.items():
        meta = ev.get("event_metadata") or {}
        if league_filter and meta.get("tournament") != league_filter:
            continue
        node = {
            "id": meta.get("id"),
            "description": meta.get("name"),
            "scheduled_start": meta.get("startTime"),
            "league": meta.get("tournament"),
            "game": {"sport": meta.get("sport"),
                     "homeTeam": {"symbol": meta.get("home_team_symbol"),
                                  "name": meta.get("home_team_name")},
                     "awayTeam": {"symbol": meta.get("away_team_symbol"),
                                  "name": meta.get("away_team_name")}},
            "markets": (ev.get("data") or {}).get("markets") or [],
            "events": [],
        }
        nev = from_novig_event(node, books=None, currency=currency,
                               league=league_filter)
        nev.raw = ev
        out.append(nev)
    return out


# ===========================================================================
# Self-test
# ===========================================================================

def _print_market(nm: NormalizedMarket, max_orders_per_side: int = 4) -> None:
    print(f"\n  [{nm.source}]  {nm.market_name}")
    print(f"    type={nm.market_type}/{nm.market_subtype}  "
          f"player={nm.player_name}  lines={len(nm.lines)}  "
          f"liquidity=${nm.total_liquidity_usd:,.2f}")
    for ln in nm.lines[:5]:
        strike_disp = "ML" if ln.strike is None else ln.line_label
        print(f"    line[{strike_disp:>6s}]  liq=${ln.total_liquidity_usd:,.2f}")
        for side in ln.sides:
            label_disp = side.label[:28]
            print(f"      side[{side.side_type:8s}] {label_disp:28s}  "
                  f"n={len(side.orders)}  ${side.total_size_usd:,.2f}")
            for o in side.orders[:max_orders_per_side]:
                am = f"+{o.american}" if o.american > 0 else str(o.american)
                print(f"          {am:>6s}  p={o.prob:.4f}  ${o.size_usd:,.2f}")
            if len(side.orders) > max_orders_per_side:
                print(f"          ... +{len(side.orders) - max_orders_per_side} more")
    if len(nm.lines) > 5:
        print(f"    ... +{len(nm.lines) - 5} more lines")


def _self_test_prophetx() -> None:
    import json, pathlib
    dump_dir = pathlib.Path(__file__).parent / "prophetx_dumps"
    if not dump_dir.exists():
        print("[skip prophetx] no prophetx_dumps/ directory")
        return
    files = sorted(dump_dir.glob("all_markets_combined_*.json"),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        print("[skip prophetx] no combined dump files")
        return
    latest = files[-1]
    print(f"\n=== ProphetX adapter — sample from {latest.name} ===")
    data = json.loads(latest.read_text())
    # Walk events until we find one with both moneyline and a strike-ladder
    # market, so the line aggregation is visible.
    for eid, ev in data.items():
        nev = from_prophetx_event(ev)
        if not nev.markets:
            continue
        print(f"\nEvent: {nev.event_name} ({nev.sport})  "
              f"{len(nev.markets)} markets")
        seen_types: set[str] = set()
        for m in nev.markets:
            if m.market_type in seen_types:
                continue
            seen_types.add(m.market_type)
            _print_market(m)
            if len(seen_types) >= 3:
                break
        break


def _self_test_novig(event_id: Optional[str]) -> None:
    if not event_id:
        print("\n[skip novig] no --novig-event UUID provided")
        return
    try:
        from NovigClient import NovigClient, NovigQueries
    except Exception as e:
        print(f"\n[skip novig] cannot import NovigClient: {e}")
        return
    print(f"\n=== Novig adapter — live fetch event {event_id} ===")
    client = NovigClient()
    q = NovigQueries(client)
    ev_node = q.get_event_markets(event_id, only_available=True)
    if not ev_node:
        print("  event not found")
        return

    # First pass: no books — show the aggregated structure.
    nev = from_novig_event(ev_node, books=None, currency="CASH")
    print(f"\nEvent: {nev.event_name}  ({nev.league}, {nev.sport})  "
          f"{len(nev.markets)} aggregated markets")

    seen: set[str] = set()
    for m in nev.markets:
        # Show a moneyline, a total/spread, and a player prop if available
        if m.market_type in seen:
            continue
        seen.add(m.market_type)
        _print_market(m)
        if len(seen) >= 3:
            break

    # Second pass: pick one TOTAL market group and fetch books for every
    # line so the multi-strike ladder shape becomes visible.
    target_group: Optional[NormalizedMarket] = None
    for m in nev.markets:
        if m.market_type == MTYPE_TOTAL and len(m.lines) >= 2:
            # Only consider a group with at least some liquid lines (i.e.
            # not all is_consensus). Use line ids to fetch books.
            target_group = m
            break
    if target_group is not None:
        line_ids = [ln.source_line_id for ln in target_group.lines
                    if ln.source_line_id]
        try:
            books_resp = client.get_market_books(line_ids[:8], currency="CASH")
        except Exception as e:
            print(f"\n  [book fetch failed] {e}")
            return
        books_map = {b.get("market", {}).get("id"): b
                     for b in books_resp if b.get("market")}
        # Re-normalize with books supplied
        nev2 = from_novig_event(ev_node, books=books_map, currency="CASH")
        # Find the same group again by source_market_id
        m2 = next((m for m in nev2.markets
                   if m.source_market_id == target_group.source_market_id),
                  None)
        if m2 is not None:
            print(f"\n  --- same TOTAL group re-normalized with /book/batch ---")
            _print_market(m2, max_orders_per_side=6)


def _self_test_match() -> None:
    """Load latest ProphetX + Novig dumps from disk, match MLB events,
    print a summary of the pairing."""
    import pathlib, json
    here = pathlib.Path(__file__).parent

    px_files = sorted((here / "prophetx_dumps").glob("all_markets_combined_*.json"),
                      key=lambda p: p.stat().st_mtime)
    nv_files = sorted((here / "novig_dumps").glob("all_events_combined_*.json"),
                      key=lambda p: p.stat().st_mtime)
    if not px_files:
        print("\n[skip match] no ProphetX dump")
        return
    if not nv_files:
        print("\n[skip match] no Novig dump — run NovigClient --scrape-dump first")
        return

    px_data = json.loads(px_files[-1].read_text())
    nv_data = json.loads(nv_files[-1].read_text())
    print(f"\nProphetX dump: {px_files[-1].name}  ({len(px_data)} events)")
    print(f"Novig dump:    {nv_files[-1].name}  ({len(nv_data)} events)")

    px_events = load_prophetx_normalized_events(px_data, league_filter="MLB")
    nv_events = load_novig_normalized_events(nv_data, league_filter="MLB",
                                             currency="CASH")
    print(f"\nMLB ProphetX events: {len(px_events)}")
    print(f"MLB Novig events:    {len(nv_events)}")

    pairs = match_events(px_events, nv_events)
    print(f"\nMatched event pairs: {len(pairs)}")
    for ep in pairs:
        print(f"  [{ep.confidence:.2f}, dt={ep.time_delta_minutes:.0f}m] "
              f"PX: {ep.event_a.event_name[:35]:35s}  <-> "
              f"NV: {ep.event_b.event_name[:25]:25s}  "
              f"markets={len(ep.market_pairs)}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Exchange market keys + matcher self-test")
    ap.add_argument("--novig-event", metavar="UUID",
                    help="If provided, fetch this Novig event live and "
                         "normalize it (network call).")
    ap.add_argument("--prophetx-only", action="store_true",
                    help="Skip the Novig live test.")
    ap.add_argument("--skip-match", action="store_true",
                    help="Skip the cross-source matcher self-test.")
    args = ap.parse_args()

    _self_test_prophetx()
    if not args.prophetx_only:
        _self_test_novig(args.novig_event)
    if not args.skip_match:
        _self_test_match()
