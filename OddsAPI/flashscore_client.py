"""
flashscore_client.py — threaded client for Flashscore's internal feed.

Flashscore has no public API. It does have an internal "feed" endpoint that the
website's SPA hits directly. This client talks to that endpoint.

Two moving pieces can break it without notice:
  * the feed HOST  (migrated from d.flashscore.com -> 2.flashscore.ninja)
  * the x-fsign TOKEN  (currently a long-lived constant embedded in the homepage)

Both are auto-discovered from the live homepage at startup (see discover_credentials).
If discovery fails we fall back to the last-known values and LOUDLY warn the user
with exact instructions for refreshing them by hand.

The feed is NOT JSON. It is a flat record stream using three delimiters:
    ~   separates records (a record is a tournament header OR an event)
    ¬   separates fields within a record
    ÷   separates key/value within a field   ->  "AA÷MiHypBQh"
We decode that into tournaments, each holding a list of events.

Schedule feed key format:
    f_<sportId>_<dayOffset>_<tz>_<lang>_<projectId>
        sportId    see SPORT_IDS
        dayOffset  0 = today, -1 = yesterday, +1 = tomorrow, ...
        tz         UTC hour offset bucket (only affects day grouping; AD is absolute unix)
        lang       "en"
        projectId  1 for flashscore.com

Event-detail feed key format:
    g_<sportId>_<eventId>
"""

#TODO: Add set scores beyond first set for Tennis

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Last-known credentials. Auto-discovery overrides these; they are the fallback.
# ---------------------------------------------------------------------------
DEFAULT_FEED_HOST = "https://2.flashscore.ninja/2/x/feed"
DEFAULT_FSIGN = "SW9D1eZo"
HOMEPAGE = "https://www.flashscore.com/"

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Canonical Flashscore sport ids (stable for years; verified against live feed).
SPORT_IDS: Dict[str, int] = {
    "football": 1,        # soccer
    "tennis": 2,
    "basketball": 3,
    "hockey": 4,          # ice hockey
    "american_football": 5,
    "baseball": 6,
    "handball": 7,
    "rugby_union": 8,
    "floorball": 9,
    "bandy": 10,
    "futsal": 11,
    "volleyball": 12,
    "cricket": 13,
    "darts": 14,
    "snooker": 15,
    "boxing": 16,
    "beach_volleyball": 17,
    "aussie_rules": 18,
    "rugby_league": 19,
    "badminton": 21,
    "water_polo": 22,
    "golf": 23,
    "field_hockey": 24,
    "table_tennis": 25,
    "beach_soccer": 26,
    "pesapallo": 30,
    "motorsport": 32,
    "cycling": 34,
    "horse_racing": 35,
    "esports": 36,
}
SPORT_NAMES: Dict[int, str] = {v: k for k, v in SPORT_IDS.items()}

# Feed field keys (the ones worth naming; many more exist and are passed through raw).
FIELD_KEYS = {
    # tournament / header
    "ZA": "tournament_header",   # "COUNTRY: League - Stage"
    "ZEE": "tournament_id",
    "ZB": "country_id",
    "ZY": "country_name",
    "ZC": "stage_id",
    "ZL": "tournament_url",
    "ZJ": "tournament_short",
    # event
    "AA": "event_id",
    "AD": "start_ts",            # unix seconds (absolute, UTC)
    "ADE": "start_ts2",
    "AC": "status_code",         # detailed status; sport-specific in-progress codes
    "AB": "stage_type",          # 1=scheduled, 2=LIVE, 3=finished — the reliable live signal
    "AI": "live_indicator",      # "y" while in-play (corroborates AB==2)
    "AE": "home_name",
    "AF": "away_name",
    "AG": "home_score",
    "AH": "away_score",
    "AT": "home_score_cur",
    "AU": "away_score_cur",
    "WM": "home_abbr",
    "WN": "away_abbr",
    "OA": "home_logo",
    "OB": "away_logo",
    "JA": "home_id",
    "JB": "away_id",
    "AL": "odds_json",           # bookmaker odds, JSON string
    "AN": "has_live_coverage",   # "y" = live-tracking available; NOT "currently live"
    "AM": "note",                # e.g. aggregate / leg result
    "AW": "winner",
}

# Best-effort status code map (Flashscore AC codes; covers the common ones).
STATUS_CODES = {
    "1": "scheduled",
    "2": "live",
    "3": "finished",
    "4": "postponed",
    "5": "canceled",
    "6": "live",          # in-progress variants
    "7": "live",
    "8": "interrupted",
    "9": "abandoned",
    "10": "live",
    "11": "halftime",
    "36": "after_extra_time",
    "37": "after_penalties",
    "42": "awarded",
    "43": "delayed",
    "45": "walkover",
    "46": "retired",
}

# AB stage type — the reliable, sport-agnostic live/scheduled/finished signal.
STAGE_TYPES = {"1": "scheduled", "2": "live", "3": "finished"}

# Baseball: status code for the 1st inning. inning = status_code - BASE + 1.
# Empirically derived and confirmed against a live game (code 28 == 3rd inning).
BASEBALL_INNING_BASE = 26


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


_CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$"}


def _format_race_info(zn: List[str]) -> Optional[str]:
    """Compose a readable horse-race meta line from the ZN fields:
    distance · conditions · prize  (e.g. "1m 1y · 3yo+ · $30,996").
    ZN layout: [0]=start_ts, [2]=distance, [5]=prize "30996.0 USD", [7]=conditions.
    """
    distance = zn[2] if len(zn) > 2 and zn[2] else None
    conditions = next((p for p in zn if p and p[0].isdigit() and "yo" in p), None)
    prize = None
    for p in zn:
        toks = p.split()
        if len(toks) == 2 and toks[1] in _CURRENCY_SYMBOL:
            try:
                amount = int(float(toks[0]))
                prize = f"{_CURRENCY_SYMBOL[toks[1]]}{amount:,}"
            except ValueError:
                pass
            break
    return " · ".join(p for p in (distance, conditions, prize) if p) or None


def format_to_par(ag: Optional[str]) -> str:
    """Golf score-to-par for display: 0 -> 'E', -3 -> '-3', 3 -> '+3'."""
    if ag in (None, ""):
        return ""
    if ag == "0":
        return "E"
    return ag if ag.startswith(("-", "+")) else f"+{ag}"


def _derive_status(d: Dict[str, str]) -> Optional[str]:
    """Best status string for an event record.

    Prefer the detailed AC code when we have a name for it; otherwise fall back
    to the coarse AB stage. AB is authoritative for is_live; AC just refines the
    label (e.g. "halftime", "after_penalties") when known.
    """
    ac = d.get("AC", "")
    if ac in STATUS_CODES:
        return STATUS_CODES[ac]
    return STAGE_TYPES.get(d.get("AB", ""), ac or None)


def format_progress(sport_id: int, decoded: Dict[str, Any]) -> str:
    """Compact progress indicator for a live event (fits a table cell).

    Returns just the stage/clock — NOT the score (the table has a score column).
    Examples: baseball "Bot 3rd", tennis "Set 2 · 4-6 (40-0)",
    basketball "Q4", hockey "P2", football "55'".
    """
    d = decoded or {}
    if sport_id == SPORT_IDS["baseball"]:
        if d.get("inning"):
            half = {"top": "Top", "bot": "Bot"}.get(d.get("inning_half"), "")
            return f"{half} {_ordinal(d['inning'])}".strip()
        return "LIVE"

    if sport_id == SPORT_IDS["tennis"]:
        games = (d.get("periods") or [("", "")])[0]   # current-set games (df_sur)
        gp = d.get("game_points") or ("", "")
        parts = []
        if games[0] not in ("", None):
            parts.append(f"{games[0]}-{games[1]}")
        if gp[0] not in ("", None) and (gp[0], gp[1]) != ("0", "0"):
            parts.append(f"({gp[0]}-{gp[1]})")
        return " · ".join(parts) if parts else "LIVE"

    if sport_id == SPORT_IDS["football"]:
        return f"{d['minute']}'" if d.get("minute") is not None else "LIVE"

    # quarter / period sports (basketball, hockey, handball, ...)
    # DI = minutes elapsed in the current period (counts up), matching
    # Flashscore's "1ST QUARTER · 5" display. num_periods = current period.
    n = d.get("num_periods")
    label = "Q" if sport_id == SPORT_IDS["basketball"] else "P"
    parts = []
    if n:
        parts.append(f"{label}{n}")
    clock = d.get("clock")
    if clock not in (None, ""):
        parts.append(f"{clock}'" if str(clock).isdigit() else str(clock))
    return " · ".join(parts) if parts else "LIVE"


# delimiters
REC_SEP = "~"
FIELD_SEP = "¬"
KV_SEP = "÷"


@dataclass
class Event:
    event_id: str
    sport_id: int
    sport: str
    tournament: str
    country: Optional[str]
    home: Optional[str]
    away: Optional[str]
    home_score: Optional[str]
    away_score: Optional[str]
    start_ts: Optional[int]
    start_iso: Optional[str]
    status: Optional[str]
    stage: str          # "scheduled" | "live" | "finished" | "" (from AB)
    is_live: bool
    note: Optional[str]
    tournament_url: Optional[str]
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass
class GolfEntry:
    """One player row on a golf leaderboard (golf records aren't head-to-head)."""
    position: Optional[str]   # CX (shared across ties)
    player: Optional[str]
    country: Optional[str]
    to_par: Optional[str]     # AG, raw (e.g. "-3"); None until teed off
    thru: Optional[int]       # holes completed this round (18 == round done)
    tee_ts: Optional[int]     # tee-time unix ts when not yet started
    started: bool
    event_id: Optional[str]


@dataclass
class ParticipantEvent:
    """A field-of-competitors event (motorsport session, cycling stage, horse
    race, ...). Unlike head-to-head events, each holds many participant rows.
    Entries are kept as raw parsed records; per-sport renderers pick the columns.
    """
    title: str                 # ZA, full ("COUNTRY: VENUE: Race N - desc")
    country: Optional[str]
    venue: Optional[str]       # parsed from title (horse racing: the track)
    race_label: Optional[str]  # parsed from title (horse racing: "Race N - ...")
    url: Optional[str]
    meeting_id: Optional[str]  # ZEE — shared across races at one venue/meeting
    start_ts: Optional[int]
    stage: str                 # scheduled | live | finished
    info: Optional[str]        # distance / prize etc. (from ZN)
    entries: List[Dict[str, str]] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.stage == "live"


@dataclass
class GolfLeaderboard:
    tournament: str
    country: Optional[str]
    url: Optional[str]
    stage: str                # scheduled | live | finished
    entries: List[GolfEntry] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.stage == "live"


# ---------------------------------------------------------------------------
# Credentials discovery
# ---------------------------------------------------------------------------
class CredentialError(RuntimeError):
    pass


def discover_credentials(timeout: int = 20) -> Dict[str, str]:
    """Scrape the live homepage for the current feed host + x-fsign token.

    Returns {"host": ..., "fsign": ...}. Raises CredentialError on hard failure
    so callers can decide whether to fall back to DEFAULT_* and warn.
    """
    resp = requests.get(HOMEPAGE, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    # token: appears verbatim in the page and in the JS bundle as feedSignature.
    # It's an 8-char-ish alnum value. Pull the one next to a "fsign" marker if we
    # can, else fall back to the standalone occurrence of the known token shape.
    fsign = None
    m = re.search(r'fsign["\']?\s*[:=]\s*["\']([A-Za-z0-9]{6,16})["\']', html, re.I)
    if m:
        fsign = m.group(1)
    if not fsign:
        # the token is also emitted bare in the page; match the known shape.
        m = re.search(r'\b([A-Za-z0-9]{8})\b(?=[^A-Za-z0-9]{0,40}feed)', html)
        if m:
            fsign = m.group(1)
    if not fsign and DEFAULT_FSIGN in html:
        fsign = DEFAULT_FSIGN

    # host: the SPA preconnects to the feed origin (e.g. https://2.flashscore.ninja)
    host = None
    m = re.search(r'https://[0-9]+\.flashscore\.ninja', html)
    if m:
        origin = m.group(0)
        # path prefix mirrors the subdomain number: /2/x/feed for 2.flashscore.ninja
        num = re.search(r'//(\d+)\.', origin).group(1)
        host = f"{origin}/{num}/x/feed"

    if not fsign or not host:
        raise CredentialError(
            f"could not discover credentials (fsign={fsign!r}, host={host!r})"
        )
    return {"host": host, "fsign": fsign}


def resolve_credentials(verbose: bool = True) -> Dict[str, str]:
    """Discover credentials, falling back to defaults with a loud warning."""
    try:
        creds = discover_credentials()
        if verbose:
            changed = []
            if creds["host"] != DEFAULT_FEED_HOST:
                changed.append(f"HOST changed: {DEFAULT_FEED_HOST} -> {creds['host']}")
            if creds["fsign"] != DEFAULT_FSIGN:
                changed.append(f"TOKEN changed: {DEFAULT_FSIGN} -> {creds['fsign']}")
            if changed:
                _warn(
                    "Flashscore credentials have CHANGED since this file was written:\n  "
                    + "\n  ".join(changed)
                    + "\nUpdate DEFAULT_FEED_HOST / DEFAULT_FSIGN in flashscore_client.py "
                      "to keep the fallback current."
                )
        return creds
    except Exception as e:  # network down, layout changed, token moved
        _warn(
            f"credential auto-discovery FAILED ({e}).\n"
            f"Falling back to last-known values:\n"
            f"  host  = {DEFAULT_FEED_HOST}\n"
            f"  fsign = {DEFAULT_FSIGN}\n"
            "If requests start returning '0'/empty, refresh them manually:\n"
            "  1. open https://www.flashscore.com/ in a browser\n"
            "  2. DevTools > Network > filter 'feed' > copy the request host and the\n"
            "     'x-fsign' request header\n"
            "  3. paste both into DEFAULT_FEED_HOST / DEFAULT_FSIGN."
        )
        return {"host": DEFAULT_FEED_HOST, "fsign": DEFAULT_FSIGN}


def _warn(msg: str) -> None:
    bar = "!" * 72
    print(f"\n{bar}\n[flashscore_client] {msg}\n{bar}\n", file=sys.stderr)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class FlashscoreClient:
    def __init__(
        self,
        host: Optional[str] = None,
        fsign: Optional[str] = None,
        lang: str = "en",
        tz: int = 0,
        project_id: int = 1,
        max_workers: int = 12,
        timeout: int = 20,
        verbose: bool = True,
    ):
        if host is None or fsign is None:
            creds = resolve_credentials(verbose=verbose)
            host = host or creds["host"]
            fsign = fsign or creds["fsign"]
        self.host = host.rstrip("/")
        self.fsign = fsign
        self.lang = lang
        self.tz = tz
        self.project_id = project_id
        self.max_workers = max_workers
        self.timeout = timeout
        self.verbose = verbose
        # one Session per client; requests.Session is thread-safe for plain GETs.
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-fsign": self.fsign,
                "User-Agent": BROWSER_UA,
                "Referer": HOMEPAGE,
                "Origin": "https://www.flashscore.com",
                "Accept": "*/*",
            }
        )

    # -- raw fetch ----------------------------------------------------------
    def fetch_feed(self, feed_key: str, retries: int = 3, backoff: float = 1.5) -> str:
        url = f"{self.host}/{feed_key}"
        last_exc = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    return r.text
                last_exc = RuntimeError(f"HTTP {r.status_code} for {feed_key}")
            except requests.RequestException as e:
                last_exc = e
            time.sleep(backoff * (attempt + 1))
        raise RuntimeError(f"fetch failed for {feed_key}: {last_exc}")

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _parse_record(rec: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for fld in rec.split(FIELD_SEP):
            if KV_SEP in fld:
                k, v = fld.split(KV_SEP, 1)
                out[k] = v
        return out

    def parse_schedule_feed(self, raw: str, sport_id: int) -> List[Event]:
        """Decode a schedule feed into Event objects.

        The stream interleaves tournament-header records (carry ZA/ZY/ZL...) with
        event records (carry AA/AE/AF...). We carry the most-recent header forward.
        """
        if not raw or raw.strip() == "0":
            return []
        sport = SPORT_NAMES.get(sport_id, str(sport_id))
        events: List[Event] = []
        cur_tour = cur_country = cur_url = None

        for rec in raw.split(REC_SEP):
            d = self._parse_record(rec)
            if not d:
                continue
            if "ZA" in d:  # tournament header
                cur_tour = d.get("ZA")
                cur_country = d.get("ZY")
                cur_url = d.get("ZL")
            if "AA" in d:  # event row
                ts = d.get("AD") or d.get("ADE")
                ts_i = int(ts) if ts and ts.isdigit() else None
                events.append(
                    Event(
                        event_id=d["AA"],
                        sport_id=sport_id,
                        sport=sport,
                        tournament=cur_tour or "",
                        country=cur_country,
                        home=d.get("AE") or d.get("CX"),
                        away=d.get("AF"),
                        home_score=d.get("AG"),
                        away_score=d.get("AH"),
                        start_ts=ts_i,
                        start_iso=(
                            datetime.fromtimestamp(ts_i, tz=timezone.utc).isoformat()
                            if ts_i
                            else None
                        ),
                        status=_derive_status(d),
                        stage=STAGE_TYPES.get(d.get("AB", ""), ""),
                        is_live=(d.get("AB") == "2"),
                        note=d.get("AM"),
                        tournament_url=cur_url,
                        raw=d,
                    )
                )
        return events

    # -- high level ---------------------------------------------------------
    def get_schedule(
        self,
        sports: Optional[List[Any]] = None,
        days: Optional[List[int]] = None,
        include_raw: bool = False,
    ) -> Dict[str, List[Event]]:
        """Threaded fetch of schedules across sports x day-offsets.

        sports: list of sport names or ids; default = all known sports.
        days:   list of day offsets; default = [0] (today).
        Returns {sport_name: [Event, ...]} merged across the requested days.
        """
        sports = sports or list(SPORT_IDS.keys())
        days = days if days is not None else [0]
        # normalize sports -> ids
        sport_ids = []
        for s in sports:
            if isinstance(s, int):
                sport_ids.append(s)
            elif str(s).isdigit():
                sport_ids.append(int(s))
            elif s in SPORT_IDS:
                sport_ids.append(SPORT_IDS[s])
            else:
                _warn(f"unknown sport {s!r}, skipping")

        jobs = [(sid, day) for sid in sport_ids for day in days]
        results: Dict[str, List[Event]] = {}

        def work(sid: int, day: int):
            key = f"f_{sid}_{day}_{self.tz}_{self.lang}_{self.project_id}"
            raw = self.fetch_feed(key)
            return sid, self.parse_schedule_feed(raw, sid)

        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futs = {ex.submit(work, sid, day): (sid, day) for sid, day in jobs}
            for fut in as_completed(futs):
                sid, day = futs[fut]
                try:
                    sid, evs = fut.result()
                except Exception as e:
                    _warn(f"sport {sid} day {day} failed: {e}")
                    continue
                name = SPORT_NAMES.get(sid, str(sid))
                results.setdefault(name, []).extend(evs)

        if not include_raw:
            for evs in results.values():
                for e in evs:
                    e.raw = {}
        # stable order
        for name in results:
            results[name].sort(key=lambda e: (e.start_ts or 0, e.tournament))
        return results

    # Sub-feed namespace for event detail. g_<sport>_<id> only returns a manifest
    def get_participant_events(self, sport_id: int, day: int = 0) -> List[ParticipantEvent]:
        """Parse a field-of-competitors sport (motorsport 32, cycling 34, horse
        racing 35, ...) into events each holding many participant rows.

        For horse racing the title is "COUNTRY: VENUE: Race N - desc" and many
        races share a meeting_id (ZEE) — the venue/race_label split lets the UI
        group races under a venue with per-race start times. start_ts comes from
        the first '|'-field of ZN.
        """
        key = f"f_{sport_id}_{day}_{self.tz}_{self.lang}_{self.project_id}"
        raw = self.fetch_feed(key)
        events: List[ParticipantEvent] = []
        cur: Optional[ParticipantEvent] = None
        for rec in raw.split(REC_SEP):
            d = self._parse_record(rec)
            if not d:
                continue
            if "ZA" in d:
                title = d.get("ZA", "")
                parts = title.split(": ")
                venue = race_label = None
                if len(parts) >= 3:  # COUNTRY: VENUE: Race N - desc
                    venue, race_label = parts[1], ": ".join(parts[2:])
                zn = (d.get("ZN") or "").split("|")
                start_ts = int(zn[0]) if zn and zn[0].isdigit() else None
                if sport_id == SPORT_IDS["horse_racing"]:
                    info = _format_race_info(zn)  # distance · conditions · prize
                else:
                    info = next((p for p in zn[2:] if p and not p.isdigit()), None)
                cur = ParticipantEvent(
                    title=title, country=d.get("ZY"), venue=venue,
                    race_label=race_label, url=d.get("ZL"), meeting_id=d.get("ZEE"),
                    start_ts=start_ts, stage="", info=info,
                )
                events.append(cur)
            elif "AA" in d and cur is not None:
                cur.entries.append(d)
                if not cur.stage:
                    cur.stage = STAGE_TYPES.get(d.get("AB", ""), "")
        return events

    def get_golf_leaderboards(self, day: int = 0) -> List[GolfLeaderboard]:
        """Parse the golf feed into per-tournament leaderboards.

        Golf records aren't head-to-head: each row is a player with a position
        (CX), score-to-par (AG), and holes-thru (GH). A not-yet-started player
        has a tee-time timestamp in GH instead of a hole count and a null AG.
        Tournaments are returned live-first, each with entries sorted by
        position then score.
        """
        key = f"f_{SPORT_IDS['golf']}_{day}_{self.tz}_{self.lang}_{self.project_id}"
        raw = self.fetch_feed(key)
        boards: List[GolfLeaderboard] = []
        cur: Optional[GolfLeaderboard] = None
        for rec in raw.split(REC_SEP):
            d = self._parse_record(rec)
            if not d:
                continue
            if "ZA" in d:
                cur = GolfLeaderboard(
                    tournament=d.get("ZA", ""),
                    country=d.get("ZY"),
                    url=d.get("ZL"),
                    stage="",
                )
                boards.append(cur)
            elif "AA" in d and cur is not None:
                gh = d.get("GH", "")
                thru = tee = None
                started = False
                if gh.isdigit():
                    g = int(gh)
                    if g <= 100:           # holes completed this round
                        thru, started = g, True
                    else:                   # large value == tee-time timestamp
                        tee = g
                cur.entries.append(GolfEntry(
                    position=d.get("CX"),
                    player=d.get("AE"),
                    country=d.get("CC") or d.get("FU"),
                    to_par=d.get("AG"),
                    thru=thru,
                    tee_ts=tee,
                    started=started,
                    event_id=d.get("AA"),
                ))
                if not cur.stage:
                    cur.stage = STAGE_TYPES.get(d.get("AB", ""), "")

        def pos_key(e: GolfEntry):
            p = e.position
            return (int(p) if p and p.isdigit() else 9999, e.player or "")
        for b in boards:
            b.entries.sort(key=pos_key)
        boards.sort(key=lambda b: (0 if b.is_live else 1, b.tournament))
        return boards

    # of content hashes; the real data lives in these per-aspect feeds.
    EVENT_SUBFEEDS = {
        "summary": "df_sur_{sport}_{id}",     # period / set scores
        "scoreboard": "dc_{sport}_{id}",      # live scoreboard + result
        "head_to_head": "df_hh_{sport}_{id}", # recent form + h2h history
        "stats": "df_st_{sport}_1_{id}",      # match statistics (when available)
    }

    def get_event_detail(
        self, sport_id: int, event_id: str, aspects: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Fetch event detail by pulling the relevant sub-feeds in parallel.

        aspects: subset of EVENT_SUBFEEDS keys; default = all.
        Returns {aspect: [record_dict, ...]} for each sub-feed that had data,
        plus 'manifest' (the raw g_ feed content-hash map).
        """
        aspects = aspects or list(self.EVENT_SUBFEEDS)
        out: Dict[str, Any] = {}

        def pull(aspect: str):
            key = self.EVENT_SUBFEEDS[aspect].format(sport=sport_id, id=event_id)
            raw = self.fetch_feed(key)
            if not raw or raw.strip() == "0":
                return aspect, None
            recs = [self._parse_record(r) for r in raw.split(REC_SEP)]
            return aspect, [r for r in recs if r]

        jobs = list(aspects) + ["__manifest__"]

        def work(job: str):
            if job == "__manifest__":
                raw = self.fetch_feed(f"g_{sport_id}_{event_id}")
                merged: Dict[str, str] = {}
                for rec in raw.split(REC_SEP):
                    merged.update(self._parse_record(rec))
                return "manifest", merged or None
            return pull(job)

        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            for fut in as_completed([ex.submit(work, j) for j in jobs]):
                try:
                    name, data = fut.result()
                except Exception as e:
                    _warn(f"event detail sub-feed failed: {e}")
                    continue
                if data is not None:
                    out[name] = data
        return out

    # -- granular live state ------------------------------------------------
    def get_live_detail(self, sport_id: int, event_id: str) -> Dict[str, Any]:
        """Fetch the granular in-play state for one event.

        Pulls the scoreboard feed (dc_) and the period/summary feed (df_sur) and
        decodes the well-understood fields into a structured `decoded` block; the
        raw `scoreboard` and `summary` records are always included so callers can
        reach anything not decoded here.

        Scoreboard (dc_) common fields:
          DE/DF = current TOTAL score (home/away)   [score sports]
          DP/DQ = current GAME points (home/away)   [tennis: 0/15/30/40/AD]
          DN/DO = games in current set (home/away)  [tennis]
          DL    = current period/set number
          DI    = game clock (sport-dependent; -1 when N/A)
          DB    = detailed status code
          DX    = comma list of available detail tabs (ST, HH, MH, PI, ...)
        Summary (df_sur):
          first record BA/BB = current period score (or total for tennis)
          subsequent B-pairs = per-period scores in order (Q1, Q2, ... / sets)
          baseball situation record: WF/WG/WH/WI (balls/strikes/outs/bases family)
        """
        out: Dict[str, Any] = {"sport_id": sport_id, "event_id": event_id}

        def pull(key):
            raw = self.fetch_feed(key)
            if not raw or raw.strip() == "0":
                return []
            return [d for d in (self._parse_record(r) for r in raw.split(REC_SEP)) if d]

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_sb = ex.submit(pull, f"dc_{sport_id}_{event_id}")
            f_sum = ex.submit(pull, f"df_sur_{sport_id}_{event_id}")
            sb_recs = f_sb.result()
            summary = f_sum.result()

        scoreboard = sb_recs[0] if sb_recs else {}
        out["scoreboard"] = scoreboard
        out["summary"] = summary
        out["available_tabs"] = [t for t in scoreboard.get("DX", "").split(",") if t]

        # ---- decode the common, reliable bits ----
        decoded: Dict[str, Any] = {}
        # tennis has no running total (DE/DF are 0); its score is sets + games.
        if sport_id != SPORT_IDS["tennis"] and scoreboard.get("DE") not in (None, ""):
            decoded["home_score"] = scoreboard.get("DE")
            decoded["away_score"] = scoreboard.get("DF")
        decoded["status_code"] = scoreboard.get("DB")
        decoded["period"] = scoreboard.get("DL")
        decoded["clock"] = scoreboard.get("DI") if scoreboard.get("DI") not in ("-1", None) else None

        # per-period breakdown: collect B?-keyed values across summary in order
        period_vals: List[str] = []
        situation: Dict[str, str] = {}
        for rec in summary:
            for k, v in rec.items():
                if len(k) == 2 and k[0] == "B" and k[1].isalpha():
                    period_vals.append(v)
                elif len(k) == 2 and k[0] == "W" and k[1].isalpha():
                    situation[k] = v
        # pair them up: (home, away), (home, away), ...
        periods = [(period_vals[i], period_vals[i + 1])
                   for i in range(0, len(period_vals) - 1, 2)]
        if periods:
            decoded["periods"] = periods  # first pair is current period / total
            # for quarter/period sports the count of period rows == current period
            # (e.g. basketball in Q4 returns current + Q1..Q3 = 4 rows)
            decoded["num_periods"] = len(periods)
        if situation:
            decoded["situation"] = situation

        # football running minute: snap the period baseline from the clock-start
        # timestamp DK, then add real time elapsed since. Handles the half/ET
        # break because DK jumps to the start of the current period.
        if sport_id == SPORT_IDS["football"]:
            dk = scoreboard.get("DK")
            kick = scoreboard.get("DC")
            if dk and dk.isdigit() and kick and kick.isdigit():
                raw_base = (int(dk) - int(kick)) / 60.0
                base = min((0, 45, 90, 105), key=lambda b: abs(b - raw_base))
                minute = base + (time.time() - int(dk)) / 60.0
                if 0 <= minute <= 130:
                    decoded["minute"] = int(minute)

        if sport_id == SPORT_IDS["tennis"]:
            # DP/DQ = current game points (reliable). Games-in-set come from the
            # df_sur period pair (decoded["periods"][0]); DL is a constant
            # (best-of) and DN/DO are unreliable, so don't use them.
            decoded["game_points"] = (scoreboard.get("DP"), scoreboard.get("DQ"))

        if sport_id == SPORT_IDS["baseball"]:
            # Flashscore baseball has no balls/strikes/outs/bases in the feed —
            # only score + inning. The inning is encoded in the status code:
            # empirically code 26 == 1st inning (confirmed: code 28 == 3rd), so
            # inning = code - 25. DR is the half: 1 = top, 2 = bottom.
            code = scoreboard.get("DB") or ""
            if code.isdigit():
                inning = int(code) - BASEBALL_INNING_BASE + 1
                if 1 <= inning <= 20:
                    decoded["inning"] = inning
                    decoded["inning_half"] = {"1": "top", "2": "bot"}.get(scoreboard.get("DR"))

        out["decoded"] = decoded
        return out

    # -- live polling -------------------------------------------------------
    def snapshot_live(self, sports: Optional[List[Any]] = None) -> List[Event]:
        """One-shot fetch of currently-live events for the given sports.

        Pulls each sport's day-0 feed (threaded) and keeps only in-play events
        (AB==2). This is the unit the poller diffs across ticks.
        """
        sched = self.get_schedule(sports=sports, days=[0], include_raw=False)
        live: List[Event] = []
        for evs in sched.values():
            live.extend(e for e in evs if e.is_live)
        return live

    @staticmethod
    def _diff_live(prev: Dict[str, Event], cur: Dict[str, Event]) -> Dict[str, Any]:
        """Compute a changeset between two {event_id: Event} live snapshots."""
        started, score_changes, ended = [], [], []
        for eid, e in cur.items():
            if eid not in prev:
                started.append(e)
            else:
                p = prev[eid]
                if (e.home_score, e.away_score) != (p.home_score, p.away_score):
                    score_changes.append(
                        {
                            "event": e,
                            "old": (p.home_score, p.away_score),
                            "new": (e.home_score, e.away_score),
                        }
                    )
        for eid, p in prev.items():
            if eid not in cur:
                ended.append(p)  # dropped out of the live set (finished/postponed)
        return {"started": started, "score_changes": score_changes, "ended": ended}

    def poll_live(
        self,
        sports: Optional[List[Any]] = None,
        interval: float = 15.0,
        on_update=None,
        on_error=None,
        stop_event: Optional[threading.Event] = None,
        max_ticks: Optional[int] = None,
    ) -> None:
        """Blocking poll loop. Each tick fetches live events for `sports`, diffs
        against the previous tick, and invokes on_update(update) with:

            {
              "tick": int, "ts": float,
              "live": [Event, ...],              # everything currently in-play
              "by_sport": {sport: [Event, ...]},
              "started": [Event, ...],           # newly in-play this tick
              "score_changes": [{event, old, new}, ...],
              "ended": [Event, ...],             # left the live set this tick
            }

        Stops when stop_event is set or max_ticks reached. Errors in a tick go to
        on_error(exc) (or are re-raised if no handler) and the loop continues.
        """
        stop_event = stop_event or threading.Event()
        prev: Dict[str, Event] = {}
        tick = 0
        while not stop_event.is_set():
            tick += 1
            t0 = time.time()
            try:
                live = self.snapshot_live(sports=sports)
                cur = {e.event_id: e for e in live}
                diff = self._diff_live(prev, cur)
                by_sport: Dict[str, List[Event]] = {}
                for e in live:
                    by_sport.setdefault(e.sport, []).append(e)
                update = {
                    "tick": tick,
                    "ts": t0,
                    "live": live,
                    "by_sport": by_sport,
                    **diff,
                }
                if on_update:
                    on_update(update)
                prev = cur
            except Exception as e:  # keep the loop alive across transient failures
                if on_error:
                    on_error(e)
                else:
                    _warn(f"poll tick {tick} failed: {e}")
            if max_ticks and tick >= max_ticks:
                break
            # interval measured from tick start, so fetch time is absorbed
            stop_event.wait(max(0.0, interval - (time.time() - t0)))

    def start_live_poller(self, sports=None, interval=15.0, on_update=None, on_error=None):
        """Non-blocking variant: spawns a daemon thread running poll_live and
        returns a controller with .stop() / .join() / .is_alive() and .stop_event.

        Suitable for driving a Qt widget — marshal on_update onto the GUI thread
        (e.g. via a pyqtSignal) since it fires from the poller thread.
        """
        stop_event = threading.Event()
        th = threading.Thread(
            target=self.poll_live,
            kwargs=dict(
                sports=sports,
                interval=interval,
                on_update=on_update,
                on_error=on_error,
                stop_event=stop_event,
            ),
            daemon=True,
            name="flashscore-live-poller",
        )
        th.start()
        return _PollerHandle(th, stop_event)


class _PollerHandle:
    def __init__(self, thread: threading.Thread, stop_event: threading.Event):
        self.thread = thread
        self.stop_event = stop_event

    def stop(self, join_timeout: float = 5.0):
        self.stop_event.set()
        self.thread.join(timeout=join_timeout)

    def is_alive(self):
        return self.thread.is_alive()

    def join(self, timeout=None):
        self.thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _events_to_jsonable(results: Dict[str, List[Event]]) -> Dict[str, List[dict]]:
    return {k: [asdict(e) for e in v] for k, v in results.items()}


def main(argv=None):
    p = argparse.ArgumentParser(description="Flashscore internal-feed client")
    p.add_argument(
        "--sports",
        nargs="+",
        help="sport names or ids (default: all). e.g. football tennis basketball",
    )
    p.add_argument(
        "--days",
        nargs="+",
        type=int,
        default=[0],
        help="day offsets (0=today, -1=yesterday, 1=tomorrow). default: 0",
    )
    p.add_argument("--tz", type=int, default=0, help="UTC hour offset bucket")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--raw", action="store_true", help="include raw feed fields per event")
    p.add_argument("--out", help="write JSON to this file instead of stdout summary")
    p.add_argument("--list-sports", action="store_true", help="print known sports and exit")
    p.add_argument("--event", nargs=2, metavar=("SPORT_ID", "EVENT_ID"),
                   help="fetch one event detail feed and dump raw fields")
    p.add_argument("--live", action="store_true",
                   help="poll live scores and print updates (Ctrl-C to stop)")
    p.add_argument("--interval", type=float, default=15.0,
                   help="live poll interval in seconds (default 15)")
    args = p.parse_args(argv)

    if args.list_sports:
        for name, sid in SPORT_IDS.items():
            print(f"{sid:>3}  {name}")
        return 0

    client = FlashscoreClient(tz=args.tz, max_workers=args.workers)

    if args.event:
        detail = client.get_event_detail(int(args.event[0]), args.event[1])
        print(json.dumps(detail, indent=2, ensure_ascii=False))
        return 0

    if args.live:
        sports = args.sports or ["football", "tennis", "basketball", "hockey"]

        def on_update(u):
            stamp = datetime.now().strftime("%H:%M:%S")
            for c in u["score_changes"]:
                e = c["event"]
                print(f"[{stamp}] GOAL  {e.sport:<10} {e.home} {c['new'][0]}-{c['new'][1]} {e.away}  "
                      f"({c['old'][0]}-{c['old'][1]} -> {c['new'][0]}-{c['new'][1]})")
            for e in u["started"]:
                print(f"[{stamp}] START {e.sport:<10} {e.home} vs {e.away}  [{e.tournament}]")
            for e in u["ended"]:
                print(f"[{stamp}] END   {e.sport:<10} {e.home} {e.home_score}-{e.away_score} {e.away}")
            live_n = len(u["live"])
            per = ", ".join(f"{s}:{len(v)}" for s, v in sorted(u["by_sport"].items()))
            print(f"[{stamp}] tick {u['tick']}: {live_n} live ({per})")

        print(f"Polling live: {', '.join(map(str, sports))} every {args.interval}s. Ctrl-C to stop.\n",
              file=sys.stderr)
        try:
            client.poll_live(sports=sports, interval=args.interval, on_update=on_update)
        except KeyboardInterrupt:
            print("\nstopped.", file=sys.stderr)
        return 0

    t0 = time.time()
    results = client.get_schedule(sports=args.sports, days=args.days, include_raw=args.raw)
    dt = time.time() - t0

    total = sum(len(v) for v in results.values())
    print(f"\nFetched {total} events across {len(results)} sports in {dt:.1f}s\n", file=sys.stderr)
    for name in sorted(results):
        print(f"  {name:<20} {len(results[name]):>5} events", file=sys.stderr)

    payload = _events_to_jsonable(results)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"\nwrote {args.out}", file=sys.stderr)
    else:
        # print a compact preview to stdout
        preview = {k: v[:3] for k, v in payload.items()}
        print(json.dumps(preview, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
