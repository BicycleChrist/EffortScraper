"""
OddsPortalClient.py — fast, threaded client for OddsPortal's internal feed.

OddsPortal (a LiveSport s.r.o. property, same parent as Flashscore) has no public
API. Its front end either embeds initial data into the page HTML or pulls it from
internal `/ajax-...` and `/feed/...` endpoints. This client talks to those
endpoints directly with plain `requests` — no browser is needed at runtime.

*** 2026-08 REWRITE ***
The site moved from a Vue SPA to Next.js. Two things changed, and between them
they took every surface here offline:

  a) The encrypted endpoints kept their names, payloads and AES parameters, but
     are now reachable ONLY behind a `/proxy` prefix (the Next server proxies
     them on to backend.oddsportal.com). The old paths still return 200 — with
     the SPA's HTML shell — so the failure surfaced as a decode error, not a
     404. `_api_path` adds the prefix for every caller.

  b) Data that used to be inlined in the HTML for a Vue custom element
     (<sports-menu>, <search-results-wrapper>, `var pageVar`) is now either in
     the page's React-Flight payload (`_next_payload`) or behind a new
     encrypted endpoint. Per-event feed tokens come from
     /ajax-event-data-shell/<encodedId>/0; the dropping-odds board is rendered
     server-side and is read straight out of the Flight payload.

Three data surfaces are exposed:

  1. SPORTS + LEAGUES  (the full taxonomy on offer)
       - sports are parsed from the homepage's Next.js Flight payload
       - leagues per sport come from
             /ajax-getSportsMenuDataBySports/<sportId>,/<tz>/
         which returns an ENCRYPTED payload (see decode_feed below).

  2. PER-EVENT ODDS  (the historical-odds surface)
       - /ajax-event-data-shell/<encodedId>/0 yields the event's xhash/xhashf
         and ids, from which the /match-event/<token>.dat feed path is built.
       - resolve a matchup to an event with resolve_event_url(); the search
         result pages themselves went dark in 2026-07 (see _resolve_via_h2h).

  3. DROPPING ODDS  (the /dropping-odds/ board)
       - server-rendered; parsed out of the page's Flight payload. NOTE: the
         encrypted feed that used to back this is retired (it answers
         {"E":"notAllowed"} for every hash the site exposes) and with it went
         pagination — the board is now a single page of ~20 rows per sport.

----------------------------------------------------------------------------
ENCRYPTED FEED FORMAT  (the one thing that can break without notice)
----------------------------------------------------------------------------
The `/feed/...` and several `/ajax-...` endpoints return a response that is:

    base64(  <ciphertext_base64> ":" <iv_hex>  )

Decode steps (mirrors the crypto helper in the app JS bundle):
    outer      = base64_decode(response_text).decode("latin1")
    ct_b64, iv = outer.split(":")
    key        = PBKDF2-HMAC-SHA256(PASSPHRASE, SALT, ITERATIONS=1000, dklen=32)
    plaintext  = AES-256-CBC(key, iv=bytes.fromhex(iv)).decrypt(b64decode(ct_b64))
    plaintext  = pkcs7_unpad(plaintext)
    if plaintext starts with gzip magic (1f 8b): plaintext = gunzip(plaintext)
    json.loads(plaintext)

The PASSPHRASE / SALT live (obfuscated) in the app JS bundle. If decoding ever
starts failing, re-derive them: download the chunks referenced by
<script src="/_next/static/chunks/*.js"> (formerly /build/assets/app-*.js),
search for the string-array function that contains "AES-CBC"/"PBKDF2"/
"deriveKey", and read off the joined passphrase and the hex salt, then update
the constants below. They have been stable — they survived the Next.js rewrite
untouched — so they are hardcoded, not discovered at runtime.

Before assuming the crypto rotated, check the cheaper explanation: a decode
error whose text mentions non-ASCII input almost always means the request went
to a path that answered with HTML (see the /proxy prefix note above).

----------------------------------------------------------------------------
USAGE
----------------------------------------------------------------------------
    c = OddsPortalClient()
    sports  = c.get_sports()                      # [Sport, ...]
    leagues = c.get_leagues(sport_id=1)           # [League, ...]  (football)
    matches = c.search("Lakers")                  # [SearchMatch, ...]
    drops   = c.dropping_odds(sport_id=0)         # [DroppingOdd, ...]

CLI:
    python OddsPortalClient.py sports
    python OddsPortalClient.py leagues --sport 1
    python OddsPortalClient.py search "Lakers" --pages 2
    python OddsPortalClient.py dropping --sport 0 --period 2 --bs 2
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import html as _html
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---------------------------------------------------------------------------
# Constants. The crypto params are the only fragile bit — see module docstring.
# ---------------------------------------------------------------------------
BASE = "https://www.oddsportal.com"

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

# AES/PBKDF2 params read out of build/assets/app-*.js (string-array w/ "AES-CBC").
DEFAULT_PASSPHRASE = b"J*8sQ!p$7aD_fR2yW@gHn*3bVp#sAdLd_k"
DEFAULT_SALT = b"5b9a8f2c3e6d1a4b7c8e9d0f1a2b3c4d"
PBKDF2_ITERATIONS = 1000

# Timezone bucket used in the leagues-menu path. Only affects day grouping; the
# odds themselves are absolute. "-8" == the value the live site sends for US.
DEFAULT_TZ = "-8"

# Canonical OddsPortal sport ids (stable; verified against the live sports-menu).
SPORT_IDS: Dict[str, int] = {
    "football": 1,      # soccer
    "tennis": 2,
    "basketball": 3,
    "hockey": 4,
    "american-football": 5,
    "baseball": 6,
    "handball": 8,
    "rugby-union": 9,
    "boxing": 11,
    "rugby-league": 12,
    "esports": 13,
    "darts": 14,
    "snooker": 15,
    "volleyball": 16,
    "cricket": 18,
    "futsal": 19,
    "floorball": 21,
    "mma": 22,
    "table-tennis": 28,
    "badminton": 30,
    "aussie-rules": 36,
}

# Dropping-odds filter dimensions (from the dropping-odds-filter :data payload).
DROPPING_PERIODS = {1: "Last 1 hour", 2: "Last 12 hours", 3: "Last 24 hours"}
DROPPING_MIN_BS = {1: "10%", 2: "20%", 3: "30%", 4: "40%", 5: "50%"}

# Max concurrent search-results page fetches. Each page is a ~340KB HTML doc;
# firing all of them at once trips OddsPortal's rate limit (it drops the
# connection). 5 keeps the fetch parallel-but-polite so pages don't get lost.
SEARCH_PAGE_CONCURRENCY = 5


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
def fmt_odd(v: Any) -> str:
    """Format a decimal odd for display: 2dp, trailing zeros trimmed, so
    1.8199999999999998 → '1.82', 12.0 → '12', None → '—'."""
    if v is None:
        return "—"
    try:
        s = f"{float(v):.2f}".rstrip("0").rstrip(".")
        return s or "0"
    except (ValueError, TypeError):
        return str(v)


# OddsPortal's feeds ALWAYS return DECIMAL odds — the site's odds-format dropdown
# is pure client-side conversion (verified: the OddsFormatID cookie / feed <fmt>
# param don't change the payload). So we convert ourselves; it's a free, exact,
# deterministic transform with no extra request.
def to_american(dec: Any) -> Optional[int]:
    """Decimal → American/moneyline. 2.50 → +150, 1.40 → -250. None if <=1."""
    try:
        d = float(dec)
    except (ValueError, TypeError):
        return None
    if d <= 1:
        return None
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def to_fractional(dec: Any) -> Optional[str]:
    """Decimal → fractional (traditional for horse racing). Fraction's
    limit_denominator lands on the standard ladder (3.5→'5/2', 1.8→'4/5')
    without maintaining a lookup table. None if <=1."""
    from fractions import Fraction
    try:
        d = float(dec)
    except (ValueError, TypeError):
        return None
    if d <= 1:
        return None
    fr = Fraction(d - 1).limit_denominator(20)
    return f"{fr.numerator}/{fr.denominator}"


def format_odd(value: Any, fmt: str = "dec") -> str:
    """Format one decimal odd in the requested display format: 'dec' (decimal),
    'us' (American/moneyline), 'frac' (fractional — horse racing)."""
    if value is None:
        return "—"
    if fmt == "us":
        a = to_american(value)
        return "—" if a is None else (f"+{a}" if a > 0 else str(a))
    if fmt == "frac":
        return to_fractional(value) or "—"
    return fmt_odd(value)


@dataclass
class Sport:
    id: int
    name: str
    url: str


@dataclass
class League:
    sport_id: int
    country: str
    country_id: Optional[int]
    name: str
    url: str
    tournament_id: Optional[int]
    count: Optional[str] = None  # e.g. "(10)" upcoming matches


@dataclass
class OutcomeOdds:
    """Aggregated odds for one outcome of a market, across bookmakers."""
    outcome_result_id: Optional[int]
    betting_type_id: Optional[int]
    scope_id: Optional[int]
    avg_odds: Optional[float]
    max_odds: Optional[float]
    max_odds_provider_id: Optional[int]
    active: Optional[bool]
    bookmaker_count: Optional[int]


@dataclass
class SearchMatch:
    id: int
    sport_id: int
    sport: str
    home: str
    away: str
    tournament: str
    tournament_url: str
    country: str
    start_ts: Optional[int]
    status: str
    result: str
    url: str
    bookmaker_count: Optional[int]
    odds: List[OutcomeOdds] = field(default_factory=list)


@dataclass
class DroppingOdd:
    # identity key for dedup: the rendered board exposes an encoded market id
    # (str) where the retired feed carried a numeric event xuid
    event_id: Any
    sport: str
    country: str
    tournament: str
    home: str
    away: str
    date: str
    time: str
    event_url: str
    betting_type: str
    drop: str                # e.g. "-49%"
    bookies: str             # e.g. "1/2"
    max_odds: Optional[float]
    max_provider: Optional[str]
    outcomes: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def drop_pct(self) -> float:
        """The % move as a float (e.g. '-49%' -> -49.0). 0.0 if unparseable."""
        try:
            return float(self.drop.replace("%", "").replace("+", "").strip())
        except (ValueError, AttributeError):
            return 0.0

    @property
    def dropped_outcome(self) -> Optional[Dict[str, Any]]:
        """The outcome that moved (the one carrying an opening price)."""
        for o in self.outcomes:
            if o.get("prev_odd") is not None:
                return o
        return None

    @property
    def odds_str(self) -> str:
        """Compact odds view, e.g. '2: 6.87→2.67 | 1 3.05 | X 2.45'.
        The dropped outcome (old→new) is listed first. Odds are rounded to 2dp
        to kill float artifacts (1.8199999999999998 → 1.82)."""
        moved = self.dropped_outcome
        parts = []
        if moved:
            parts.append(f"{moved['name']}: {fmt_odd(moved['prev_odd'])}"
                         f"→{fmt_odd(moved['odd'])}")
        for o in self.outcomes:
            if o is moved:
                continue
            if o.get("odd") is not None:
                parts.append(f"{o['name']} {fmt_odd(o['odd'])}")
        return "  |  ".join(parts)


@dataclass
class OutcomeBook:
    """One outcome's odds across bookmakers (current + opening)."""
    name: str                       # "1" / "X" / "2" (or home/away label)
    avg_odds: Optional[float]       # mean of current odds across books
    max_odds: Optional[float]       # best current odds
    max_book: Optional[str]         # bookmaker offering max_odds
    opening_avg: Optional[float]    # mean of opening odds across books
    n_books: int                    # how many books priced this outcome
    books: Dict[str, float] = field(default_factory=dict)        # name -> current
    opening: Dict[str, float] = field(default_factory=dict)      # name -> opening
    # Per-book UNIX timestamps: when this book first hung the price and when it
    # last moved it. The feed has carried these all along and they were being
    # dropped — they are the time axis a closing-line-value study needs, since
    # "opening" and "closing" are otherwise just two numbers with no clock.
    opened_at: Dict[str, int] = field(default_factory=dict)
    changed_at: Dict[str, int] = field(default_factory=dict)
    # "up" / "down" per book — direction of the most recent move.
    movement: Dict[str, str] = field(default_factory=dict)

    @property
    def drift(self) -> Optional[float]:
        """Avg current vs avg opening as a fraction (e.g. -0.18 = shortened 18%)."""
        if self.avg_odds and self.opening_avg:
            return (self.avg_odds - self.opening_avg) / self.opening_avg
        return None


# Betting-type id -> slug, read straight off `eventData.bt_hash`, which is a
# comma-separated list whose INDEX is the betting-type id:
#   ",#1X2,#over-under,#home-away,#double,#ah,#dnb,#qualify,#cs,#ht-ft,
#    #odd-even,#league-winner,#eh,#bts"
# The client used to fetch only `defaultBettingType` (3 = home/away for
# baseball), which is why it returned moneylines and nothing else. The feed
# token takes the bet id directly and the event's `xhashf` salt is
# market-INDEPENDENT, so every one of these is reachable with no extra
# handshake — see `event_markets`.
BETTING_TYPES: Dict[int, str] = {
    1: "1X2", 2: "over-under", 3: "home-away", 4: "double-chance",
    5: "asian-handicap", 6: "draw-no-bet", 7: "qualify", 8: "correct-score",
    9: "ht-ft", 10: "odd-even", 11: "league-winner", 12: "european-handicap",
    13: "btts",
}
# Markets that carry a line (one feed block per handicap value) rather than a
# single block. Totals on a baseball game return ~23 blocks, 2.5 through 13.5.
LINE_MARKETS = frozenset({2, 5, 12})

# Scope = the slice of the game a price covers. The client only ever fetched
# `defaultScope`, so period markets were invisible too.
#
# Scope ids are NOT comparable across sports and the meanings must be MEASURED,
# not assumed. Baseball's whole-game scope is 1; **soccer has no scope 1 at
# all** — its whole-match scope is 2, with 3/4 the halves. A first guess that
# mapped football 2 -> "1st-half" produced a market list with no full-time
# prices in it and looked entirely reasonable.
#
# So only verified sports get names here. Everything else takes its whole-game
# label from the event shell's OWN `defaultScope`, which is authoritative, and
# is otherwise reported as a bare id rather than a guess.
#
# Baseball verified by consensus total on one MLB game: scope 1 -> 8.0 over 23
# lines (full game), 2 -> 7.5 (regulation, no extras), 3 -> 4.0 over 12 lines,
# exactly half, i.e. first five innings.
SCOPES: Dict[str, Dict[int, str]] = {
    "baseball": {1: "full-game", 2: "regulation", 3: "first-5-innings"},
}


def scope_name(sport: str, scope_id: int,
               default_scope: Optional[int] = None) -> str:
    """Label a scope. Verified sports use their measured map; otherwise the
    event's own default scope IS the whole-game one and the rest stay bare."""
    known = SCOPES.get((sport or "").lower())
    if known and scope_id in known:
        return known[scope_id]
    if default_scope is not None and scope_id == default_scope:
        return "full-game"
    return f"scope-{scope_id}"

# What each market's outcome COLUMNS mean. Verified empirically for over-under
# rather than assumed: across 23 total lines on one game, column 0 rose
# monotonically with the line (1.01 at 2.5 -> 7.25 at 13.5) while column 1
# fell (13.0 -> 1.08), so column 0 is Over. Guessing this backwards would
# invert every totals price while looking entirely plausible.
_MARKET_LABELS: Dict[int, List[str]] = {
    2: ["Over", "Under"],
    10: ["Odd", "Even"],
    13: ["Yes", "No"],
    4: ["1X", "12", "X2"],
}


@dataclass
class MarketLine:
    """One priced line within a market — e.g. totals 8.5, or run line -1.5.

    `handicap` is None for markets that have a single block (moneyline, BTTS).
    For asian/european handicaps it is signed from the HOME side."""
    betting_type_id: int
    market: str                     # slug from BETTING_TYPES
    handicap: Optional[float]
    outcomes: List[OutcomeBook] = field(default_factory=list)
    scope_id: int = 1
    scope: str = "full-game"

    @property
    def n_books(self) -> int:
        return max((o.n_books for o in self.outcomes), default=0)

    @property
    def label(self) -> str:
        """Human line label. A TOTAL is unsigned (8.5), a HANDICAP is signed
        from the home side (-1.5) — printing a total as "+8.5" reads as a
        handicap and is the kind of thing that gets copied into a bet slip."""
        if self.handicap is None:
            return self.market
        if self.betting_type_id in (5, 12):
            return f"{self.market} {self.handicap:+g}"
        return f"{self.market} {self.handicap:g}"

    def __repr__(self) -> str:
        legs = "  ".join(f"{o.name} {o.avg_odds}" for o in self.outcomes)
        sc = "" if self.scope_id == 1 else f" [{self.scope}]"
        return f"<{self.label}{sc}  {legs}  ({self.n_books} books)>"


@dataclass
class EventOdds:
    """Full per-bookmaker odds for one event (one betting type / scope), pulled
    from the /match-event/ feed. The historical-odds surface, restored."""
    event_url: str
    sport: str
    home: str
    away: str
    tournament: str
    start_ts: Optional[int]
    betting_type_id: int
    scope_id: int
    outcomes: List[OutcomeBook] = field(default_factory=list)
    book_logos: Dict[str, str] = field(default_factory=dict)  # book name -> logo url
    live: bool = False              # `outcomes` came from the in-play feed
    # for in-play events: the frozen pregame (closing) odds alongside the live
    # ones, so both can be shown. Empty when not live.
    pre_outcomes: List[OutcomeBook] = field(default_factory=list)
    # Additional markets (totals, handicaps, ...) when the caller asked for
    # them. Empty by default so every existing consumer is untouched.
    markets: List[MarketLine] = field(default_factory=list)

    @property
    def main_scope(self) -> int:
        """The whole-game scope id for THIS event — 1 for baseball, 2 for
        soccer. Taken from the event's own default rather than assumed."""
        return self.scope_id or 1

    def lines(self, market: str, scope_id: Optional[int] = -1
              ) -> List[MarketLine]:
        """Every priced line for one market slug, ascending by handicap.

        Defaults to whole-game (`scope_id=1`). Pass None for every scope —
        without the filter a first-five-innings total sits in the same list as
        the full-game one and `main_line` can pick the wrong game entirely."""
        if scope_id == -1:
            scope_id = self.main_scope
        out = [m for m in self.markets if m.market == market
               and (scope_id is None or m.scope_id == scope_id)]
        return sorted(out, key=lambda m: (m.scope_id, m.handicap is None,
                                          m.handicap))

    def main_line(self, market: str, scope_id: int = -1) -> Optional[MarketLine]:
        """The market's CONSENSUS line — the one books actually hang.

        A totals feed returns every line the whole book set quotes, 2.5
        through 13.5 on a baseball game, and the tails are one stale book
        apiece. The real number is the line priced closest to even money by
        the most books, which is what a projection has to be compared
        against; taking the median handicap or the first block instead picks
        up a tail and quietly misprices the whole comparison.

        Ranks on book count first, then on how balanced the two sides are.
        """
        best, best_key = None, None
        for ln in self.lines(market, scope_id):
            prices = [o.avg_odds for o in ln.outcomes if o.avg_odds]
            if len(prices) < 2 or ln.n_books == 0:
                continue
            balance = abs(max(prices) - min(prices))
            key = (ln.n_books, -balance)
            if best_key is None or key > best_key:
                best, best_key = ln, key
        return best

    def line_at(self, market: str, handicap: float,
                scope_id: int = -1) -> Optional[MarketLine]:
        if scope_id == -1:
            scope_id = self.main_scope
        for m in self.markets:
            if m.market == market and m.scope_id == scope_id \
                    and m.handicap is not None \
                    and abs(m.handicap - handicap) < 1e-9:
                return m
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class OddsPortalClient:
    def __init__(self, passphrase: bytes = DEFAULT_PASSPHRASE,
                 salt: bytes = DEFAULT_SALT, tz: str = DEFAULT_TZ,
                 max_workers: int = 8, timeout: int = 20, verbose: bool = False,
                 proxy: Optional[str] = None):
        """proxy: an http(s)/socks proxy URL applied to every request, e.g.
        "http://user:pass@host:port" or "socks5h://host:port". OddsPortal
        geo-filters the bookmaker set by egress IP, so routing through a UK/EU/
        Asia proxy surfaces books (Pinnacle, Asian books, exchanges) that are
        hidden from a US IP. Falls back to the ODDSPORTAL_PROXY env var.
        """
        self.tz = tz
        self.timeout = timeout
        self.max_workers = max_workers
        self.verbose = verbose
        self._key = hashlib.pbkdf2_hmac("sha256", passphrase, salt,
                                        PBKDF2_ITERATIONS, dklen=32)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
        proxy = proxy or os.environ.get("ODDSPORTAL_PROXY")
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
            self._log(f"routing through proxy {proxy}")
        # per-event caches: the event PAGE (xhashf etc.) is stable so cache it
        # long; the odds FEED moves, so cache the result only briefly to dedupe
        # rapid re-clicks. Avoids re-fetching the ~160KB event HTML every click.
        self._event_cfg_cache: Dict[str, tuple] = {}    # url -> (ts, cfg)
        self._event_odds_cache: Dict[str, tuple] = {}   # url -> (ts, EventOdds)
        self._event_cfg_ttl = 300
        self._event_odds_ttl = 15
        # participant search is the slowest OddsPortal endpoint (~4s server
        # side); cache per query so repeated resolves don't re-pay it
        self._search_cache: Dict[str, tuple] = {}       # query -> (ts, parts)
        self._search_ttl = 600
        # sport match listings, used to resolve matchups auto-search can't see
        # (doubles). One page covers every game of the day, so a short TTL is
        # enough to keep a screen full of rows to a single fetch.
        self._listing_cache: Dict[str, tuple] = {}      # sport -> (ts, rows)
        self._listing_ttl = 300
        # An EMPTY listing is remembered longer. Since 2026-10 /matches/<sport>/
        # server-renders no match rows (they load behind OddsPortal's anti-bot
        # proof step, which we do not try to defeat), so every resolve paid a
        # ~534KB / ~1.5s fetch per sport every 5 min for a guaranteed miss.
        # Recovers on its own if rows come back.
        self._listing_empty_ttl = 1800

    def geo(self) -> Optional[str]:
        """The country code OddsPortal sees for our (possibly proxied) egress IP.
        Determines which bookmakers are shown. Returns e.g. 'US', 'GB', 'SG'."""
        r = self._get("/")
        return r.headers.get("x-country-code")

    def _log(self, *a):
        if self.verbose:
            print("[oddsportal]", *a, file=sys.stderr)

    # -- low-level fetch / decode -------------------------------------------
    # OddsPortal moved its front end to Next.js (2026-08). The encrypted data
    # endpoints survived UNCHANGED — same AES key/salt, same payload shapes —
    # but they are now reached through a `/proxy` prefix, because the Next
    # server proxies them to backend.oddsportal.com. Everything that used to
    # live at /feed/..., /ajax-..., /match-event/... now answers only at
    # /proxy/feed/..., /proxy/ajax-..., /proxy/match-event/...; the old paths
    # return the SPA's HTML shell (200 OK), which is why every feed decode
    # started failing with "string argument should contain only ASCII
    # characters" rather than a clean 404.
    #
    # Prefixing here (rather than at ~15 call sites) keeps every caller —
    # including the geo-proxy pool — on one code path.
    _API_PREFIX_RE = re.compile(
        r"^/(feed/|ajax-|api/|match-event/|match-event-history/)")

    @classmethod
    def _api_path(cls, path: str) -> str:
        """Route an internal data path through the Next.js /proxy prefix.
        Page paths ('/', '/baseball/usa/mlb/', '/dropping-odds/...') are HTML
        and must NOT be prefixed, so only the known data prefixes are rewritten.
        Already-prefixed paths pass through unchanged (idempotent)."""
        if path.startswith("/proxy/"):
            return path
        return "/proxy" + path if cls._API_PREFIX_RE.match(path) else path

    def _get(self, path: str, *, referer: Optional[str] = None,
             ajax: bool = False) -> requests.Response:
        url = path if path.startswith("http") else BASE + self._api_path(path)
        headers = {}
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer
        # (connect, read) timeout: cap connect short so a dead/unreachable proxy
        # fails fast instead of stalling a whole multi-geo sweep to the read TTL.
        timeout = (min(8, self.timeout), self.timeout)
        r = self.session.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r

    def decode_feed(self, text: str) -> Any:
        """Decode an encrypted OddsPortal feed/ajax response into JSON.

        See module docstring for the format. Raises ValueError on failure so
        the caller can tell "crypto params drifted" from an empty result.
        """
        try:
            outer = base64.b64decode(text.strip()).decode("latin1")
            ct_b64, iv_hex = outer.split(":")
            ct = base64.b64decode(ct_b64)
            iv = bytes.fromhex(iv_hex)
            dec = Cipher(algorithms.AES(self._key), modes.CBC(iv)).decryptor()
            pt = dec.update(ct) + dec.finalize()
            pt = pt[:-pt[-1]]  # strip PKCS7 padding
            if pt[:2] == b"\x1f\x8b":
                pt = gzip.decompress(pt)
            return json.loads(pt.decode("utf-8"))
        except Exception as e:
            raise ValueError(
                f"feed decode failed ({e}); the AES passphrase/salt in app-*.js "
                f"may have rotated — see OddsPortalClient module docstring"
            ) from e

    # Next.js streams its server-rendered data as a sequence of
    # `self.__next_f.push([1, "<chunk>"])` calls whose chunks concatenate into
    # one React-Flight payload. The chunks are JSON string literals, so the
    # escaping in the raw HTML varies between responses (some arrive
    # double-escaped) — always json.loads each push arg and join, never regex
    # the raw HTML, or the same field will match on one fetch and not the next.
    _NEXT_PUSH_RE = re.compile(
        r'self\.__next_f\.push\((\[.*?\])\)</script>', re.S)

    @classmethod
    def _next_payload(cls, page_html: str) -> str:
        """Concatenated React-Flight payload of a Next.js page ('' if absent)."""
        parts = []
        for m in cls._NEXT_PUSH_RE.finditer(page_html):
            try:
                arr = json.loads(m.group(1))
            except ValueError:
                continue
            if len(arr) > 1 and isinstance(arr[1], str):
                parts.append(arr[1])
        return "".join(parts)

    @staticmethod
    def _embedded_attr(page_html: str, tag: str, attr: str = ":data") -> Any:
        """Pull a JSON value out of a Vue custom-element attribute in the HTML."""
        m = re.search(r'<%s\b[^>]*\s%s="([^"]*)"' % (re.escape(tag), re.escape(attr)),
                      page_html)
        if not m:
            return None
        return json.loads(_html.unescape(m.group(1)))

    # -- sports --------------------------------------------------------------
    # Homepage top-events blocks, one per sport, in the Flight payload:
    #   "title":"Baseball","url":"/baseball/","id":6,"sport-url-name":"baseball"
    # This replaced the old <sports-menu> Vue embed, which the Next.js rewrite
    # removed along with the rest of the custom elements.
    _SPORT_RE = re.compile(
        r'"title":"([^"]+)","url":"(/[^"]*/)","id":(\d+),"sport-url-name":"([^"]+)"')

    def get_sports(self) -> List[Sport]:
        """All sports offered, parsed from the homepage's Next.js payload."""
        blob = self._next_payload(self._get("/").text)
        seen: Dict[int, Sport] = {}
        for name, url, sid, _slug in self._SPORT_RE.findall(blob):
            seen.setdefault(int(sid), Sport(id=int(sid), name=name, url=url))
        return sorted(seen.values(), key=lambda s: s.id)

    # -- leagues -------------------------------------------------------------
    def get_leagues(self, sport_id: int) -> List[League]:
        """All countries/tournaments (leagues) offered for a sport."""
        path = f"/ajax-getSportsMenuDataBySports/{sport_id},/{self.tz}/"
        r = self._get(path, ajax=True, referer=BASE + "/")
        decoded = self.decode_feed(r.text)
        out: List[League] = []
        # shape: {"s":<sportId>,"d":{<sportId>:{<countryKey>:{name,country_id,inner_sub:{...}}}}}
        sport_block = (decoded.get("d") or {}).get(str(sport_id)) \
            or (decoded.get("d") or {}).get(sport_id) or {}
        for country in sport_block.values():
            if not isinstance(country, dict):
                continue
            inner = country.get("inner_sub")
            if not isinstance(inner, dict):
                continue
            cname = country.get("name", "")
            cid = country.get("country_id")
            for t in inner.values():
                if not isinstance(t, dict):
                    continue
                out.append(League(
                    sport_id=sport_id,
                    country=cname,
                    country_id=cid,
                    name=t.get("tournament_name", ""),
                    url=t.get("tournament_url", ""),
                    tournament_id=t.get("tournament-id"),
                    count=t.get("count"),
                ))
        return out

    def get_all_leagues(self, sport_ids: Optional[List[int]] = None
                        ) -> Dict[int, List[League]]:
        """Leagues for many sports in parallel."""
        if sport_ids is None:
            sport_ids = [s.id for s in self.get_sports()]
        result: Dict[int, List[League]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futs = {ex.submit(self.get_leagues, sid): sid for sid in sport_ids}
            for f in as_completed(futs):
                sid = futs[f]
                try:
                    result[sid] = f.result()
                except Exception as e:
                    self._log(f"leagues sport {sid} failed: {e}")
                    result[sid] = []
        return result

    # -- search (historical odds) -------------------------------------------
    def search(self, query: str, pages: int = 1, start_page: int = 1,
               results: bool = True) -> List[SearchMatch]:
        """Search matches by free-text query.

        `query` is the search token as it appears in the URL. `results=True`
        hits the Results tab (/search/results/<query>/, finished matches with
        closing avg odds, newest first); `results=False` the Next Matches tab
        (/search/<query>/, upcoming fixtures with current odds). As of
        2026-07 the free-text page server-renders match rows again (the
        participant picker era is over — see search_matches for the
        both-ways-resilient entry point).
        """
        first = self._search_page(query, start_page, results=results)
        matches = first["matches"]
        page_count = first["page_count"]
        last = min(start_page + pages - 1, page_count)
        rest = list(range(start_page + 1, last + 1))
        if rest:
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futs = {ex.submit(self._search_page, query, p,
                                  results=results): p for p in rest}
                bypage = {}
                for f in as_completed(futs):
                    p = futs[f]
                    try:
                        bypage[p] = f.result()["matches"]
                    except Exception as e:
                        self._log(f"search page {p} failed: {e}")
                        bypage[p] = []
                for p in rest:
                    matches.extend(bypage.get(p, []))
        return matches

    def _search_page(self, query: str, page: int,
                     results: bool = True) -> Dict[str, Any]:
        """One page of search results.

        The Next.js rewrite dropped the <search-results-wrapper :data> embed the
        old parser read, but the archive behind it is completely intact — it now
        answers on an ENCRYPTED JSON endpoint that mirrors the page:
            /api/search/results/<query>/page/<n>/   (results tab)
            /api/search/<query>/page/<n>/           (next-matches tab)
        carrying searchData{total, onePage, page, rows[]} + pagination.pageCount.
        Depth is unchanged: "Chicago Cubs" = 1125 rows over 57 pages reaching
        back to 2020-02-22, spring training included. Rows are the same shape the
        listing uses, so _parse_search_row still applies.
        """
        base = (f"/api/search/results/{query}" if results
                else f"/api/search/{query}")
        d = self.decode_feed(
            self._get(f"{base}/page/{page}/", ajax=True).text)
        body = d.get("d") or d
        data = body.get("searchData") or {}
        page_count = ((data.get("pagination") or body.get("pagination") or {})
                      .get("pageCount"))
        if not page_count:
            total, per = data.get("total") or 0, data.get("onePage") or 20
            page_count = max(1, -(-total // per)) if total else 1
        matches = [self._parse_search_row(row) for row in data.get("rows") or []]
        return {"matches": matches, "page_count": page_count}

    # -- search (participants + their matches) ------------------------------
    # A plain free-text search() query (/search/results/<name>/) no longer
    # returns match rows — OddsPortal now renders only the PARTICIPANT picker
    # (<search-results-wrapper :search-participants>) for an ambiguous name.
    # But the PARTICIPANT-SCOPED page /search/results/:<encoded_id>/ still
    # server-renders the full match list in <search-results-wrapper :data>
    # (rows carry the event url + inline odds; :tabs defaults to "Results").
    # So the historical surface is intact — it just needs the two-hop resolve:
    # search_participants(name) -> encoded_id -> participant_matches(encoded_id).
    _TAG_RE = re.compile(r"<[^>]+>")

    def participant_matches(self, encoded_id: str, *, results: bool = True,
                            pages: int = 1, start_page: int = 1,
                            on_batch: Optional[Callable[[List["SearchMatch"]],
                                                        None]] = None
                            ) -> List[SearchMatch]:
        """Matches for one participant (team/player) by their encoded id.

        The participant-scoped search page keeps the server-rendered match rows
        that the free-text search() lost, so this is the working historical
        resolver. Each SearchMatch carries the event `url` (feed into
        get_event_odds for the full per-book breakdown) and inline `odds`.

            results=True   the "Results" tab — historical finished matches
                           (/search/results/:<id>/page/<n>/), newest first.
            results=False  the "Next Matches" tab — upcoming fixtures
                           (/search/:<id>/page/<n>/).

        Returns up to `pages` pages (20 rows/page). Page count is derived from
        the embed's total/onePage, so extra pages fetch in parallel.

        `on_batch`, if given, is called for progressive delivery: first with the
        newest page alone (fast first paint), then with the full accumulated set
        once the remaining pages land. Lets the UI show recent games in ~1 RTT
        instead of blocking on the whole season's fetch.
        """
        # Same move as _search_page: the participant-scoped embed went with the
        # Next.js rewrite, the encrypted /api/search route behind it did not.
        # It is also far cheaper — ~20KB of JSON per page instead of ~340KB of
        # HTML for the same 20 rows.
        base = (f"/api/search/results/:{encoded_id}" if results
                else f"/api/search/:{encoded_id}")

        def page(p: int) -> Dict[str, Any]:
            # Under a burst OddsPortal drops the connection (rate limit). Retry
            # once on a transient network error so a dropped page doesn't
            # SILENTLY lose 20 games (a date gap in the results).
            last_exc = None
            for attempt in range(2):
                try:
                    r = self._get(f"{base}/page/{p}/", ajax=True)
                    break
                except requests.RequestException as e:
                    last_exc = e
                    time.sleep(0.4 * (attempt + 1))
            else:
                raise last_exc
            body = self.decode_feed(r.text)
            body = body.get("d") or body
            data = body.get("searchData") or {}
            rows = data.get("rows") or []
            page_count = ((data.get("pagination") or body.get("pagination")
                           or {}).get("pageCount"))
            if not page_count:
                total = data.get("total") or 0
                per = data.get("onePage") or 20
                page_count = max(1, -(-total // per)) if total else 1
            return {"matches": [self._parse_search_row(rw) for rw in rows],
                    "page_count": page_count}

        first = page(start_page)
        matches = first["matches"]
        last = min(start_page + pages - 1, first["page_count"])
        rest = list(range(start_page + 1, last + 1))
        if on_batch and matches:
            on_batch(list(matches))  # first paint: newest page only
        if rest:
            # One capped pool over all remaining pages: it pipelines (refills as
            # each finishes) so it's faster than discrete batches, while the cap
            # keeps 340KB × N pages from tripping OddsPortal's rate limit /
            # connection drops (the cause of the earlier "hanging").
            page_workers = min(self.max_workers, SEARCH_PAGE_CONCURRENCY)
            with ThreadPoolExecutor(max_workers=page_workers) as ex:
                futs = {ex.submit(page, p): p for p in rest}
                bypage: Dict[int, List[SearchMatch]] = {}
                for f in as_completed(futs):
                    p = futs[f]
                    try:
                        bypage[p] = f.result()["matches"]
                    except Exception as e:
                        self._log(f"participant page {p} failed: {e}")
                        bypage[p] = []
                for p in rest:
                    matches.extend(bypage.get(p, []))
            if on_batch:
                on_batch(list(matches))  # full accumulated set
        return matches

    @staticmethod
    def _norm_participant(name: str) -> str:
        """Accent/punctuation-insensitive key for matching a head-to-head name
        across sources (both use the 'Surname X.' convention)."""
        if not name:
            return ""
        s = unicodedata.normalize("NFKD", str(name))
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
        return re.sub(r"\s+", " ", s).strip()

    @classmethod
    def _best_participant(cls, query: str, parts: List[Dict[str, Any]]
                          ) -> Dict[str, Any]:
        """Pick the participant that best matches `query`, instead of blindly
        taking parts[0] (which OddsPortal orders unhelpfully — e.g. "Lakers"
        returns Vaxjo hockey first, LA Lakers last). Exact normalized name wins;
        then all-query-tokens-present; then substring; then token overlap. Ties
        break toward more query coverage. Falls back to parts[0]."""
        qn = cls._norm_participant(query)
        qtok = set(qn.split())

        def score(p):
            n = cls._norm_participant(p.get("name", ""))
            if not n:
                return -1
            if n == qn:
                return 1000
            ntok = set(n.split())
            s = 0
            if qtok and qtok <= ntok:      # every typed word appears in the name
                s = 500 + 10 * len(qtok)
            elif qn and qn in n:           # query is a contiguous substring
                s = 300
            elif n in qn:
                s = 200
            s += 20 * len(qtok & ntok)     # token overlap
            return s

        return max(parts, key=score)

    def resolve_event_url(self, home: str, away: str, *,
                          sport: Optional[str] = None,
                          start_ts: Optional[int] = None,
                          pages: int = 2) -> Optional[SearchMatch]:
        """Best-effort resolve a head-to-head matchup (home vs away display
        names) to its OddsPortal match — for pinning a Flashscore game to its
        odds page. Primary route (2026-07): construct the H2H event page from
        the two auto-search participants (_resolve_via_h2h) — the old
        match-listing surfaces (search results / team events) went dark.
        Falls back to the legacy participant-matches probe in case they return.
        Returns a SearchMatch (carrying the event url) or None."""
        nh, na = self._norm_participant(home), self._norm_participant(away)
        if not nh or not na:
            return None
        # Listing FIRST, participants second. The listing is one cached page per
        # sport covering every game of the day, so it answers in ~0.3ms once
        # warm, where the participant route costs 2–4s PER MATCHUP (two
        # auto-search calls plus up to six trial h2h page fetches to find which
        # candidate pair is real). On a screen full of games — the actual usage
        # — the first row pays ~1.5s for the listing and every later row in that
        # sport is free. It also returns a url carrying the #<encodedId>
        # fragment, which lets _event_page_config skip a 246KB HTML fetch.
        #
        # Participants remain the fallback: the listing only spans today and
        # yesterday, and a miss costs nothing once the page is cached.
        for step in (self._resolve_via_listing, self._resolve_via_h2h):
            m = step(home, away, nh=nh, na=na, sport=sport, start_ts=start_ts)
            if m is not None:
                return m
        return self._resolve_via_search(home, away, nh=nh, na=na, sport=sport,
                                        start_ts=start_ts, pages=pages)

    # Rows of /matches/<sport>/, embedded in the page's Flight payload as plain
    # JSON objects. Each carries the event url (with its #<encodedId> fragment),
    # both display names and the start time.
    _LISTING_ROW_RE = re.compile(r'\{"id":\d+,"is-double":')

    @staticmethod
    def _json_object_at(blob: str, start: int) -> Optional[Dict[str, Any]]:
        """Extract the brace-balanced JSON object beginning at `start`."""
        depth, i, in_str, esc = 0, start, False, False
        while i < len(blob):
            ch = blob[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(blob[start:i + 1])
                    except ValueError:
                        return None
            i += 1
        return None

    def _listing_rows(self, sport: str) -> List[Dict[str, Any]]:
        """Today's/upcoming rows for a sport, from /matches/<sport>/ (cached)."""
        hit = self._listing_cache.get(sport)
        if hit and time.time() - hit[0] < (self._listing_ttl if hit[1]
                                           else self._listing_empty_ttl):
            return hit[1]
        blob = self._next_payload(self._get(f"/matches/{sport}/").text)
        rows = []
        for m in self._LISTING_ROW_RE.finditer(blob):
            obj = self._json_object_at(blob, m.start())
            if obj and obj.get("url"):
                rows.append(obj)
        self._listing_cache[sport] = (time.time(), rows)
        return rows

    def _resolve_via_listing(self, home: str = "", away: str = "", *,
                             nh: str = "", na: str = "",
                             sport: Optional[str] = None,
                             start_ts: Optional[int] = None
                             ) -> Optional[SearchMatch]:
        """Resolve off the sport's match listing.

        The participant route (_resolve_via_h2h) can only see what auto-search
        indexes, and auto-search indexes SINGLES PLAYERS ONLY — so every tennis
        doubles match ("Luz O./Matos R." vs "Arevalo M./Pavic M.") was
        unresolvable, even though OddsPortal carries it. The listing does have
        those rows, keyed by the same 'Surname X.' names Flashscore uses, so one
        cached page fetch covers doubles and anything else auto-search misses.

        The row's url keeps its #<encodedId> fragment, which pins the exact
        meeting — important for same-day rematches, where the bare h2h page
        resolves to the most recent PLAYED game instead."""
        if not sport:
            return None
        try:
            rows = self._listing_rows(sport)
        except Exception as e:
            self._log(f"listing resolve failed for {sport}: {e}")
            return None
        best = None
        for r in rows:
            names = {self._norm_participant(r.get("home-name", "")),
                     self._norm_participant(r.get("away-name", ""))}
            if names != {nh, na}:
                continue
            ts = r.get("date-start-timestamp")
            if start_ts and ts and abs(ts - start_ts) > 14 * 86400:
                continue
            # several meetings can match a pair — keep the closest to start_ts
            if best is None or (start_ts and ts and best[1]
                                and abs(ts - start_ts) < abs(best[1] - start_ts)):
                best = (r, ts)
        if best is None:
            return None
        r, ts = best
        crumbs = r.get("breadcrumbs") or {}
        return SearchMatch(
            id=r.get("id") or 0, sport_id=r.get("sport-id") or 0,
            sport=sport,
            home=r.get("home-name", ""), away=r.get("away-name", ""),
            tournament=(crumbs.get("tournament") or {}).get("name", ""),
            tournament_url=(crumbs.get("tournament") or {}).get("url", ""),
            country=r.get("country-name", ""), start_ts=ts,
            status=r.get("event-stage-name", ""), result=r.get("result", ""),
            url=r.get("url", ""), bookmaker_count=None)

    def _resolve_via_h2h(self, home: str, away: str, *,
                         nh: Optional[str] = None, na: Optional[str] = None,
                         sport: Optional[str] = None,
                         start_ts: Optional[int] = None
                         ) -> Optional[SearchMatch]:
        """Resolve via the H2H page: /<sport>/h2h/<slug>-<id>/<slug>-<id>/ IS a
        full event page for the two participants' next meeting (eventData +
        match-event feed), so no match listing is needed. Participants come
        from the auto-search feed; candidate pairs sharing an upcoming event id
        are tried first. The page-config fetch doubles as validation and is
        cached, so the follow-up get_event_odds pays nothing extra."""
        try:
            ph = self.search_participants(home)
            pa = self.search_participants(away)
        except Exception as e:
            self._log(f"h2h resolve participants failed: {e}")
            return None

        def cands(parts):
            ps = [p for p in parts if p.get("slug") and p.get("encoded_id")]
            if sport:
                sp = [p for p in ps if p.get("sport") == sport]
                ps = sp or ps
            return ps[:4]

        pairs = []
        for hp in cands(ph):
            for ap in cands(pa):
                if hp["sport"] != ap["sport"] \
                        or hp["encoded_id"] == ap["encoded_id"]:
                    continue
                shared = set(hp["event_ids"]) & set(ap["event_ids"])
                pairs.append((0 if shared else 1, hp, ap))
        pairs.sort(key=lambda x: x[0])

        for _rank, hp, ap in pairs[:3]:
            for a, b in ((hp, ap), (ap, hp)):  # h2h url order may matter
                url = (f"/{a['sport']}/h2h/{a['slug']}-{a['encoded_id']}"
                       f"/{b['slug']}-{b['encoded_id']}/")
                try:
                    cfg = self._event_page_config(url)
                except Exception:
                    continue
                if start_ts and cfg.get("start_ts") \
                        and abs(cfg["start_ts"] - start_ts) > 14 * 86400:
                    continue  # page shows a different (old/far-off) meeting
                return SearchMatch(
                    id=0, sport_id=a.get("sport_id") or 0, sport=a["sport"],
                    home=cfg.get("home") or home, away=cfg.get("away") or away,
                    tournament=cfg.get("tournament") or "", tournament_url="",
                    country="", start_ts=cfg.get("start_ts"), status="",
                    result="", url=url, bookmaker_count=None)
        return None

    def _resolve_via_search(self, home: str, away: str, *, nh: str, na: str,
                            sport: Optional[str] = None,
                            start_ts: Optional[int] = None,
                            pages: int = 2) -> Optional[SearchMatch]:
        """Legacy resolve via the participant match listings (dead 2026-07 —
        kept for when OddsPortal restores the surface)."""

        # Pin the matchup via EITHER participant: their opponent lists overlap
        # on the shared match, but recency/pagination can hide it from one
        # side. Both probes run concurrently (each probe itself fetches its
        # upcoming + results tabs concurrently) and the first hit wins — the
        # sequential version of this chain cost 15-20s per click.
        def _probe(query: str) -> List[SearchMatch]:
            parts = self.search_participants(query)
            if sport:
                parts = [p for p in parts if p.get("sport") == sport] or parts
            cands: List[SearchMatch] = []
            with ThreadPoolExecutor(max_workers=2) as tabs:
                if parts:
                    eid = parts[0]["encoded_id"]
                    futs = [tabs.submit(self.participant_matches, eid,
                                        results=r, pages=pages)
                            for r in (False, True)]
                else:
                    # picker not rendered — free-text rows (see search_matches)
                    futs = [tabs.submit(self.search, query, pages, 1, r)
                            for r in (False, True)]
                for f in futs:  # keep upcoming-first ordering
                    try:
                        cands.extend(f.result())
                    except Exception as e:
                        self._log(f"resolve_event_url {query} page fetch failed: {e}")
            return [m for m in cands
                    if {self._norm_participant(m.home),
                        self._norm_participant(m.away)} == {nh, na}]

        ex = ThreadPoolExecutor(max_workers=2)
        try:
            futs = [ex.submit(_probe, q) for q in (home, away)]
            for f in futs:  # home-side answer preferred; both already running
                try:
                    hits = f.result()
                except Exception as e:
                    self._log(f"resolve_event_url probe failed: {e}")
                    continue
                if hits:
                    if start_ts:
                        hits.sort(key=lambda m: abs((m.start_ts or 0) - start_ts))
                    return hits[0]
            return None
        finally:
            ex.shutdown(wait=False)

    def search_matches(self, query: str, *, results: bool = True,
                       pages: int = 1, sport: Optional[str] = None,
                       match_name: Optional[str] = None,
                       on_batch: Optional[Callable[[List["SearchMatch"]],
                                                   None]] = None
                       ) -> List[SearchMatch]:
        """Free-text -> a participant's matches (with odds), resilient to
        OddsPortal's flip-flopping search markup: try the participant hop
        (picker -> encoded id -> participant-scoped rows), and when the picker
        isn't rendered (the 2026-07 state — free-text pages server-render the
        match rows directly again) fall back to plain free-text search.

        `sport` (an OddsPortal sport url-name, e.g. "tennis") disambiguates
        same-named participants across sports. `match_name` (e.g. the full team
        name behind a short query) is used ONLY to score which participant to
        pick — the auto-search still runs on the short `query`, since a full
        name like "Los Angeles Lakers" hurts recall (returns other LA teams,
        not the Lakers) while "Lakers" returns Lakers-named teams the full name
        then disambiguates. `on_batch` enables progressive delivery. [] if none.
        """
        parts = self.search_participants(query)
        if sport:
            parts = [p for p in parts if p.get("sport") == sport] or parts
        if parts:
            best = self._best_participant(match_name or query, parts)
            return self.participant_matches(
                best["encoded_id"], results=results, pages=pages,
                on_batch=on_batch)
        rows = self.search(query, pages=pages, results=results)
        if sport:
            rows = [m for m in rows if m.sport == sport]
        if on_batch and rows:
            on_batch(list(rows))
        return rows

    @classmethod
    def _clean_name(cls, s: str) -> str:
        """Strip the <strong> match-highlight markup and &nbsp; from a name."""
        return _html.unescape(cls._TAG_RE.sub("", s or "")).replace("\xa0", " ").strip()

    def search_participants(self, query: str) -> List[Dict[str, Any]]:
        """Resolve a free-text query to OddsPortal participants (players/teams).

        Primary source (2026-07): the /ajax-auto-search/ encrypted feed — the
        endpoint the site's own search box uses. The old server-rendered
        :search-participants embed now returns [] in every geo, so it's kept
        only as a fallback for when OddsPortal flip-flops back. Returns dicts
        with: id, encoded_id, slug, url, name, sport_id, sport, event_ids, ...
        Cached per query (~10 min); participant ids never move.
        """
        hit = self._search_cache.get(query)
        if hit and time.time() - hit[0] < self._search_ttl:
            return hit[1]
        try:
            out = self._auto_search(query)
        except Exception as e:
            self._log(f"auto-search failed for {query!r}: {e}")
            out = []
        if not out:
            out = self._search_participants_embed(query)
        self._search_cache[query] = (time.time(), out)
        return out

    def _auto_search(self, query: str) -> List[Dict[str, Any]]:
        """The search box's autocomplete feed: encrypted, geo-independent, and
        the only search surface OddsPortal still serves (2026-07). Each
        participant carries its page url (/…/team/<slug>/<encoded_id>/) and the
        ids of its upcoming events with odds.

        The query is a PATH segment, and a trailing '.' makes the server match
        nothing at all — it reads as a file extension on ".../<query>/". That
        silently broke every abbreviated name Flashscore hands us ("Vukic A.",
        "Mejia N."), which is most of tennis: 'Vukic A.' returned 0 rows where
        'Vukic A' returns the right player first. Strip trailing dots/space;
        OddsPortal's own participant names keep the period ("Vukic A."), so this
        only affects the lookup, never the display or match text.
        """
        q = str(query).rstrip(". \t")
        r = self._get(f"/ajax-auto-search/{quote(q)}/", ajax=True,
                      referer=BASE + "/")
        d = self.decode_feed(r.text)
        parts = ((d.get("d") or {}).get("result") or {}).get("participants") or []
        out: List[Dict[str, Any]] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            url = p.get("url") or ""
            segs = [s for s in url.split("/") if s]
            # team pages: /football/team/<slug>/<id>/. Participants without a
            # team page (esports) get /search/:<id>/ — the h2h URL's slug part
            # is arbitrary (the server keys off the encoded id), so synthesize
            # one from the name.
            enc = segs[-1].lstrip(":") if segs else None
            slug = segs[-2] if len(segs) >= 2 else None
            if not slug or slug == "search":
                slug = re.sub(r"[^a-z0-9]+", "-",
                              (p.get("name") or "").lower()).strip("-") or "team"
            out.append({
                "id": p.get("id"),
                "encoded_id": enc,
                "slug": slug,
                "url": url,
                "name": self._clean_name(p.get("name", "")),
                "sport_id": p.get("sport_id"),
                "sport": p.get("sport_name", ""),
                "country": "",
                "country_code": "",
                "event_ids": [str(e) for e in (p.get("events") or [])],
                "search_url": "",
                "score": p.get("score"),
                "has_odds": p.get("hasFutureEventsWithOdds"),
            })
        out.sort(key=lambda d: d.get("score") or 0, reverse=True)
        return out

    def _search_participants_embed(self, query: str) -> List[Dict[str, Any]]:
        """Legacy: the server-rendered :search-participants embed (empty as of
        2026-07, kept as a fallback against markup flip-flops)."""
        r = self._get(f"/search/results/{query}/page/1/")
        raw = self._embedded_attr(r.text, "search-results-wrapper",
                                  attr=":search-participants") or {}
        if not isinstance(raw, dict):
            return []
        out: List[Dict[str, Any]] = []
        for p in raw.values():
            if not isinstance(p, dict):
                continue
            out.append({
                "id": p.get("id"),
                "encoded_id": p.get("encoded-id"),
                "slug": None,
                "url": "",
                "name": self._clean_name(p.get("name", "")),
                "sport_id": p.get("sport-id"),
                "sport": p.get("sport-url-name", ""),
                "country": p.get("country-name", ""),
                "country_code": p.get("country-two-chart-name", ""),
                "event_ids": p.get("events", []) or [],
                "search_url": p.get("search-url", ""),
                "score": p.get("score"),
            })
        out.sort(key=lambda d: d.get("score") or 0, reverse=True)
        return out

    # -- per-event odds (the historical-odds surface) -----------------------
    # The match-odds feed lives at
    #   /match-event/<versionId>-<sportId>-<encodedId>-<betType>-<scope>-<xhashf>.dat
    # (note: NO /feed/ segment, unlike live-event/postmatch-score — though both
    # now sit behind the /proxy prefix, see _api_path). The token parts come
    # from the event-data-shell endpoint, so no browser is needed.
    # Decoded payload (decode_feed) is
    #   {s, d:{bt, sc, oddsdata:{back:{<key>:{outcomeId, odds:{bookieId:[...]},
    #          openingOdd, movement, bs:{bookieId:[betslip-url,...]}, ...}}}}}
    # Bookmaker NAMES aren't mapped inline — we read them out of the betslip URL
    # path (/bookmakers/<name>/betslip/...), so no extra request is needed.
    _BOOKIE_RE = re.compile(r"/bookmakers/([^/]+)/")

    # provider id -> web name, learned from betslip urls as feeds are parsed
    # and persisted to disk. Closed markets (pregame odds of an in-play game,
    # historical events) drop their betslip links, so this learned map is the
    # only way to label their books. Class-level: shared by every client
    # (incl. the geo pool) — any open market anywhere teaches all of them.
    _BOOKIE_NAMES: Dict[str, str] = {}
    _BOOKIE_NAMES_LOCK = threading.Lock()
    _BOOKIE_NAMES_PATH = os.path.join(
        os.path.expanduser("~"), ".cache", "effortodds", "bookies.json")
    _bookie_names_loaded = False

    @classmethod
    def _load_bookie_names(cls):
        if cls._bookie_names_loaded:
            return
        with cls._BOOKIE_NAMES_LOCK:
            if cls._bookie_names_loaded:
                return
            try:
                with open(cls._BOOKIE_NAMES_PATH) as fh:
                    cls._BOOKIE_NAMES.update(json.load(fh))
            except Exception:
                pass
            cls._bookie_names_loaded = True

    @classmethod
    def _learn_bookie_names(cls, names: Dict[str, str]):
        new = {bid: n for bid, n in names.items()
               if not n.isdigit() and cls._BOOKIE_NAMES.get(bid) != n}
        if not new:
            return
        with cls._BOOKIE_NAMES_LOCK:
            cls._BOOKIE_NAMES.update(new)
            try:
                os.makedirs(os.path.dirname(cls._BOOKIE_NAMES_PATH),
                            exist_ok=True)
                tmp = cls._BOOKIE_NAMES_PATH + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(cls._BOOKIE_NAMES, fh)
                os.replace(tmp, cls._BOOKIE_NAMES_PATH)  # atomic
            except Exception:
                pass

    # The Next.js event page no longer inlines `requestPreMatch` / `requestLive`
    # (those lived in the old `var pageVar`). The same values now come from an
    # encrypted shell endpoint keyed by the event's encoded id:
    #   /ajax-event-data-shell/<encodedId>/0
    #     -> d.eventData: {id, xhash, xhashf, versionId, sportId, home, away,
    #                      defaultBetId, defaultScopeId, isLive, isStarted, ...}
    #        d.eventBody: {startDate, ...}
    # from which the feed path is assembled (see _feed_token). xhash/xhashf are
    # percent-encoded in the payload ("%79%6a%63%37%31" -> "yjc71").
    #
    # A `#<encodedId>` fragment on a h2h deep link IS that encoded id, so a
    # deep-linked past meeting is fetched straight from the shell with no HTML
    # request at all — the fragment used to require rewriting a token parsed out
    # of ~250KB of markup. Without a fragment we pay one page fetch to read
    # `defaultEventHash":"#<encodedId>:...` and learn which meeting is current.
    _EVENT_HASH_RE = re.compile(r'"defaultEventHash":"#([A-Za-z0-9]+)')

    @staticmethod
    def _feed_token(ed: Dict[str, Any], bet: int, scope: int, xhash_key: str) -> str:
        """<versionId>-<sportId>-<encodedId>-<betType>-<scope>-<xhash>"""
        return (f"{ed.get('versionId', 1)}-{ed.get('sportId', 0)}-{ed.get('id')}"
                f"-{bet}-{scope}-{unquote(str(ed.get(xhash_key) or ''))}")

    def _event_encoded_id(self, base_url: str) -> str:
        """The encoded id of the meeting an event page currently shows."""
        txt = self._get(base_url).text
        m = self._EVENT_HASH_RE.search(txt) \
            or self._EVENT_HASH_RE.search(self._next_payload(txt))
        if not m:
            raise ValueError(
                f"no defaultEventHash on {base_url}; OddsPortal markup may have "
                f"drifted again — re-derive from the page's Next.js payload")
        return m.group(1)

    def _event_page_config(self, event_url: str) -> Dict[str, Any]:
        """Pull what we need to build the odds feed + label it: the match-event
        feed path and the home/away/sport/tournament. Cached per URL (the
        event's xhashf/version are stable) to skip re-fetching on repeat clicks.

        A `#<encodedId>` fragment on `event_url` (h2h deep link from search)
        selects which meeting's odds we fetch."""
        hit = self._event_cfg_cache.get(event_url)
        if hit and time.time() - hit[0] < self._event_cfg_ttl:
            return hit[1]
        base_url, _, frag = event_url.partition("#")
        # a deep-link fragment may carry a market suffix ("#<id>:home-away;1")
        enc = frag.split(":")[0] if frag else self._event_encoded_id(base_url)

        shell = self.decode_feed(
            self._get(f"/ajax-event-data-shell/{enc}/0",
                      ajax=True, referer=BASE + base_url).text)
        d = shell.get("d") or {}
        ed = d.get("eventData") or {}
        body = d.get("eventBody") or {}
        if not ed.get("id"):
            raise ValueError(f"event-data-shell carried no eventData for {enc}")

        bet = int(d.get("defaultBettingType") or ed.get("defaultBetId") or 0)
        scope = int(d.get("defaultScope") or ed.get("defaultScopeId") or 0)
        feed_path = f"/match-event/{self._feed_token(ed, bet, scope, 'xhashf')}.dat"

        # In-play events serve their prices on the live feed (the prematch feed
        # goes empty at kickoff); it shares the token shape but is salted with
        # `xhash` rather than `xhashf`. A deep-linked PAST meeting is never live.
        live_path = None
        if not frag and (ed.get("isLive") or d.get("hasLiveOdds")
                         or (ed.get("isStarted") and not ed.get("isFinished"))):
            live_path = (f"/feed/live-event/"
                         f"{self._feed_token(ed, bet, scope, 'xhash')}.dat")

        cfg = {
            "feed_path": feed_path,
            "live_feed_path": live_path,
            "home": ed.get("home") or "", "away": ed.get("away") or "",
            "sport": ed.get("sportUrl") or "",
            "tournament": ed.get("tournamentName") or "",
            "betting_type_id": bet,
            "scope_id": scope,
            "start_ts": body.get("startDate"),
            # kept so other markets can be tokenised without re-fetching the
            # shell — the xhashf salt is market-independent.
            "event_data": ed,
        }
        self._event_cfg_cache[event_url] = (time.time(), cfg)
        return cfg

    @staticmethod
    def _outcome_labels(n: int, home: str, away: str,
                        bet: Optional[int] = None) -> List[str]:
        """Map outcome columns to readable labels.

        Keyed on the MARKET first, then the column count. Labelling by count
        alone is what would silently turn an Over/Under pair into
        "Reds / Marlins" — the shape is identical, only the meaning differs.
        """
        fixed = _MARKET_LABELS.get(bet or -1)
        if fixed and len(fixed) == n:
            return list(fixed)
        if n == 2:
            return [home or "1", away or "2"]
        if n == 3:
            return [home or "1", "Draw", away or "2"]
        return [str(i + 1) for i in range(n)]

    def _fetch_back(self, feed_path: str, event_url: str):
        """GET + decode one odds feed; returns (d, oddsdata.back blocks)."""
        ts = int(time.time() * 1000)
        if "?_=" in feed_path:
            path = feed_path.replace("?_=", f"?_={ts}", 1)
        else:
            path = feed_path + ("&" if "?" in feed_path else "?") + f"_={ts}"
        decoded = self.decode_feed(
            self._get(path, ajax=True, referer=BASE + event_url).text)
        d = decoded.get("d") or {}
        return d, (d.get("oddsdata") or {}).get("back") or {}

    def _back_to_outcomes(self, back: Dict[str, Any], cfg: Dict[str, Any]):
        """The DEFAULT market's outcomes — first block only.

        Kept exactly as it was so every existing consumer (live_scores_widget,
        TennisWindow) is untouched. `_back_to_lines` is the general form.
        """
        lines, logos = self._back_to_lines(back, cfg, cfg.get("betting_type_id"))
        return (lines[0].outcomes if lines else []), logos

    def _back_to_lines(self, back: Dict[str, Any], cfg: Dict[str, Any],
                       bet: Optional[int] = None) -> tuple:
        """Parse EVERY back block into one MarketLine per handicap.

        The old code ended this loop with `break  # default market only`, which
        is why the client could never see totals: a totals feed returns one
        block PER LINE (23 of them on a baseball game, 2.5 through 13.5) and
        only the first was ever read. Nothing else about the request had to
        change — the same event `xhashf` salts every market.
        """
        lines: List[MarketLine] = []
        book_logos: Dict[str, str] = {}
        for key, block in back.items():
            odds = block.get("odds") or {}
            opening = block.get("openingOdd") or {}
            bs = block.get("bs") or {}
            # bookie id -> readable name (from betslip url), and width of the row.
            # The bid is OddsPortal's idProvider, so the logo is servable at
            # /serve/bookmaker/<bid>/ (a small png) — keyed here by book name.
            self._load_bookie_names()
            names, width = {}, 0
            for bid, links in bs.items():
                first = (links or [None])[0] or ""
                mm = self._BOOKIE_RE.search(first)
                names[bid] = (mm.group(1) if mm
                              else self._BOOKIE_NAMES.get(bid, bid))
                book_logos[names[bid]] = f"{BASE}/serve/bookmaker/{bid}/"
            self._learn_bookie_names(names)
            for bid in odds:  # books priced without a betslip link in bs
                if bid not in names:  # closed market: use the learned map
                    names[bid] = self._BOOKIE_NAMES.get(bid, bid)
                    book_logos[names[bid]] = f"{BASE}/serve/bookmaker/{bid}/"
            # per-book odds come as a list [o0, o1, ...] OR a dict keyed by
            # stringified index {"0": o0, ...} depending on the event — normalize.
            def _seq(x):
                if isinstance(x, list):
                    return x
                if isinstance(x, dict):
                    return [x.get(str(i)) for i in range(len(x))]
                return []

            def _at(x, i):
                """Element i of a per-book field that may be a list OR a
                dict keyed by stringified index."""
                if isinstance(x, dict):
                    return x.get(str(i))
                if isinstance(x, list):
                    return x[i] if i < len(x) else None
                return None
            odds = {bid: _seq(v) for bid, v in odds.items()}
            opening = {bid: _seq(v) for bid, v in opening.items()}
            for arr in odds.values():
                width = max(width, len(arr))
            labels = self._outcome_labels(
                width, cfg["home"], cfg["away"],
                block.get("bettingTypeId") if block.get("bettingTypeId")
                is not None else bet)
            # transpose: build one OutcomeBook per outcome column
            cols: List[OutcomeBook] = [
                OutcomeBook(name=labels[i], avg_odds=None, max_odds=None,
                            max_book=None, opening_avg=None, n_books=0)
                for i in range(width)]
            ct = block.get("changeTime") or {}
            oct_ = block.get("openingChangeTime") or {}
            mv = block.get("movement") or {}
            for bid, arr in odds.items():
                bname = names.get(bid, bid)
                for i, val in enumerate(arr):
                    if i >= width or not val:
                        continue
                    cols[i].books[bname] = val
                    op = opening.get(bid) or []
                    if i < len(op) and op[i]:
                        cols[i].opening[bname] = op[i]
                    # {bookId: {columnIndex: value}} on most events, but
                    # {bookId: [v0, v1, ...]} on some — the same list-vs-dict
                    # inconsistency the odds arrays already have. `_at` takes
                    # either; indexing one shape blindly raises on the other.
                    for src, dest in ((ct, cols[i].changed_at),
                                      (oct_, cols[i].opened_at)):
                        v = _at(src.get(bid), i)
                        if isinstance(v, (int, float)) and v:
                            dest[bname] = int(v)
                    d = _at(mv.get(bid), i)
                    if d:
                        cols[i].movement[bname] = str(d)
            for c in cols:
                cur = list(c.books.values())
                op = list(c.opening.values())
                c.n_books = len(cur)
                if cur:
                    c.avg_odds = round(sum(cur) / len(cur), 3)
                    c.max_book, c.max_odds = max(c.books.items(), key=lambda kv: kv[1])
                if op:
                    c.opening_avg = round(sum(op) / len(op), 3)
            hv = block.get("handicapValue")
            try:
                handicap = float(hv) if hv not in (None, "") else None
            except (TypeError, ValueError):
                handicap = None
            bid_block = block.get("bettingTypeId")
            bt = int(bid_block if bid_block is not None else (bet or 0))
            if bt not in LINE_MARKETS:
                handicap = None          # a single-block market has no line
            sc = int(block.get("scopeId") or cfg.get("scope_id") or 1)
            lines.append(MarketLine(
                betting_type_id=bt, market=BETTING_TYPES.get(bt, str(bt)),
                handicap=handicap, outcomes=cols, scope_id=sc,
                scope=scope_name(cfg.get("sport", ""), sc,
                                 cfg.get("scope_id"))))
        lines.sort(key=lambda m: (m.handicap is None, m.handicap))
        return lines, book_logos

    # Markets worth pulling for a US-style two-way sport. Soccer would want
    # 1 (1X2) and 13 (BTTS) instead of 3.
    DEFAULT_MARKETS = (3, 2, 5)          # moneyline, totals, run line

    # Scopes worth sweeping. 1 is whole-game everywhere; 2/3 are the period
    # markets (first five innings, halves) that were invisible before.
    # 1-4 covers whole-game plus periods on every sport checked; the event's
    # own default scope is always added, since it is the only id guaranteed
    # to be priced (soccer never answers scope 1).
    DEFAULT_SCOPES = (1, 2, 3, 4)

    def event_markets(self, event_url: str, markets: Sequence[int] = (),
                      *, scope: Optional[int] = None,
                      scopes: Sequence[int] = (),
                      cfg: Optional[Dict[str, Any]] = None
                      ) -> List[MarketLine]:
        """Every priced line across the requested betting types.

        `markets` are betting-type ids (see `BETTING_TYPES`); defaults to
        `DEFAULT_MARKETS`. Each is one small encrypted feed and they are
        independent, so they are fetched concurrently — the whole set costs
        about as much as the single moneyline fetch did.

        The event's `xhashf` is market-independent, so no extra handshake is
        needed: only the `bet` segment of the feed token changes.
        """
        cfg = cfg or self._event_page_config(event_url)
        markets = tuple(markets) or self.DEFAULT_MARKETS
        if scope is not None:
            scope_list = (scope,)
        else:
            scope_list = tuple(scopes) or self.DEFAULT_SCOPES
            dsc = cfg.get("scope_id")
            if dsc is not None and dsc not in scope_list:
                scope_list = (dsc,) + tuple(scope_list)
        ed = cfg.get("event_data") or {}
        if not ed:
            return []

        def one(job: tuple) -> List[MarketLine]:
            bet, sc = job
            token = self._feed_token(ed, bet, sc, "xhashf")
            try:
                _, back = self._fetch_back(f"/match-event/{token}.dat",
                                           event_url.partition("#")[0])
                lines, logos = self._back_to_lines(back, cfg, bet)
                # A market the book does not offer answers with an empty feed
                # rather than an error, so drop lines nobody priced instead of
                # emitting hollow rows.
                return [ln for ln in lines if ln.n_books]
            except Exception as e:
                self._log(f"market {bet} failed for {event_url}: {e}")
                return []

        jobs = [(b, sc) for b in markets for sc in scope_list]
        out: List[MarketLine] = []
        with ThreadPoolExecutor(max_workers=min(len(jobs), 8)) as ex:
            for res in ex.map(one, jobs):
                out.extend(res)
        # A sport with no period markets answers scope 2/3 with the SAME blocks
        # as scope 1 rather than an error, so dedupe on the identity of a line.
        seen, uniq = set(), []
        for ln in out:
            key = (ln.betting_type_id, ln.scope_id, ln.handicap)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(ln)
        return uniq

    def get_event_odds(self, event_url: str, *,
                       markets: Sequence[int] = (),
                       cfg: Optional[Dict[str, Any]] = None,
                       _fresh: bool = False) -> EventOdds:
        """Full per-bookmaker odds (current + opening) for an event's default
        market — restores the historical-odds surface. `event_url` is a site
        path like '/tennis/h2h/<slug>/<slug>/' (e.g. DroppingOdd.event_url).
        Result is briefly cached to make repeat clicks instant.

        In-play events serve LIVE odds in `outcomes` (the prematch feed goes
        empty at first pitch) with the frozen pregame closing odds alongside in
        `pre_outcomes` — both feeds are fetched concurrently. Prematch/finished
        events serve the prematch feed alone.

        `cfg`: a precomputed _event_page_config dict. The event page HTML is
        mostly geo-independent, so multi-geo fetches pass one shared cfg and
        each geo only hits the small encrypted feed — but a geo whose own page
        would disagree (e.g. a limited geo emitting the wrong default betting
        type) can pass `_fresh=True` to bypass the odds cache and re-derive its
        own cfg."""
        ckey = (event_url, tuple(markets))
        if not _fresh:
            hit = self._event_odds_cache.get(ckey)
            if hit and time.time() - hit[0] < self._event_odds_ttl:
                return hit[1]
        cfg = cfg or self._event_page_config(event_url)

        outcomes, pre_outcomes, book_logos, d, live = [], [], {}, {}, False
        if cfg.get("live_feed_path"):
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_live = ex.submit(self._fetch_back,
                                   cfg["live_feed_path"], event_url)
                f_pre = ex.submit(self._fetch_back, cfg["feed_path"], event_url)
                try:
                    d, back = f_live.result()
                    outcomes, book_logos = self._back_to_outcomes(back, cfg)
                    live = any(c.books for c in outcomes)
                except Exception as e:
                    self._log(f"live feed failed for {event_url}: {e}")
                try:
                    d_pre, back_pre = f_pre.result()
                    pre, pre_logos = self._back_to_outcomes(back_pre, cfg)
                    if live:
                        pre_outcomes = pre
                        book_logos.update(pre_logos)
                    else:  # live feed empty/failed: prematch is THE odds
                        d, outcomes, book_logos = d_pre, pre, pre_logos
                except Exception as e:
                    if not live:
                        raise
                    self._log(f"prematch feed failed for {event_url}: {e}")
        else:
            d, back = self._fetch_back(cfg["feed_path"], event_url)
            outcomes, book_logos = self._back_to_outcomes(back, cfg)

        extra: List[MarketLine] = []
        if markets:
            extra = self.event_markets(event_url, markets, cfg=cfg)
            for ln in extra:
                for o in ln.outcomes:
                    for bname in o.books:
                        book_logos.setdefault(bname, book_logos.get(bname, ""))

        eo = EventOdds(
            event_url=event_url, sport=cfg["sport"], home=cfg["home"],
            away=cfg["away"], tournament=cfg["tournament"],
            start_ts=cfg["start_ts"], betting_type_id=d.get("bt") or 0,
            scope_id=d.get("sc") or 0, outcomes=outcomes,
            book_logos=book_logos, live=live, pre_outcomes=pre_outcomes,
            markets=extra)
        self._event_odds_cache[ckey] = (time.time(), eo)
        return eo

    @staticmethod
    def _merge_event_odds(results: List["EventOdds"]) -> Optional["EventOdds"]:
        """Merge per-geo EventOdds into one, unioning the bookmaker sets per
        outcome and recomputing avg/max/opening. OddsPortal returns a different
        book set per egress geo, so the union is strictly richer than any one."""
        results = [r for r in results if r and r.outcomes]
        if not results:
            return None
        # deep copy: results[0] lives in a geo client's odds cache (and may
        # already be rendering in a UI from a staged first delivery) — merging
        # in place would mutate it from another thread
        def _union(mine: List["OutcomeBook"], theirs: List["OutcomeBook"]):
            if not mine and theirs:
                mine.extend(copy.deepcopy(theirs))
                return
            for i, oc in enumerate(theirs):
                if i >= len(mine):
                    break
                mine[i].books.update(oc.books)
                mine[i].opening.update(oc.opening)
                mine[i].opened_at.update(oc.opened_at)
                mine[i].changed_at.update(oc.changed_at)
                mine[i].movement.update(oc.movement)

        # Geos can disagree on liveness (one live feed priced, another empty →
        # that geo fell back to prematch). NEVER mix the two into one table:
        # live results merge into `outcomes`, everything pregame (live results'
        # pre_outcomes + non-live results' outcomes) into `pre_outcomes`.
        live_rs = [r for r in results if r.live]
        if live_rs:
            base = copy.deepcopy(live_rs[0])
            for other in live_rs[1:]:
                _union(base.outcomes, other.outcomes)
            for other in live_rs[1:]:
                _union(base.pre_outcomes, other.pre_outcomes)
            for other in results:
                if not other.live:
                    _union(base.pre_outcomes, other.outcomes)
        else:
            base = copy.deepcopy(results[0])
            for other in results[1:]:
                _union(base.outcomes, other.outcomes)
        for other in results:
            base.book_logos.update(other.book_logos)  # union logo map too

        # Union the extra markets too. Keyed on (betting type, handicap) and
        # NOT positional: a geo prices whichever lines its books offer, so one
        # exit may carry totals 7.5-9.5 and another 6.5-11. Merging these by
        # index would silently staple the odds for 6.5 onto the 7.5 row.
        if any(r.markets for r in results):
            by_key: Dict[tuple, "MarketLine"] = {}
            for r in results:
                for ln in r.markets:
                    key = (ln.betting_type_id, ln.scope_id, ln.handicap)
                    have = by_key.get(key)
                    if have is None:
                        by_key[key] = copy.deepcopy(ln)
                        continue
                    for i, oc in enumerate(ln.outcomes):
                        if i >= len(have.outcomes):
                            break
                        have.outcomes[i].books.update(oc.books)
                        have.outcomes[i].opening.update(oc.opening)
                        have.outcomes[i].opened_at.update(oc.opened_at)
                        have.outcomes[i].changed_at.update(oc.changed_at)
                        have.outcomes[i].movement.update(oc.movement)
            base.markets = sorted(
                by_key.values(),
                key=lambda m: (m.betting_type_id, m.scope_id,
                               m.handicap is None, m.handicap))

        merged_cols = base.outcomes + base.pre_outcomes + [
            o for ln in base.markets for o in ln.outcomes]
        for c in merged_cols:  # recompute over the union
            cur = list(c.books.values())
            op = list(c.opening.values())
            c.n_books = len(cur)
            c.avg_odds = round(sum(cur) / len(cur), 3) if cur else None
            if cur:
                c.max_book, c.max_odds = max(c.books.items(), key=lambda kv: kv[1])
            c.opening_avg = round(sum(op) / len(op), 3) if op else None
        return base

    # persistent per-proxy client pool for multi-geo fetches: keeps TLS
    # sessions and the per-client cfg/odds caches alive across calls instead of
    # paying a fresh handshake + event-page download per geo per click.
    _geo_pool: Dict[Optional[str], "OddsPortalClient"] = {}
    _geo_pool_lock = threading.Lock()

    @classmethod
    def _geo_client(cls, proxy: Optional[str],
                    client_kwargs: Dict[str, Any]) -> "OddsPortalClient":
        with cls._geo_pool_lock:
            c = cls._geo_pool.get(proxy)
            if c is None:
                c = cls._geo_pool[proxy] = cls(proxy=proxy, **client_kwargs)
            return c

    @classmethod
    def get_event_odds_multi(cls, event_url: str,
                             locations: Dict[str, Optional[str]], *,
                             client_kwargs: Optional[Dict[str, Any]] = None,
                             max_workers: Optional[int] = None,
                             markets: Sequence[int] = (),
                             on_partial: Optional[Callable[["EventOdds"], None]]
                             = None) -> EventOdds:
        """Fetch an event's odds across many geos concurrently and merge the
        bookmaker sets. `locations` is {label: proxy_url_or_None}; each is hit
        in parallel through its own pooled client. Books hidden from one geo
        (Pinnacle, Asian books, exchanges) show up in another, so the merged
        result carries a far wider price set than a single-IP fetch.
        Dead/blocked proxies are skipped, not raised.

        The event page config (feed path etc.) is geo-independent, so it is
        fetched once via the direct client and shared: each geo pays only the
        small encrypted feed fetch, not the ~160KB HTML."""
        client_kwargs = client_kwargs or {}
        workers = max_workers or max(1, len(locations))
        clients = {label: cls._geo_client(px, client_kwargs)
                   for label, px in locations.items()}
        direct = clients.get("direct") or next(iter(clients.values()))
        try:
            cfg = direct._event_page_config(event_url)
        except Exception:
            cfg = None  # geos fall back to their own page fetch

        def _one(c):
            # circuit breaker: a geo that failed (dead proxy, timeout) is
            # skipped for a cooldown instead of re-paying its timeout on
            # every click — this is what keeps repeat clicks snappy when a
            # free proxy dies mid-session.
            if getattr(c, "_dead_until", 0) > time.time():
                return None
            try:
                r = c.get_event_odds(event_url, cfg=cfg, markets=markets)
                # A limited geo's page (e.g. US, odds display pulled 2026-07)
                # emits a default betting type its own feed can't price, so a
                # shared cfg from it fetches valid-but-EMPTY feeds everywhere.
                # If the shared-cfg fetch priced nothing, re-derive this geo's
                # own cfg (bypassing the odds cache) before giving up.
                if cfg is not None and r is not None \
                        and not any(o.books for o in r.outcomes):
                    r2 = c.get_event_odds(event_url, _fresh=True,
                                          markets=markets)
                    if r2 is not None and any(o.books for o in r2.outcomes):
                        return r2
                return r
            except requests.RequestException:
                c._dead_until = time.time() + 120
                return None
            except Exception:
                if cfg is None:
                    return None
                try:  # shared cfg may be stale for this geo — refetch locally
                    return c.get_event_odds(event_url, _fresh=True,
                                            markets=markets)
                except Exception:
                    c._dead_until = time.time() + 120
                    return None

        results: List[EventOdds] = []
        painted_first = False
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one, c) for c in clients.values()]
            for f in as_completed(futs):
                r = f.result()
                if r is None:
                    continue
                results.append(r)
                # Progressive paint: the moment the FIRST geo with real prices
                # lands, hand back a merge-so-far so the caller can render
                # immediately instead of waiting on the slowest free proxy. This
                # is the big win for geo-limited (e.g. US) users whose direct
                # feed is empty — books now appear at first-geo latency, not
                # full-merge latency. Later geos still enrich the final return.
                if (on_partial is not None and not painted_first
                        and any(o.books for o in r.outcomes)):
                    snap = cls._merge_event_odds(results)
                    if snap is not None:
                        painted_first = True
                        try:
                            on_partial(snap)
                        except Exception:
                            pass
        merged = cls._merge_event_odds(results)
        if merged is None:
            raise ValueError(f"no geo returned odds for {event_url}")
        return merged

    @staticmethod
    def _parse_search_row(row: Dict[str, Any]) -> SearchMatch:
        odds = []
        for o in row.get("odds", []) or []:
            odds.append(OutcomeOdds(
                outcome_result_id=o.get("outcomeResultId"),
                betting_type_id=o.get("bettingTypeId"),
                scope_id=o.get("scopeId"),
                avg_odds=o.get("avgOdds"),
                max_odds=o.get("maxOdds"),
                max_odds_provider_id=o.get("maxOddsProviderId"),
                active=o.get("active"),
                bookmaker_count=o.get("cntActive"),
            ))
        return SearchMatch(
            id=row.get("id"),
            sport_id=row.get("sport-id"),
            sport=row.get("sport-url-name", ""),
            home=row.get("home-name", ""),
            away=row.get("away-name", ""),
            tournament=row.get("tournament-name", ""),
            tournament_url=row.get("tournament-url", ""),
            country=row.get("country-name", ""),
            start_ts=row.get("date-start-timestamp"),
            status=row.get("event-stage-name", ""),
            result=row.get("result", ""),
            url=row.get("url", ""),
            bookmaker_count=row.get("bookmakersCount"),
            odds=odds,
        )

    # -- dropping odds -------------------------------------------------------
    # The dropping-odds FEED is gone. The Next.js rewrite (2026-08) renders this
    # board server-side — /proxy/feed/dropping-odds/<...>.dat still resolves but
    # answers {"E":"notAllowed"} for every hash the pages expose, and the page
    # no longer ships an xHash at all. So the rows are read out of the page's
    # React-Flight payload instead of an encrypted feed.
    #
    # Page URL:  /dropping-odds/<period>/<bs>/<betType>/<sport>
    #   period  1=1h, 2=12h, 3=24h
    #   bs      min dropping-bookies %, 1=10% .. 5=50%
    #   betType 0=all, else a bettingType id (1=1X2, 2=O/U, 3=Home/Away, ...)
    #   sport   "overall" = all sports, else a sport url-name ("football")
    #
    # Each row is one Flight chunk holding a "group-dropping_<outcomeId>" div.
    # Every row is emitted TWICE (desktop + mobile layouts), hence the dedup on
    # outcome id. The bookies cell is usually inline in the row's own chunk, but
    # the first row defers it to a separate chunk via a "$L<id>" reference.
    _CHUNK_RE = re.compile(r'(?m)^([0-9a-f]+):(?=[\[{])')
    _DROP_ID_RE = re.compile(r'"group-dropping_([A-Za-z0-9]+)"')
    _DROP_CELL_RE = re.compile(r'"cell":(\{"columnKey":"bookies".*?\})\}\]\}\]')
    _LREF_RE = re.compile(r'"\$L([0-9a-f]+)"')

    @classmethod
    def _flight_chunks(cls, blob: str) -> Dict[str, str]:
        """Split a React-Flight payload into its {chunkId: body} definitions."""
        out, marks = {}, list(cls._CHUNK_RE.finditer(blob))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(blob)
            out[m.group(1)] = blob[m.end():end]
        return out

    def _dropping_fetch(self, sport, period, bs, bet_type, _page0=0):
        sport_seg = "overall" if str(sport) in ("0", "", "overall") else str(sport)
        url = f"/dropping-odds/{period}/{bs}/{bet_type}/{sport_seg}"
        blob = self._next_payload(self._get(url).text)
        chunks = self._flight_chunks(blob)
        rows, seen = [], set()
        for cid, seg in chunks.items():
            m = self._DROP_ID_RE.search(seg)
            if not m or m.group(1) in seen:
                continue
            cell = self._DROP_CELL_RE.search(seg)
            if cell is None:  # deferred to a referenced chunk
                for ref in self._LREF_RE.findall(seg):
                    cell = self._DROP_CELL_RE.search(chunks.get(ref, ""))
                    if cell is not None:
                        break
            try:
                row = self._parse_dropping_row(m.group(1), seg, cell)
            except Exception as e:
                self._log(f"dropping row {cid} unparseable: {e}")
                continue
            seen.add(m.group(1))
            rows.append(row)
        return rows, 1  # the board is a single server-rendered page now

    def dropping_odds(self, sport="0", period: int = 2, bs: int = 2,
                      bet_type=0, page: int = 1) -> List[DroppingOdd]:
        """A single page of dropping-odds rows.

        sport:    "0" = all sports, else a sport url-name (e.g. "football", "tennis")
        period:   1=last 1h, 2=last 12h, 3=last 24h
        bs:       minimum dropping-bookies %, 1=10% .. 5=50%
        bet_type: 0=all, else a bettingType id (1=1X2, 2=Over/Under, 3=Home/Away,
                  5=Asian Handicap, 6=Draw No Bet, 9=HT/FT, 13=BTTS, ...)
        page:     1-based page within the result set
        """
        rows, _ = self._dropping_fetch(sport, period, bs, bet_type, page - 1)
        rows.sort(key=lambda d: d.drop_pct)  # biggest drop (most negative) first
        return rows

    def dropping_odds_pages(self, sport="0", period: int = 2, bs: int = 2,
                            bet_type=0, max_pages: int = 10) -> List[DroppingOdd]:
        """All pages of dropping odds for a filter (page count is only known
        after the first fetch, so page 1 is sequential then the rest parallel).

        The feed re-ranks continuously, so the same event can surface on two
        pages fetched moments apart; results are de-duplicated on
        (event_id, betting_type), keeping the first (larger) drop seen.
        """
        out, page_count = self._dropping_fetch(sport, period, bs, bet_type, 0)
        rest = list(range(2, min(page_count, max_pages) + 1))
        if rest:
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futs = {ex.submit(self._dropping_fetch, sport, period, bs, bet_type, p - 1): p
                        for p in rest}
                bypage = {}
                for f in as_completed(futs):
                    p = futs[f]
                    try:
                        bypage[p] = f.result()[0]
                    except Exception as e:
                        self._log(f"dropping page {p} failed: {e}")
                        bypage[p] = []
                for p in rest:
                    out.extend(bypage.get(p, []))
        seen, deduped = set(), []
        for d in out:
            k = (d.event_id, d.betting_type)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(d)
        deduped.sort(key=lambda d: d.drop_pct)  # biggest drop (most negative) first
        return deduped

    @staticmethod
    def _parse_dropping_row(outcome_id: str, seg: str,
                            cell: Optional[re.Match]) -> DroppingOdd:
        """Build a DroppingOdd from one row's React-Flight chunk.

        The row renders one market of one event: a breadcrumb (sport / country /
        tournament), the matchup, the moved outcome's label, and the price pair
        (opening -> current) that produced the drop %. Unlike the old feed there
        is no full 1/X/2 ladder here, so `outcomes` carries the single moved
        outcome — which is the one `dropped_outcome` / `odds_str` report anyway.
        """
        def s(pat, default=""):
            m = re.search(pat, seg)
            return _html.unescape(m.group(1)) if m else default

        segs = re.search(r'"segments":(\[.*?\]),"is', seg)
        crumbs = {}
        if segs:
            for c in json.loads(segs.group(1)):
                crumbs[c.get("type")] = c.get("name", "")

        # Prices: the opening is rendered muted (text-gray-dark) and the current
        # bold (text-black-main). Fall back to document order if the utility
        # classes change, since only their ORDER is load-bearing.
        prices = [float(x) for x in
                  re.findall(r'"children":"(\d+(?:\.\d+)?)"', seg)]
        opening = s(r'text-gray-dark","children":"(\d+(?:\.\d+)?)"') \
            or (prices[0] if prices else None)
        current = s(r'text-black-main","children":"(\d+(?:\.\d+)?)"') \
            or (prices[1] if len(prices) > 1 else None)

        # "outcomes":["1","Best Current Odds"] — the moved outcome, then the
        # column header; "outcomeLeft" is the market ("EH -1, 1st Half").
        outs = re.search(r'"outcomes":(\[[^\]]*\])', seg)
        name = ""
        if outs:
            arr = json.loads(outs.group(1))
            name = str(arr[0]) if arr else ""

        cd = json.loads(cell.group(1)) if cell else {}
        bm = cd.get("bookmaker") or {}
        href = s(r'"href":"([^"]+)"')
        ts = s(r'"date":"datet t(\d+)')
        when = time.localtime(int(ts)) if ts else None

        return DroppingOdd(
            # the encoded market/outcome id — identity only (the numeric xuid
            # the old feed carried is not exposed by the rendered board)
            event_id=outcome_id,
            sport=crumbs.get("sport", ""),
            country=crumbs.get("country", ""),
            tournament=crumbs.get("tournament", ""),
            home=s(r'"homeParticipant":"([^"]*)"'),
            away=s(r'"awayParticipant":"([^"]*)"'),
            date=time.strftime("%d %b %Y", when) if when else "",
            time=time.strftime("%H:%M", when) if when else "",
            # strip the market fragment (…/#EHUTapLa:over-under;…) so every
            # market of an event shares one url (page/odds caches unify)
            event_url=href.split("#")[0],
            betting_type=s(r'"outcomeLeft":"([^"]*)"'),
            drop=s(r'"children":"(-?\d+%)"'),
            bookies=cd.get("display", ""),
            max_odds=cd.get("maxOdds"),
            max_provider=bm.get("name"),
            outcomes=[{
                "name": name,
                "odd": float(current) if current is not None else None,
                "prev_odd": float(opening) if opening is not None else None,
            }],
        )

    # -- aggregate snapshots -------------------------------------------------
    @staticmethod
    def _run_surface(fn):
        """Run one surface fn, capturing timing and any exception (never raises).
        Returns {ok, data|error, dt}."""
        t = time.perf_counter()
        try:
            return {"ok": True, "data": fn(), "dt": time.perf_counter() - t}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "dt": time.perf_counter() - t}

    def snapshot(self, *, search_query: Optional[str] = None, search_pages: int = 3,
                 include_leagues: bool = True, dropping_sport="0",
                 dropping_period: int = 3, dropping_bs: int = 1,
                 dropping_max_pages: int = 50) -> Dict[str, Any]:
        """Fetch all surfaces for THIS (possibly proxied) location concurrently.

        Each surface is isolated: one failing (or a slow/dead proxy) does not
        sink the others — failures land in snap["errors"][surface]. Also guards
        the regulated-market redirect (e.g. ES -> cuotasahora.com), which this
        client does not support, returning a clear error instead of a cryptic one.
        """
        snap: Dict[str, Any] = {
            "geo": None, "host": None, "sports": None, "leagues": None,
            "dropping": None, "search": None, "errors": {}, "timings": {},
            "elapsed": 0.0,
        }
        t0 = time.perf_counter()

        # geo probe + regulated-redirect guard (also warms the session)
        try:
            r = self._get("/")
            snap["geo"] = r.headers.get("x-country-code")
            snap["host"] = urlsplit(r.url).netloc
            if "oddsportal.com" not in (snap["host"] or ""):
                snap["errors"]["geo"] = (
                    f"regulated-market redirect to {snap['host']}; "
                    f"client unsupported for geo {snap['geo']}")
                snap["elapsed"] = time.perf_counter() - t0
                return snap
        except Exception as e:
            snap["errors"]["geo"] = f"{type(e).__name__}: {e}"
            snap["elapsed"] = time.perf_counter() - t0
            return snap

        def _taxonomy():
            sports = self.get_sports()
            leagues = (self.get_all_leagues([s.id for s in sports])
                       if include_leagues else None)
            return {"sports": sports, "leagues": leagues}

        def _search():
            if not search_query:
                return {"search": None}
            return {"search": self.search(search_query, pages=search_pages)}

        def _dropping():
            return {"dropping": self.dropping_odds_pages(
                dropping_sport, dropping_period, dropping_bs,
                max_pages=dropping_max_pages)}

        tasks = {"taxonomy": _taxonomy, "search": _search, "dropping": _dropping}
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(self._run_surface, fn): name for name, fn in tasks.items()}
            for fut in as_completed(futs):
                name = futs[fut]
                res = fut.result()  # _run_surface never raises
                snap["timings"][name] = round(res["dt"], 2)
                if res["ok"]:
                    snap.update(res["data"])
                else:
                    snap["errors"][name] = res["error"]
                    self._log(f"[{name}] {res['error']}")
        snap["elapsed"] = round(time.perf_counter() - t0, 2)
        return snap

    @classmethod
    def multi_geo(cls, locations: Dict[str, Optional[str]], *,
                  client_kwargs: Optional[Dict[str, Any]] = None,
                  max_location_workers: Optional[int] = None,
                  **snapshot_kwargs) -> Dict[str, Dict[str, Any]]:
        """Run snapshot() across many locations in parallel.

        locations: {label: proxy_url_or_None}.  Returns {label: snapshot}.
        Per-location failures (bad proxy, client init error) are captured in
        snap["errors"]["location"] rather than raised, so one dead proxy never
        aborts the whole sweep.
        """
        client_kwargs = client_kwargs or {}
        workers = max_location_workers or max(1, len(locations))

        def _one(label, proxy):
            try:
                c = cls(proxy=proxy, **client_kwargs)
                return c.snapshot(**snapshot_kwargs)
            except Exception as e:
                return {"geo": None, "host": None, "sports": None, "leagues": None,
                        "dropping": None, "search": None,
                        "errors": {"location": f"{type(e).__name__}: {e}"},
                        "timings": {}, "elapsed": 0.0}

        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_one, lbl, px): lbl for lbl, px in locations.items()}
            for fut in as_completed(futs):
                lbl = futs[fut]
                try:
                    results[lbl] = fut.result()
                except Exception as e:  # _one is already guarded; belt & suspenders
                    results[lbl] = {"errors": {"location": f"{type(e).__name__}: {e}"},
                                    "timings": {}, "elapsed": 0.0}
        return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_json(obj):
    if isinstance(obj, list):
        print(json.dumps([asdict(x) for x in obj], indent=2, ensure_ascii=False))
    else:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(description="OddsPortal internal API client")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--proxy", default=None,
                    help="proxy URL (http://.. or socks5h://..) to change egress "
                         "geo and unlock non-US books; or set ODDSPORTAL_PROXY")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sports", help="list all sports")
    sub.add_parser("geo", help="show the country code OddsPortal sees (book set)")

    pl = sub.add_parser("leagues", help="list leagues for a sport")
    pl.add_argument("--sport", type=int, required=True)

    pa = sub.add_parser("all-leagues", help="list leagues for every sport")

    ps = sub.add_parser("search", help="search matches (historical odds)")
    ps.add_argument("query")
    ps.add_argument("--pages", type=int, default=1)

    pd = sub.add_parser("dropping", help="dropping odds")
    pd.add_argument("--sport", default="0",
                    help='"0" for all, or a sport url-name e.g. football, tennis')
    pd.add_argument("--period", type=int, default=2, help="1=1h 2=12h 3=24h")
    pd.add_argument("--bs", type=int, default=2, help="min dropping-bookies %%: 1=10%%..5=50%%")
    pd.add_argument("--bet-type", type=float, default=0,
                    help="0=all 1=1X2 2=O/U 3=Home/Away 5=AH 6=DNB 9=HT/FT 13=BTTS")
    pd.add_argument("--pages", type=int, default=1)

    pw = sub.add_parser("sweep",
                        help="run all surfaces across many locations in parallel")
    pw.add_argument("--location", "-l", action="append", default=[], metavar="LABEL=PROXY",
                    help="repeatable; e.g. -l 'UK=http://u:p@host:port'. "
                         "Use PROXY='direct' or omit '=' for no proxy.")
    pw.add_argument("--no-direct", action="store_true",
                    help="do not include the direct (no-proxy) location")
    pw.add_argument("--search", default=None, help="optional search token to include")
    pw.add_argument("--json", action="store_true", help="dump full JSON instead of summary")

    pm = sub.add_parser("markets",
                        help="all markets for one event (moneyline, totals, handicaps)")
    pm.add_argument("event_url",
                    help="site path, e.g. /baseball/h2h/<home>/<away>/#<encodedId>")
    pm.add_argument("--bets", default=None,
                    help="comma-separated betting-type ids (default 3,2,5). "
                         + ", ".join(f"{k}={v}" for k, v in sorted(BETTING_TYPES.items())))
    pm.add_argument("--main", action="store_true",
                    help="only the consensus line per market")

    args = ap.parse_args(argv)

    if args.cmd == "sweep":
        return _run_sweep(args)

    c = OddsPortalClient(verbose=args.verbose, proxy=args.proxy)

    if args.cmd == "markets":
        bets = (tuple(int(x) for x in args.bets.split(","))
                if args.bets else c.DEFAULT_MARKETS)
        eo = c.get_event_odds(args.event_url, markets=bets)
        print(f"{eo.away} @ {eo.home}   {eo.tournament}")
        seen = []
        for ln in eo.markets:
            if ln.market not in seen:
                seen.append(ln.market)
        for slug in seen:
            lines = [eo.main_line(slug)] if args.main else eo.lines(slug)
            for ln in lines:
                if not ln:
                    continue
                legs = "  ".join(
                    f"{o.name} {o.avg_odds}"
                    + (f" (open {o.opening_avg})" if o.opening_avg else "")
                    for o in ln.outcomes if o.avg_odds)
                print(f"  {ln.label:<28s} {legs}   [{ln.n_books} books]")
    elif args.cmd == "sports":
        _print_json(c.get_sports())
    elif args.cmd == "geo":
        print(c.geo())
    elif args.cmd == "leagues":
        _print_json(c.get_leagues(args.sport))
    elif args.cmd == "all-leagues":
        allg = c.get_all_leagues()
        print(json.dumps({sid: [asdict(l) for l in ls] for sid, ls in allg.items()},
                         indent=2, ensure_ascii=False))
    elif args.cmd == "search":
        _print_json(c.search(args.query, pages=args.pages))
    elif args.cmd == "dropping":
        bt = int(args.bet_type) if float(args.bet_type).is_integer() else args.bet_type
        if args.pages > 1:
            _print_json(c.dropping_odds_pages(args.sport, args.period, args.bs,
                                              bet_type=bt, max_pages=args.pages))
        else:
            _print_json(c.dropping_odds(args.sport, args.period, args.bs, bet_type=bt))


def _run_sweep(args):
    locations: Dict[str, Optional[str]] = {}
    if not args.no_direct:
        locations["direct"] = None
    for spec in args.location:
        if "=" in spec:
            label, proxy = spec.split("=", 1)
        else:
            label, proxy = spec, "direct"
        locations[label.strip()] = None if proxy.strip().lower() == "direct" else proxy.strip()
    if not locations:
        print("no locations to sweep (did you pass --no-direct with no -l?)", file=sys.stderr)
        return

    t0 = time.perf_counter()
    results = OddsPortalClient.multi_geo(
        locations,
        client_kwargs={"verbose": args.verbose, "timeout": 30, "max_workers": 12},
        search_query=args.search)
    wall = time.perf_counter() - t0

    if args.json:
        def _enc(o):
            if isinstance(o, list):
                return [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in o]
            if isinstance(o, dict):
                return {k: _enc(v) for k, v in o.items()}
            return o
        print(json.dumps({lbl: _enc(snap) for lbl, snap in results.items()},
                         indent=2, ensure_ascii=False))
        return

    print(f"\nswept {len(locations)} locations in {wall:.1f}s wall-clock "
          f"(parallel)\n")
    hdr = f"{'LOCATION':<14} {'GEO':<4} {'SPORTS':>6} {'LEAGUES':>7} {'DROPS':>6} {'SEARCH':>6} {'TIME':>6}  ERRORS"
    print(hdr); print("-" * len(hdr))
    for lbl in locations:
        s = results.get(lbl, {})
        geo = s.get("geo") or "?"
        nsp = len(s["sports"]) if s.get("sports") else 0
        nlg = sum(len(v) for v in s["leagues"].values()) if s.get("leagues") else 0
        ndr = len(s["dropping"]) if s.get("dropping") else 0
        nse = len(s["search"]) if s.get("search") else 0
        t = s.get("elapsed", 0.0)
        errs = "; ".join(f"{k}:{v}" for k, v in (s.get("errors") or {}).items()) or "-"
        print(f"{lbl:<14} {geo:<4} {nsp:>6} {nlg:>7} {ndr:>6} {nse:>6} {t:>5.1f}s  {errs}")


if __name__ == "__main__":
    main()
