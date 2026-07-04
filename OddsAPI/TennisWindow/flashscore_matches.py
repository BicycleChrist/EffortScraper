"""Cached Flashscore match-stats access for the Tennis window.

A thin data layer over `flashscore_client.FlashscoreClient` that:
  * resolves a player name -> Flashscore participant id (cached in the DB),
  * lists a player's recent completed singles matches (short in-memory TTL),
  * pulls the full per-match detail (statistics + point-by-point + set summary)
    and **persists it permanently for finished matches** so we never refetch a
    completed match's numbers again.

"Be careful" (per the caching requirement): only *finished* matches are written
to the detail cache — an in-progress match's stats are volatile and must never be
frozen in the DB. The results list itself is cheap (one request) and mutates as
new matches finish, so it is only memoised in-process, not persisted.

flashscore_client lives one directory up (OddsAPI/); we add it to sys.path.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import sqlite3
import threading
from typing import Any, Dict, List, Optional

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import flashscore_client as fsc

_RESULTS_TTL = 1800.0          # in-process results-list cache (30 min)
_PARTICIPANT_TTL = 30 * 86400  # re-resolve a name at most monthly


def _name_key(name: str) -> str:
    """Normalise a display name to a stable match key (lowercase word set)."""
    s = re.sub(r"[^a-z ]", " ", (name or "").lower())
    return " ".join(sorted(t for t in s.split() if t))


def _surname(name: str) -> str:
    toks = re.sub(r"[^A-Za-z ]", " ", name or "").split()
    return toks[-1].lower() if toks else ""


class FlashscoreMatchStore:
    """Player-history + per-match-stat access with a SQLite cache."""

    def __init__(self, db_path: str = "tennis_rankings.db"):
        self.db_path = db_path
        self._client: Optional[fsc.FlashscoreClient] = None
        self._client_lock = threading.Lock()
        self._results_cache: Dict[str, Any] = {}   # player_id -> (ts, matches)
        self._ensure_schema()

    # -- client (lazy; credential discovery does a network hit) -------------
    def client(self) -> fsc.FlashscoreClient:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = fsc.FlashscoreClient(verbose=False)
        return self._client

    # -- schema -------------------------------------------------------------
    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure_schema(self):
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS fs_participant (
                    name_key   TEXT PRIMARY KEY,
                    player_id  TEXT,
                    slug       TEXT,
                    display    TEXT,
                    gender     TEXT,
                    country    TEXT,
                    fetched_ts INTEGER
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS fs_match_detail (
                    event_id   TEXT PRIMARY KEY,
                    stats_json TEXT,
                    pbp_json   TEXT,
                    setsum_json TEXT,
                    fetched_ts INTEGER
                )""")

    # -- player resolution --------------------------------------------------
    def resolve_player(self, name: str,
                       gender_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Name -> {player_id, slug, display, gender, country}, cached in the DB.

        gender_hint: 'Men' or 'Women' to disambiguate (ATP vs WTA)."""
        key = _name_key(name)
        if not key:
            return None
        now = time.time()
        with self._connect() as c:
            row = c.execute(
                "SELECT player_id, slug, display, gender, country, fetched_ts "
                "FROM fs_participant WHERE name_key=?", (key,)).fetchone()
        if row and row[0] and (now - (row[5] or 0)) < _PARTICIPANT_TTL:
            return {"player_id": row[0], "slug": row[1], "display": row[2],
                    "gender": row[3], "country": row[4]}

        hits = self.client().search_participant(name, sport_id=2, limit=15)
        best = self._best_participant(name, hits, gender_hint)
        if not best:
            return None
        rec = {"player_id": best["id"], "slug": best["slug"],
               "display": best["name"], "gender": best.get("gender"),
               "country": best.get("country")}
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO fs_participant "
                "(name_key, player_id, slug, display, gender, country, fetched_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, rec["player_id"], rec["slug"], rec["display"],
                 rec["gender"], rec["country"], int(now)))
        return rec

    @staticmethod
    def _best_participant(name: str, hits: List[Dict[str, Any]],
                          gender_hint: Optional[str]) -> Optional[Dict[str, Any]]:
        want_tokens = set(_name_key(name).split())
        # The app names players "First Last"; Flashscore returns "Last First".
        # Match on token membership, not position: require the surname token to
        # appear somewhere in the hit's name, then rank by token overlap.
        want_surname = _surname(name)
        best, best_score = None, -1.0
        for h in hits:
            if h.get("type") != "Player":
                continue
            htoks = set(_name_key(h.get("name", "")).split())
            if want_surname and want_surname not in htoks:
                continue
            overlap = len(want_tokens & htoks) / max(1, len(want_tokens))
            score = overlap
            if gender_hint and h.get("gender") == gender_hint:
                score += 0.5
            if score > best_score:
                best, best_score = h, score
        return best

    # -- results list (per-player recent matches) ---------------------------
    def get_recent_matches(self, name: str, months: int = 6,
                           gender_hint: Optional[str] = None) -> List[Dict[str, Any]]:
        """Recent completed singles matches for `name`, newest first, limited to
        roughly the last `months`. Empty list if the player can't be resolved."""
        p = self.resolve_player(name, gender_hint)
        if not p:
            return []
        pid = p["player_id"]
        now = time.time()
        cached = self._results_cache.get(pid)
        if cached and (now - cached[0]) < _RESULTS_TTL:
            matches = cached[1]
        else:
            matches = self.client().get_player_results(p["slug"], pid)
            self._results_cache[pid] = (now, matches)
        cutoff = now - months * 31 * 86400
        out = [m for m in matches
               if m.get("finished") and (m.get("start_ts") or 0) >= cutoff]
        # tag with the resolved player's id so the UI can orient scores
        for m in out:
            m["_player_id"] = pid
        return out

    # -- per-match detail (persisted for finished matches) ------------------
    def get_match_detail(self, event_id: str,
                         finished: bool = True) -> Dict[str, Any]:
        """Full detail for one match: {'stats': structured, 'pbp': [...],
        'set_summary': [...]}. Served from the DB cache when present; otherwise
        fetched and (for finished matches) persisted."""
        with self._connect() as c:
            row = c.execute(
                "SELECT stats_json, pbp_json, setsum_json FROM fs_match_detail "
                "WHERE event_id=?", (event_id,)).fetchone()
        if row:
            return {"stats": json.loads(row[0]) if row[0] else {},
                    "pbp": json.loads(row[1]) if row[1] else [],
                    "set_summary": json.loads(row[2]) if row[2] else [],
                    "cached": True}

        det = self.client().get_event_detail(
            2, event_id, ["stats", "point_by_point", "set_summary"])
        stats = fsc.FlashscoreClient.parse_tennis_stats(det.get("stats", []) or [])
        pbp = det.get("point_by_point", []) or []
        setsum = det.get("set_summary", []) or []
        out = {"stats": stats, "pbp": pbp, "set_summary": setsum, "cached": False}

        # Only freeze *finished* matches; volatile in-play stats must not persist.
        if finished and (stats or pbp or setsum):
            with self._connect() as c:
                c.execute(
                    "INSERT OR REPLACE INTO fs_match_detail "
                    "(event_id, stats_json, pbp_json, setsum_json, fetched_ts) "
                    "VALUES (?,?,?,?,?)",
                    (event_id, json.dumps(stats), json.dumps(pbp),
                     json.dumps(setsum), int(time.time())))
        return out
