"""Tennis Abstract scraper.

Sources data from Tennis Abstract's internal static data files instead of
driving a browser:

  * Main player data  -> /jsfrags/<Name>.js   (the `player_frag` template literal
    holds the fully-rendered HTML of every table, ids identical to the live page)
  * Bio / overall Elo -> /cgi-bin/player.cgi?p=<Name>   (clean JS vars)
  * Surface Elo        -> /reports/{atp,wta}_elo_ratings.html  (current h/c/g Elo)
  * Historical matches -> /cgi-bin/player-classic.cgi?p=<Name>&f=ACareerqq
    (the `matchmx` data array; the on-page table is JS-rendered and empty)

No Selenium / webdriver required. The public interface (TennisAbstractScraper,
its dataclasses, `_scrape_player_page`, `scrape_players`, `save_data`, `close`)
is unchanged so callers need no modification.
"""
import asyncio
import concurrent.futures
import json
import pathlib
import re
import threading
import time
import html as _html
import requests
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime, date

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

BASE = "https://www.tennisabstract.com"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:124.0) "
              "Gecko/20100101 Firefox/124.0")


@dataclass
class MatchResult:
    date: str
    tournament: str
    surface: str
    round: str
    player_rank: str
    opponent_rank: str
    opponent: str
    score: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    break_points_saved: str
    match_time: str

@dataclass
class SeasonStats:
    year: str
    matches: str
    wins: str
    losses: str
    win_percentage: str
    set_record: str
    set_percentage: str
    game_record: str
    game_percentage: str
    tiebreak_record: str
    tiebreak_percentage: str
    matches_with_stats: str
    hold_percentage: str
    break_percentage: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    service_points_won: str
    return_points_won: str
    total_points_won: str
    dominance_ratio: str
    best_result: str

@dataclass
class FinalsResult:
    date: str
    tournament: str
    surface: str
    round: str
    player_rank: str
    opponent_rank: str
    opponent: str
    score: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    break_points_saved: str
    match_time: str

@dataclass
class YearEndRanking:
    year: str
    atp_rank: str
    points: str
    elo_rank: str
    elo_rating: str
    hard_elo_rank: str
    hard_elo: str
    clay_elo_rank: str
    clay_elo: str
    grass_elo_rank: str
    grass_elo: str

@dataclass
class EventResult:
    event: str
    years_entered: str
    surface: str
    matches: str
    wins: str
    losses: str
    win_percentage: str
    tiebreaks: str
    tb_wins: str
    tb_losses: str
    tb_percentage: str
    first_year: str
    last_year: str
    best_result: str
    matches_with_stats: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    service_points_won: str
    return_points_won: str
    break_points_saved_pct: str
    break_points_converted_pct: str

@dataclass
class SplitStats:
    split: str
    matches: str
    wins: str
    losses: str
    win_percentage: str
    set_record: str
    set_percentage: str
    game_record: str
    game_percentage: str
    tiebreak_record: str
    tiebreak_percentage: str
    matches_with_stats: str
    hold_percentage: str
    break_percentage: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    service_points_won: str
    return_points_won: str
    total_points_won: str
    dominance_ratio: str

@dataclass
class WinnersErrorsData:
    match: str
    result: str
    winners: str
    unforced_errors: str
    ratio: str
    winners_per_point: str
    ufe_per_point: str
    rally_winners: str
    rally_ufes: str
    rally_ratio: str
    rally_winners_per_point: str
    rally_ufe_per_point: str
    fh_winners_per_point: str
    bh_winners_per_point: str
    opponent_ratio: str

@dataclass
class ServeSpeedData:
    match: str
    first_serve_avg: str
    first_serve_max: str
    second_serve_avg: str
    second_serve_max: str

@dataclass
class TacticsData:
    match: str
    result: str
    snv_freq: str
    snv_w_pct: str
    net_freq: str
    net_w_pct: str
    fh_wnr_pct: str
    dtl_wnr_pct: str
    io_wnr_pct: str
    bh_wnr_pct: str
    dtl_wnr_pct_bh: str
    drop_freq: str
    drop_wnr_pct: str
    rally_agg: str
    return_agg: str

@dataclass
class HistoricalMatchData:
    date: str
    tournament: str
    surface: str
    round: str
    player_rank: str
    opponent_rank: str
    opponent: str
    result: str  # Win/Loss extracted from match description
    score: str
    charting_link: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    break_points_saved: str
    match_time: str
    level: str = ""  # TA level code: G/M/A/F/D/O=tour, C=challenger, S/15/25=ITF

@dataclass
class PlayerBio:
    name: str
    country: str
    age: str
    birth_date: str
    plays: str
    current_rank: str
    peak_rank: str
    peak_rank_date: str
    elo_rank: str
    elo_rating: str
    photo_url: str
    # Current surface Elo ratings (from the Tennis Abstract Elo report).
    # New fields with defaults -> existing positional construction stays valid.
    hard_elo: str = ""
    hard_elo_rank: str = ""
    clay_elo: str = ""
    clay_elo_rank: str = ""
    grass_elo: str = ""
    grass_elo_rank: str = ""
    peak_elo: str = ""

@dataclass
class PlayerData:
    player_name: str
    player_bio: PlayerBio
    recent_results: List[MatchResult]
    tour_seasons: List[SeasonStats]
    challenger_seasons: List[SeasonStats]
    recent_finals: List[FinalsResult]
    year_end_rankings: List[YearEndRanking]
    recent_events: List[EventResult]
    career_splits: List[SplitStats]
    last52_splits: List[SplitStats]
    career_splits_chall: List[SplitStats]
    last52_splits_chall: List[SplitStats]
    winners_errors: List[WinnersErrorsData]
    serve_speed: List[ServeSpeedData]
    tactics: List[TacticsData]
    historical_matches: List[HistoricalMatchData]
    scrape_timestamp: str
    source_url: str
    # Rich Match-Charting-Project / point-by-point tables, stored generically as
    # {'headers': [...], 'rows': [[cell, ...], ...]} since columns are numerous
    # and we aggregate them. New fields with defaults keep positional construction
    # valid for any existing callers.
    mcp_serve: Dict = field(default_factory=dict)
    mcp_return: Dict = field(default_factory=dict)
    mcp_rally: Dict = field(default_factory=dict)
    pbp_stats: Dict = field(default_factory=dict)
    pbp_games: Dict = field(default_factory=dict)
    pbp_points: Dict = field(default_factory=dict)
    serve_speed_detail: Dict = field(default_factory=dict)
    head_to_heads: Dict = field(default_factory=dict)


def _norm_name_key(name: str) -> str:
    """Normalise a player name for matching across sources."""
    return re.sub(r"[^a-z]", "", _html.unescape(name).lower())


class TennisAbstractScraper:
    """Fetches Tennis Abstract player data over plain HTTP (no browser)."""

    # Surface-Elo report rows shared across all instances (fetched at most once
    # per tour per process). Maps normalised name -> dict of elo fields.
    _elo_cache: Dict[str, Dict[str, Dict[str, str]]] = {}

    # The cgi-bin endpoints (player.cgi / player-classic.cgi) rate-limit hard.
    # Serialise them process-wide so two concurrent player loads don't 429.
    _cgi_lock = threading.Lock()

    def __init__(self, headless: bool = True, timeout: int = 10,
                 max_workers: int = 4, reuse_driver: bool = True):
        # headless / reuse_driver kept only for call-site compatibility.
        self.timeout = timeout
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._element_cache: Dict[str, Dict] = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #
    # Disk cache for fetched responses: a successfully-loaded player persists
    # across loads/restarts, so we stop re-hitting the rate-limited cgi-bin.
    CACHE_TTL_SECONDS = 6 * 3600

    def _cache_path(self, url: str):
        import hashlib
        key = hashlib.md5(url.encode("utf-8")).hexdigest()
        d = pathlib.Path(__file__).parent / "ta_cache"
        try:
            d.mkdir(exist_ok=True)
        except OSError:
            return None
        return d / f"{key}.txt"

    def _fetch(self, url: str, retries: int = 4) -> str:
        """GET a URL with a disk cache (TTL) + 429 backoff. cgi-bin requests are
        serialised behind a process-wide lock (they 429 under concurrent load)."""
        cp = self._cache_path(url)
        if cp is not None and cp.exists():
            if (time.time() - cp.stat().st_mtime) < self.CACHE_TTL_SECONDS:
                try:
                    return cp.read_text(encoding="utf-8")
                except OSError:
                    pass

        if "cgi-bin" in url:
            with TennisAbstractScraper._cgi_lock:
                text = self._fetch_with_backoff(url, retries)
                time.sleep(0.4)   # be polite between serialised cgi-bin hits
        else:
            text = self._fetch_with_backoff(url, retries)

        if cp is not None:
            try:
                cp.write_text(text, encoding="utf-8")
            except OSError:
                pass
        return text

    def _fetch_with_backoff(self, url: str, retries: int) -> str:
        delay = 1.0
        for attempt in range(retries):
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.text
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _player_slug(name_or_url: str) -> str:
        """Return the TennisAbstract `p=` slug (spaces removed) from a name or URL."""
        if "?p=" in name_or_url:
            return name_or_url.split("?p=")[1].split("&")[0]
        return name_or_url.replace(" ", "")

    @staticmethod
    def _js_str(text: str, var: str) -> str:
        """Extract a quoted JS string/number assignment: var X = '...' / "..." / 123."""
        for pat in (rf"var\s+{var}\s*=\s*'([^']*)'",
                    rf'var\s+{var}\s*=\s*"([^"]*)"',
                    rf"var\s+{var}\s*=\s*([0-9.]+)\s*;"):
            m = re.search(pat, text)
            if m:
                return m.group(1).strip()
        return ""

    # ------------------------------------------------------------------ #
    # Bio + overall Elo (from player.cgi JS vars) and surface Elo (report)
    # ------------------------------------------------------------------ #
    def _extract_bio(self, slug: str) -> PlayerBio:
        try:
            html = self._fetch(f"{BASE}/cgi-bin/player.cgi?p={slug}")
        except Exception as e:
            print(f"Error fetching bio page for {slug}: {e}")
            return PlayerBio("", "", "", "", "", "", "", "", "", "", "")

        name = self._js_str(html, "fullname") or slug
        country = self._js_str(html, "country")
        current_rank = self._js_str(html, "currentrank")
        peak_rank = self._js_str(html, "peakrank")
        dob = self._js_str(html, "dob")            # YYYYMMDD
        hand = self._js_str(html, "hand")          # 'R' / 'L'
        backhand = self._js_str(html, "backhand")  # '1' / '2'
        elo_rating = self._js_str(html, "elo_rating")
        elo_rank = self._js_str(html, "elo_rank")
        photog = self._js_str(html, "photog")

        age, birth_date = self._age_and_birthdate(dob)
        plays = self._format_plays(hand, backhand)

        photo_url = ""
        if name:
            slug_photo = name.lower().replace(" ", "_")
            photo_url = f"{BASE}/photos/{slug_photo}-{photog}.jpg" if photog else ""

        bio = PlayerBio(
            name=name, country=country, age=age, birth_date=birth_date,
            plays=plays, current_rank=current_rank, peak_rank=peak_rank,
            peak_rank_date="", elo_rank=elo_rank, elo_rating=elo_rating,
            photo_url=photo_url,
        )

        # Layer current surface Elo on top.
        surf = self._lookup_surface_elo(name)
        if surf:
            bio.hard_elo = surf.get("hard_elo", "")
            bio.hard_elo_rank = surf.get("hard_elo_rank", "")
            bio.clay_elo = surf.get("clay_elo", "")
            bio.clay_elo_rank = surf.get("clay_elo_rank", "")
            bio.grass_elo = surf.get("grass_elo", "")
            bio.grass_elo_rank = surf.get("grass_elo_rank", "")
            bio.peak_elo = surf.get("peak_elo", "")
            # Backfill overall Elo from the report if the bio page lacked it.
            if not bio.elo_rating and surf.get("elo"):
                bio.elo_rating = surf["elo"]
            if not bio.elo_rank and surf.get("elo_rank"):
                bio.elo_rank = surf["elo_rank"]
        return bio

    @staticmethod
    def _age_and_birthdate(dob: str) -> Tuple[str, str]:
        """dob 'YYYYMMDD' -> ('26', 'YYYY-MM-DD')."""
        if not (dob and len(dob) == 8 and dob.isdigit()):
            return "", ""
        y, m, d = int(dob[:4]), int(dob[4:6]), int(dob[6:8])
        try:
            bd = date(y, m, d)
        except ValueError:
            return "", f"{dob[:4]}-{dob[4:6]}-{dob[6:8]}"
        today = date.today()
        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        return str(age), bd.isoformat()

    @staticmethod
    def _format_plays(hand: str, backhand: str) -> str:
        """Build a 'plays' string the downstream abbreviator understands."""
        parts = []
        if hand == "R":
            parts.append("Right-Handed")
        elif hand == "L":
            parts.append("Left-Handed")
        if backhand == "2":
            parts.append("Two-Handed Backhand")
        elif backhand == "1":
            parts.append("One-Handed Backhand")
        return ", ".join(parts)

    def _lookup_surface_elo(self, name: str) -> Optional[Dict[str, str]]:
        key = _norm_name_key(name)
        for tour in ("atp", "wta"):
            table = self._elo_report(tour)
            if key in table:
                return table[key]
        return None

    def _elo_report(self, tour: str) -> Dict[str, Dict[str, str]]:
        """Fetch & parse the current Elo ratings report once per tour."""
        if tour in TennisAbstractScraper._elo_cache:
            return TennisAbstractScraper._elo_cache[tour]

        table: Dict[str, Dict[str, str]] = {}
        try:
            html = self._fetch(f"{BASE}/reports/{tour}_elo_ratings.html")
        except Exception as e:
            print(f"Error fetching {tour} Elo report: {e}")
            TennisAbstractScraper._elo_cache[tour] = table
            return table

        def clean(s: str) -> str:
            return _html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").strip()

        # Columns: 0 eloRank, 1 player, 2 age, 3 elo, 4 spacer,
        # 5 hEloRank, 6 hElo, 7 cEloRank, 8 cElo, 9 gEloRank, 10 gElo,
        # 11 spacer, 12 peakElo, 13 peakMonth, 14 spacer, 15 atpRank
        for tr in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
            if "player.cgi" not in tr:
                continue
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
            if len(tds) < 16:
                continue
            vals = [clean(td) for td in tds]
            if not vals[0].isdigit():     # skip the wrapping layout row
                continue
            table[_norm_name_key(vals[1])] = {
                "elo_rank": vals[0],
                "elo": vals[3],
                "hard_elo_rank": vals[5],
                "hard_elo": vals[6],
                "clay_elo_rank": vals[7],
                "clay_elo": vals[8],
                "grass_elo_rank": vals[9],
                "grass_elo": vals[10],
                "peak_elo": vals[12],
            }
        TennisAbstractScraper._elo_cache[tour] = table
        return table

    # ------------------------------------------------------------------ #
    # Main player tables (from jsfrags player_frag fragment)
    # ------------------------------------------------------------------ #
    def _get_cached_data(self, slug: str) -> Dict:
        """Fetch jsfrags/<slug>.js, pull the player_frag HTML, parse all tables."""
        if slug in self._element_cache:
            return self._element_cache[slug]

        data: Dict = {}
        try:
            js = self._fetch(f"{BASE}/jsfrags/{slug}.js")
        except Exception as e:
            print(f"Error fetching jsfrags for {slug}: {e}")
            js = ""

        frag = ""
        m = re.search(r"var\s+player_frag\s*=\s*`(.*?)`;", js, re.DOTALL)
        if m:
            frag = m.group(1)

        soup = BeautifulSoup(frag, "html.parser") if (frag and BeautifulSoup) else None

        def tbl(table_id):
            if not soup:
                return []
            return self._scrape_table_from_soup(soup.find(id=table_id), table_id)

        data["recent-results"] = tbl("recent-results")
        data["tour-years"] = tbl("tour-years")
        data["chall-years"] = tbl("chall-years")
        data["recent-finals"] = tbl("recent-finals")
        data["year-end-rankings"] = tbl("year-end-rankings")
        data["recent-events"] = tbl("recent-events")
        data["career-splits"] = tbl("career-splits")
        data["last52-splits"] = tbl("last52-splits")
        data["career-splits-chall"] = tbl("career-splits-chall")
        data["last52-splits-chall"] = tbl("last52-splits-chall")
        data["winners-errors"] = tbl("winners-errors")
        data["serve-speed"] = tbl("serve-speed")
        data["mcp-tactics"] = tbl("mcp-tactics")

        # Rich Match-Charting / point-by-point tables (headers+rows, aggregated
        # downstream). These carry duplicate column names per serve/return split
        # so a header-keyed dict would collide -> keep headers + raw rows.
        for tid in ("mcp-serve", "mcp-return", "mcp-rally",
                    "pbp-stats", "pbp-games", "pbp-points",
                    "serve-speed", "head-to-heads"):
            data[f"generic:{tid}"] = self._scrape_generic_table(soup, tid)

        self._element_cache[slug] = data
        return data

    def _scrape_generic_table(self, soup, table_id) -> Dict:
        """Return {'headers': [...], 'rows': [[cell, ...], ...]} for a table,
        preserving column order (duplicate header names are common)."""
        if not soup:
            return {"headers": [], "rows": []}
        t = soup.find(id=table_id)
        if not t:
            return {"headers": [], "rows": []}
        trs = t.find_all("tr")
        if len(trs) < 2:
            return {"headers": [], "rows": []}

        def cells(tr):
            return [c.get_text(strip=True).replace("\xa0", " ")
                    for c in tr.find_all(["th", "td"])]

        headers = cells(trs[0])
        rows = []
        for tr in trs[1:]:
            c = cells(tr)
            if c and any(v for v in c):
                rows.append(c)
        return {"headers": headers, "rows": rows}

    def _scrape_table_from_soup(self, table_element, table_type):
        """Generic table scraper that works with BeautifulSoup elements"""
        if not table_element:
            return []

        try:
            tbody = table_element.find('tbody')
            if not tbody:
                return []

            rows = tbody.find_all('tr')
            if not rows:
                return []

            # Convert BeautifulSoup rows to text data and use existing scrapers
            row_data = []
            for row in rows:
                cells = row.find_all(['td', 'th'])
                cell_texts = [cell.get_text(strip=True) for cell in cells]
                row_data.append(cell_texts)

            # Use existing scraping logic based on table type
            if table_type in ["recent-results"]:
                return self._process_match_results(row_data)
            elif table_type in ["tour-years", "chall-years"]:
                return self._process_season_stats(row_data)
            elif table_type in ["recent-finals"]:
                return self._process_finals_results(row_data)
            elif table_type in ["year-end-rankings"]:
                return self._process_year_end_rankings(row_data)
            elif table_type in ["recent-events"]:
                return self._process_recent_events(row_data)
            elif table_type in ["career-splits", "last52-splits", "career-splits-chall", "last52-splits-chall"]:
                return self._process_split_stats(row_data)
            elif table_type in ["winners-errors"]:
                return self._process_winners_errors(row_data)
            elif table_type in ["serve-speed"]:
                return self._process_serve_speed(row_data)
            elif table_type in ["mcp-tactics"]:
                return self._process_tactics(row_data)
            else:
                return []

        except Exception:
            return []

    def _process_match_results(self, row_data):
        """Process match results from text data"""
        results = []
        for row_texts in row_data:
            if len(row_texts) >= 16:
                try:
                    result = MatchResult(*row_texts[:16])
                    results.append(result)
                except Exception:
                    continue
        return results

    def _process_season_stats(self, row_data):
        """Process season stats from text data"""
        stats = []
        for row_texts in row_data:
            if len(row_texts) >= 23:
                try:
                    season = SeasonStats(
                        year=row_texts[0],
                        matches=row_texts[1],
                        wins=row_texts[2],
                        losses=row_texts[3],
                        win_percentage=row_texts[4],
                        set_record=row_texts[5],
                        set_percentage=row_texts[6],
                        game_record=row_texts[7],
                        game_percentage=row_texts[8],
                        tiebreak_record=row_texts[9],
                        tiebreak_percentage=row_texts[10],
                        matches_with_stats=row_texts[11],
                        hold_percentage=row_texts[12],
                        break_percentage=row_texts[13],
                        ace_rate=row_texts[14],
                        double_fault_rate=row_texts[15],
                        first_serve_in=row_texts[16],
                        first_serve_won=row_texts[17],
                        second_serve_won=row_texts[18],
                        service_points_won=row_texts[19],
                        return_points_won=row_texts[20],
                        total_points_won=row_texts[21],
                        dominance_ratio=row_texts[22],
                        best_result=row_texts[23] if len(row_texts) > 23 else ""
                    )
                    stats.append(season)
                except Exception:
                    continue
        return stats

    def _process_finals_results(self, row_data):
        """Process finals results from text data"""
        results = []
        for row_texts in row_data:
            if len(row_texts) >= 16:
                try:
                    result = FinalsResult(*row_texts[:16])
                    results.append(result)
                except Exception:
                    continue
        return results

    def _process_year_end_rankings(self, row_data):
        """Process year end rankings from text data"""
        rankings = []
        for row_texts in row_data:
            if len(row_texts) >= 11:
                try:
                    ranking = YearEndRanking(*row_texts[:11])
                    rankings.append(ranking)
                except Exception:
                    continue
        return rankings

    def _process_recent_events(self, row_data):
        """Process recent events from text data"""
        events = []
        for row_texts in row_data:
            if len(row_texts) >= 25:
                try:
                    event = EventResult(*row_texts[:25])
                    events.append(event)
                except Exception:
                    continue
        return events

    def _process_split_stats(self, row_data):
        """Process split stats from text data"""
        splits = []
        for row_texts in row_data:
            if len(row_texts) >= 22:
                try:
                    split = SplitStats(*row_texts[:22], row_texts[22] if len(row_texts) > 22 else "")
                    splits.append(split)
                except Exception:
                    continue
        return splits

    def _process_winners_errors(self, row_data):
        """Process winners/errors from text data"""
        data = []
        for row_texts in row_data:
            if len(row_texts) >= 15:
                try:
                    winners_errors = WinnersErrorsData(*row_texts[:15])
                    data.append(winners_errors)
                except Exception:
                    continue
        return data

    def _process_serve_speed(self, row_data):
        """Process serve speed from text data"""
        data = []
        for row_texts in row_data:
            if len(row_texts) >= 5:
                try:
                    serve_speed = ServeSpeedData(*row_texts[:5])
                    data.append(serve_speed)
                except Exception:
                    continue
        return data

    def _process_tactics(self, row_data):
        """Process tactics from text data"""
        data = []
        for row_texts in row_data:
            if len(row_texts) >= 15:
                try:
                    tactics = TacticsData(*row_texts[:15])
                    data.append(tactics)
                except Exception:
                    continue
        return data

    # ------------------------------------------------------------------ #
    # Historical matches (from matchmx data array on player-classic page)
    # ------------------------------------------------------------------ #
    # matchmx column indices. Derived from Tennis Abstract's own `matchhead`
    # array on the player-classic page, so the OPPONENT serve block (needed for
    # dominance ratio) and the break-point columns are reliably positioned too:
    #   ...games(27) saved(28) chances(29) oaces(30) odfs(31) opts(32)
    #   ofirsts(33) ofwon(34) oswon(35) ogames(36) ...
    _MX = dict(date=0, tourn=1, surf=2, level=3, wl=4, rank=5, round=8, score=9,
               opp=11, orank=12, time=20, aces=21, dfs=22, pts=23,
               firsts=24, fwon=25, swon=26, saved=28, chances=29,
               opts=32, ofwon=34, oswon=35)

    def _scrape_historical_matches(self, slug: str) -> List[HistoricalMatchData]:
        try:
            html = self._fetch(f"{BASE}/cgi-bin/player-classic.cgi?p={slug}&f=ACareerqq")
        except Exception as e:
            print(f"Error fetching historical matches for {slug}: {e}")
            return []

        m = re.search(r"var\s+matchmx\s*=\s*(\[.*?\]);", html, re.DOTALL)
        if not m:
            return []
        try:
            rows = json.loads(m.group(1))
        except Exception as e:
            print(f"Error parsing matchmx for {slug}: {e}")
            return []

        out: List[HistoricalMatchData] = []
        c = self._MX
        for r in rows:
            if len(r) <= c["swon"]:
                continue
            wl = r[c["wl"]]
            if wl not in ("W", "L"):     # skip upcoming ('U') / walkovers
                continue
            date_str = r[c["date"]]
            opp = r[c["opp"]]
            if not (date_str and opp):
                continue

            ace = df = f_in = f_won = s_won = dr = ""
            try:
                pts = float(r[c["pts"]] or 0)
                firsts = float(r[c["firsts"]] or 0)
                if pts > 0:
                    ace = f"{float(r[c['aces']] or 0) / pts * 100:.1f}%"
                    df = f"{float(r[c['dfs']] or 0) / pts * 100:.1f}%"
                    f_in = f"{firsts / pts * 100:.0f}%"
                    if firsts > 0:
                        f_won = f"{float(r[c['fwon']] or 0) / firsts * 100:.0f}%"
                    seconds = pts - firsts
                    if seconds > 0:
                        s_won = f"{float(r[c['swon']] or 0) / seconds * 100:.0f}%"
                    # Dominance ratio = (return points won) / (serve points lost),
                    # matching TA's own formula: rpw/spl. Needs the opponent
                    # serve block, which the matchhead map now gives us.
                    opts = float(r[c["opts"]] or 0) if len(r) > c["opts"] else 0
                    if opts > 0 and len(r) > c["oswon"]:
                        spl = 1 - (float(r[c["fwon"]] or 0) + float(r[c["swon"]] or 0)) / pts
                        rpw = 1 - (float(r[c["ofwon"]] or 0) + float(r[c["oswon"]] or 0)) / opts
                        if spl > 0:
                            dr = f"{rpw / spl:.2f}"
            except (ValueError, TypeError):
                pass

            # Break points saved, kept as a "saved/faced" fraction (the widget
            # parses either a fraction or a percentage).
            bps = ""
            if len(r) > c["chances"]:
                saved, chances = r[c["saved"]], r[c["chances"]]
                if str(chances).strip() not in ("", "0"):
                    bps = f"{saved}/{chances}"

            out.append(HistoricalMatchData(
                date=date_str,
                tournament=r[c["tourn"]],
                surface=r[c["surf"]],
                round=r[c["round"]],
                player_rank=r[c["rank"]],
                opponent_rank=r[c["orank"]],
                opponent=opp,
                result="Win" if wl == "W" else "Loss",
                score=r[c["score"]],
                charting_link="",
                level=r[c["level"]] if len(r) > c["level"] else "",
                dominance_ratio=dr,
                ace_rate=ace,
                double_fault_rate=df,
                first_serve_in=f_in,
                first_serve_won=f_won,
                second_serve_won=s_won,
                break_points_saved=bps,
                match_time=r[c["time"]],
            ))
        return out

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def _scrape_player_page(self, url: str) -> Optional[PlayerData]:
        """Scrape a single player using the static data endpoints."""
        slug = self._player_slug(url)
        try:
            print(f"Scraping: {slug}")
            bio = self._extract_bio(slug)
            main = self._get_cached_data(slug)
            historical_matches = self._scrape_historical_matches(slug)

            player_name = bio.name or slug

            player_data = PlayerData(
                player_name=player_name,
                player_bio=bio,
                recent_results=main["recent-results"],
                tour_seasons=main["tour-years"],
                challenger_seasons=main["chall-years"],
                recent_finals=main["recent-finals"],
                year_end_rankings=main["year-end-rankings"],
                recent_events=main["recent-events"],
                career_splits=main["career-splits"],
                last52_splits=main["last52-splits"],
                career_splits_chall=main["career-splits-chall"],
                last52_splits_chall=main["last52-splits-chall"],
                winners_errors=main["winners-errors"],
                serve_speed=main["serve-speed"],
                tactics=main["mcp-tactics"],
                historical_matches=historical_matches,
                scrape_timestamp=datetime.now().isoformat(),
                source_url=f"{BASE}/cgi-bin/player.cgi?p={slug}",
                mcp_serve=main.get("generic:mcp-serve", {}),
                mcp_return=main.get("generic:mcp-return", {}),
                mcp_rally=main.get("generic:mcp-rally", {}),
                pbp_stats=main.get("generic:pbp-stats", {}),
                pbp_games=main.get("generic:pbp-games", {}),
                pbp_points=main.get("generic:pbp-points", {}),
                serve_speed_detail=main.get("generic:serve-speed", {}),
                head_to_heads=main.get("generic:head-to-heads", {}),
            )

            print(f"Successfully scraped {player_name}: "
                  f"{len(player_data.recent_results)} recent results, "
                  f"{len(player_data.tour_seasons)} tour seasons, "
                  f"{len(player_data.challenger_seasons)} challenger seasons, "
                  f"{len(player_data.recent_finals)} finals, "
                  f"{len(player_data.year_end_rankings)} year rankings, "
                  f"{len(player_data.recent_events)} events, "
                  f"{len(player_data.career_splits)} career splits, "
                  f"{len(player_data.last52_splits)} recent splits, "
                  f"{len(player_data.winners_errors)} winner/error matches, "
                  f"{len(player_data.serve_speed)} serve speed matches, "
                  f"{len(player_data.tactics)} tactics matches, "
                  f"{len(historical_matches)} historical matches | "
                  f"Elo {bio.elo_rating} (H {bio.hard_elo} / C {bio.clay_elo} / G {bio.grass_elo})")

            return player_data

        except Exception as e:
            print(f"Error scraping {slug}: {e}")
            return None

    async def scrape_players_async(self, urls: List[str]) -> Dict[str, PlayerData]:
        """Asynchronously scrape multiple player pages"""
        loop = asyncio.get_event_loop()
        futures = [
            loop.run_in_executor(self.executor, self._scrape_player_page, url)
            for url in urls
        ]
        results = await asyncio.gather(*futures, return_exceptions=True)

        scraped_data = {}
        for url, result in zip(urls, results):
            if isinstance(result, PlayerData):
                scraped_data[url] = result
            elif isinstance(result, Exception):
                print(f"Exception for {url}: {result}")
            else:
                print(f"No data returned for {url}")
        return scraped_data

    def scrape_players(self, urls: List[str]) -> Dict[str, PlayerData]:
        """Synchronous wrapper for scraping multiple players"""
        return asyncio.run(self.scrape_players_async(urls))

    def save_data(self, data: Dict[str, PlayerData], filename: str = None):
        """Save scraped data to JSON file"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"tennis_abstract_data_{timestamp}.json"

        json_data = {}
        for url, player_data in data.items():
            json_data[url] = asdict(player_data)

        save_path = pathlib.Path(__file__).parent / filename
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)

        print(f"Data saved to {save_path}")

    def _extract_player_name_from_url(self, url: str) -> str:
        """Extract player name (slug) from URL for compatibility."""
        return self._player_slug(url)

    def close(self):
        """Clean up resources"""
        self._element_cache.clear()
        try:
            self.session.close()
        except Exception:
            pass
        self.executor.shutdown(wait=True)


# Example usage and testing
def main():
    test_urls = [
        "https://www.tennisabstract.com/cgi-bin/player.cgi?p=JannikSinner"
    ]

    scraper = TennisAbstractScraper(headless=True)
    try:
        data = scraper.scrape_players(test_urls)
        scraper.save_data(data)

        print(f"\nScraping complete! Scraped {len(data)} players:")
        for url, player_data in data.items():
            print(f"- {player_data.player_name}: {len(player_data.recent_results)} recent matches, "
                  f"{len(player_data.tour_seasons) + len(player_data.challenger_seasons)} total seasons, "
                  f"{len(player_data.recent_events)} events tracked")
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
