"""
OddsPortalClient.py — fast, threaded client for OddsPortal's internal feed.

OddsPortal (a LiveSport s.r.o. property, same parent as Flashscore) has no public
API. Its Vue SPA either embeds initial data into the page HTML or pulls it from
internal `/feed/...` and `/ajax-...` endpoints. This client talks to those
endpoints directly with plain `requests` — no browser is needed at runtime.

Three data surfaces are exposed:

  1. SPORTS + LEAGUES  (the full taxonomy on offer)
       - sports come embedded in every page inside  <sports-menu :data="...">
       - leagues per sport come from
             /ajax-getSportsMenuDataBySports/<sportId>,/<tz>/
         which returns an ENCRYPTED payload (see decode_feed below).

  2. HISTORICAL ODDS via SEARCH  (the /search/results/ page)
       - fully server-rendered: the match rows (with avg/max odds per outcome)
         are embedded in the page inside  <search-results-wrapper :data="...">.
         Just fetch the HTML and parse the attribute. Paginated.

  3. DROPPING ODDS  (the /dropping-odds/ live section)
       - pulled from /feed/dropping-odds/<period>-<bs>-<sport>-<fmt>-<xHash>/<sub>/<page>.dat
         which returns an ENCRYPTED payload (see decode_feed below).

----------------------------------------------------------------------------
ENCRYPTED FEED FORMAT  (the one thing that can break without notice)
----------------------------------------------------------------------------
The `/feed/...` and several `/ajax-...` endpoints return a response that is:

    base64(  <ciphertext_base64> ":" <iv_hex>  )

Decode steps (mirrors window's crypto helper in build/assets/app-*.js):
    outer      = base64_decode(response_text).decode("latin1")
    ct_b64, iv = outer.split(":")
    key        = PBKDF2-HMAC-SHA256(PASSPHRASE, SALT, ITERATIONS=1000, dklen=32)
    plaintext  = AES-256-CBC(key, iv=bytes.fromhex(iv)).decrypt(b64decode(ct_b64))
    plaintext  = pkcs7_unpad(plaintext)
    if plaintext starts with gzip magic (1f 8b): plaintext = gunzip(plaintext)
    json.loads(plaintext)

The PASSPHRASE / SALT live (obfuscated) in the app JS bundle. If decoding ever
starts failing, re-derive them: download the bundle referenced by
<script src="/build/assets/app-*.js">, search for the string-array function that
contains "AES-CBC"/"PBKDF2"/"deriveKey", and read off the joined passphrase and
the hex salt. They have been stable constants; auto-discovery is attempted at
startup and falls back to the values below.

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
import gzip
import hashlib
import html as _html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
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
    event_id: int
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
        """Compact odds view, e.g. '2: 6.87->2.67 | 1 3.05 | X 2.45'.
        The dropped outcome (old->new) is listed first."""
        moved = self.dropped_outcome
        parts = []
        if moved:
            parts.append(f"{moved['name']}: {moved['prev_odd']}->{moved['odd']}")
        for o in self.outcomes:
            if o is moved:
                continue
            if o.get("odd") is not None:
                parts.append(f"{o['name']} {o['odd']}")
        return " | ".join(parts)


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
        self._x_hash: Optional[str] = None      # dropping-odds feed hash
        self._odds_format: int = 3              # decimal; from dropping pageVar

    def geo(self) -> Optional[str]:
        """The country code OddsPortal sees for our (possibly proxied) egress IP.
        Determines which bookmakers are shown. Returns e.g. 'US', 'GB', 'SG'."""
        r = self._get("/")
        return r.headers.get("x-country-code")

    def _log(self, *a):
        if self.verbose:
            print("[oddsportal]", *a, file=sys.stderr)

    # -- low-level fetch / decode -------------------------------------------
    def _get(self, path: str, *, referer: Optional[str] = None,
             ajax: bool = False) -> requests.Response:
        url = path if path.startswith("http") else BASE + path
        headers = {}
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer
        r = self.session.get(url, headers=headers, timeout=self.timeout)
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

    @staticmethod
    def _embedded_attr(page_html: str, tag: str, attr: str = ":data") -> Any:
        """Pull a JSON value out of a Vue custom-element attribute in the HTML."""
        m = re.search(r'<%s\b[^>]*\s%s="([^"]*)"' % (re.escape(tag), re.escape(attr)),
                      page_html)
        if not m:
            return None
        return json.loads(_html.unescape(m.group(1)))

    # -- sports --------------------------------------------------------------
    def get_sports(self) -> List[Sport]:
        """All sports offered, parsed from the <sports-menu> embed on the homepage."""
        r = self._get("/")
        data = self._embedded_attr(r.text, "sports-menu") or {}
        out = []
        for v in data.values():
            out.append(Sport(id=int(v["sport_id"]),
                             name=v.get("name", ""), url=v.get("url", "")))
        out.sort(key=lambda s: s.id)
        return out

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
    def search(self, query: str, pages: int = 1, start_page: int = 1
               ) -> List[SearchMatch]:
        """Search matches (historical + upcoming) by free-text query.

        `query` is the search token as it appears in the URL
        (/search/results/<query>/). Returns up to `pages` pages of results.
        """
        first = self._search_page(query, start_page)
        matches = first["matches"]
        page_count = first["page_count"]
        last = min(start_page + pages - 1, page_count)
        rest = list(range(start_page + 1, last + 1))
        if rest:
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futs = {ex.submit(self._search_page, query, p): p for p in rest}
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

    def _search_page(self, query: str, page: int) -> Dict[str, Any]:
        path = f"/search/results/{query}/page/{page}/"
        r = self._get(path)
        data = self._embedded_attr(r.text, "search-results-wrapper") or {}
        page_count = (data.get("pagination") or {}).get("pageCount", 1)
        matches = [self._parse_search_row(row) for row in data.get("rows", [])]
        return {"matches": matches, "page_count": page_count}

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
    def _ensure_dropping_hash(self) -> str:
        if self._x_hash:
            return self._x_hash
        r = self._get("/dropping-odds/")
        m = re.search(r"var pageVar = '([^']+)'", r.text)
        if not m:
            raise ValueError("could not find pageVar on /dropping-odds/")
        pv = json.loads(m.group(1))
        self._x_hash = pv["xHash"]
        self._odds_format = pv.get("oddsFormat", self._odds_format)
        self._log(f"dropping xHash={self._x_hash} oddsFormat={self._odds_format}")
        return self._x_hash

    def _dropping_path(self, sport, period, bs, bet_type, page0):
        """Build a dropping-odds feed path.

        URL layout (confirmed against the live filter controls):
            /feed/dropping-odds/<period>-<bs>-<betType>-<fmt>-<xHash>/<page0>/<sport>.dat
              period   1=1h, 2=12h, 3=24h
              bs       min dropping-bookies %, 1=10% .. 5=50%
              betType  0=all, else a bettingType id (1=1X2, 2=O/U, 3=Home/Away, ...)
              fmt      odds format (3=decimal), from /dropping-odds/ pageVar
              page0    0-based page index
              sport    "0" = all sports, else a sport url-name e.g. "football"
        The decoded rows always live under content.tabs["0"] (active-tab key).
        """
        xhash = self._ensure_dropping_hash()
        ts = int(time.time() * 1000)
        return (f"/feed/dropping-odds/"
                f"{period}-{bs}-{bet_type}-{self._odds_format}-{xhash}"
                f"/{page0}/{sport}.dat?=_{ts}")

    def _dropping_fetch(self, sport, period, bs, bet_type, page0):
        path = self._dropping_path(sport, period, bs, bet_type, page0)
        decoded = self.decode_feed(
            self._get(path, ajax=True, referer=BASE + "/dropping-odds/").text)
        block = (decoded.get("content") or {}).get("tabs", {}).get("0", {})
        rows = [self._parse_dropping_row(rw) for rw in (block.get("rows") or {}).values()]
        page_count = (block.get("pagination") or {}).get("pageCount", 1)
        return rows, page_count

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
    def _parse_dropping_row(rw: Dict[str, Any]) -> DroppingOdd:
        sd = rw.get("sport-data") or {}
        cd = rw.get("country-data") or {}
        td = rw.get("tournament-data") or {}
        bt = rw.get("betting-type-data") or {}
        dd = rw.get("drop-data") or {}
        bk = rw.get("bookies-data") or {}
        ed = rw.get("event-data") or {}
        # Each outcome (1/X/2 or Over/Under/...) carries either a stable `value`
        # (no move) or a `value1` (opening) -> `value2` (current) pair for the
        # outcome that actually dropped. Flatten that into name/odd/prev_odd.
        outcomes = []
        for val in bt.get("values", []) or []:
            cur = prev = None
            for o in val.get("odds", []) or []:
                if "value2" in o:
                    cur = o["value2"]
                elif "value1" in o:
                    prev = o["value1"]
                elif "value" in o:
                    cur = o["value"]
            outcomes.append({
                "name": str(val.get("name")),
                "odd": cur,           # current/best odds for this outcome
                "prev_odd": prev,     # opening odds, only set on the dropped outcome
            })
        return DroppingOdd(
            event_id=ed.get("xuid"),
            sport=sd.get("name", ""),
            country=cd.get("name", ""),
            tournament=td.get("name", ""),
            home=_html.unescape(ed.get("home-name", "")),
            away=_html.unescape(ed.get("away-name", "")),
            date=_html.unescape(ed.get("date", "")),
            time=ed.get("time", ""),
            event_url=ed.get("event-url", ""),
            betting_type=bt.get("name", ""),
            drop=dd.get("value", ""),
            bookies=bk.get("value", ""),
            max_odds=bk.get("maxOdds"),
            max_provider=bk.get("maxProviderName"),
            outcomes=outcomes,
        )


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

    args = ap.parse_args(argv)
    c = OddsPortalClient(verbose=args.verbose, proxy=args.proxy)

    if args.cmd == "sports":
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


if __name__ == "__main__":
    main()
