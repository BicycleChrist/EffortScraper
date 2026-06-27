"""
Kalshi API Client
-----------------
Client for interacting with Kalshi's prediction markets API.
Supports fetching events, markets, and historical candlestick data.
"""

import requests
import hashlib
import json
import orjson
import os
import concurrent.futures as cf
import threading
import traceback
import time
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Callable, Iterable, Optional
from Creds import Kalshi_Key, Kalshi_Private_Key
import websocket
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from PyQt6.QtCore import QObject, pyqtSignal

# Valid historical time intervals for kalshi market data: Valid values: 1 (1 minute), 60 (1 hour), 1440 (1 day).
# Time period length of each candlestick in minutes.
# When integrating Client TO BE USED WITH histroical odds widget, be cognizant of time intervals between OddsAPI and Kalshi
# Some markets such as totals or spreads have sub markets  (Alt lines)

class KalshiClient:
    """Client for Kalshi API interactions."""

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    # Authenticated TRADING endpoints (orders) live on the dedicated external host.
    # The legacy /portfolio/orders* endpoints on the elections host now return HTTP
    # 410 (Gone); orders moved to the V2 event-order endpoints under this host.
    TRADE_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

    # Sports-related series prefixes
    SPORTS_SERIES = ['NFL', 'NBA', 'MLB', 'NHL', 'SOCCER', 'FOOTBALL', 'BASKETBALL',
                     'BASEBALL', 'HOCKEY', 'TENNIS', 'GOLF', 'UFC', 'MMA', 'MVE']

    # Game-level series tickers (for fetching individual game markets)
    GAME_SERIES = {
        # Basketball
        'NBA': [
            'KXNBAGAME',
            'KXNBASPREAD',
            'KXNBATOTAL'
        ],
    
        # Football
        'NFL': [
            'KXNFLGAME',
            'KXNFLSPREAD',
            'KXNFLTOTAL'
        ],
        # Uncomment if needed:
        # 'NFL_SINGLE_PROPS': ['KXMVENFLSINGLEGAME'],
        # 'NFL_MULTI_PROPS': ['KXMVENFLMULTIGAMEEXTENDED'],
        'NCAAF': ['KXNCAAFGAME'],
    
        # Baseball
        'MLB': [
            'KXMLBGAME',
            'KXMLBSPREAD',
            'KXMLBTOTAL'
        ],
        'MLB_SERIES': ['KXMLBSERIESGAMETOTAL'],
    
        # Hockey
        'NHL': [
            'KXNHLGAME',
            'KXNHLSPREAD',
            'KXNHLTOTAL'
        ],
    
        # Soccer
        'EPL': ['KXEPLGAME'],
        'UCL': ['KXUCLGAME'],
        'LA_LIGA': ['KXLALIGAGAME'],
        'BUNDESLIGA': ['KXBUNDESLIGAGAME'],
        'SERIE_A': ['KXSERIEAGAME'],
        'LIGUE_1': ['KXLIGUE1GAME'],
        'MLS': ['KXMLSGAME'],
    
        # Esports
        'LOL': ['KXLOLGAMES'],
    }


    # Human-readable descriptions for each series
    SERIES_DESCRIPTIONS = {
        'NBA': 'NBA Basketball',
        'NFL': 'NFL Football',
        'NFL_SINGLE_PROPS': 'NFL Single Game Props',
        'NFL_MULTI_PROPS': 'NFL Multi-Game Props',
        'NCAAF': 'NCAA College Football',
        'MLB': 'MLB Baseball',
        'MLB_SERIES': 'MLB Series Totals',
        'NHL': 'NHL Hockey',
        'EPL': 'English Premier League',
        'UCL': 'UEFA Champions League',
        'LA_LIGA': 'La Liga (Spain)',
        'BUNDESLIGA': 'Bundesliga (Germany)',
        'SERIE_A': 'Serie A (Italy)',
        'LIGUE_1': 'Ligue 1 (France)',
        'MLS': 'Major League Soccer',
        'LOL': 'League of Legends Esports',
    }

    def __init__(self, api_key: str = None):
        """
        Initialize Kalshi client.

        Args:
            api_key: Kalshi API key (defaults to Creds.Kalshi_Key)
        """
        self.api_key = api_key or Kalshi_Key
        self.session = requests.Session()

    def _make_request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        """
        Make authenticated request to Kalshi API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            params: Query parameters
            data: Request body data

        Returns:
            JSON response as dictionary
        """
        url = f"{self.BASE_URL}{endpoint}"

        headers = {
            "Content-Type": "application/json",
        }

        # Note: For now making unauthenticated requests to public endpoints
        # Full authentication requires RSA signing with private key

        # Retry on 429 with exponential backoff. Loading many sports series at once
        # bursts past Kalshi's public rate limit; without this a 429 silently drops
        # a whole league from the event menu (MLB/KBO/etc. randomly vanishing).
        last_exc = None
        for attempt in range(4):
            try:
                response = self.session.request(
                    method=method, url=url, headers=headers,
                    params=params, json=data,
                    timeout=15  # never hang the event loader on a stalled connection
                )
                if response.status_code == 429:
                    time.sleep(0.4 * (2 ** attempt))  # 0.4s, 0.8s, 1.6s, 3.2s
                    last_exc = requests.exceptions.HTTPError("429 Too Many Requests")
                    continue
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                last_exc = e
                if getattr(e, 'response', None) is not None and e.response.status_code == 429:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                print(f"Error making request to {url}: {e}")
                raise
        print(f"Error making request to {url}: rate-limited after retries")
        raise last_exc

    # ------------------------------------------------------------------
    # AUTHENTICATED REST (RSA-PSS signed) — required for portfolio/orders
    # ------------------------------------------------------------------
    def _get_private_key(self):
        if getattr(self, "_priv_key", None) is None:
            self._priv_key = serialization.load_pem_private_key(
                Kalshi_Private_Key.encode(), password=None)
        return self._priv_key

    def _signed_request(self, method: str, endpoint: str,
                        params: Dict = None, data: Dict = None,
                        base_url: str = None) -> Dict:
        """Authenticated request signed per Kalshi's scheme:
        sign `timestamp + METHOD + /trade-api/v2<endpoint-path>` with RSA-PSS/SHA256.

        `base_url` overrides the host (e.g. TRADE_BASE_URL for order endpoints); the
        signed path is `/trade-api/v2<endpoint>` and is host-independent, so the same
        RSA key works across hosts."""
        url = f"{base_url or self.BASE_URL}{endpoint}"
        ts = str(int(time.time() * 1000))
        path = "/trade-api/v2" + endpoint.split("?")[0]
        signature = self._get_private_key().sign(
            (ts + method + path).encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
        resp = self.session.request(method, url, headers=headers,
                                    params=params, json=data)
        if not resp.ok:
            # Surface the API's error body (field name / reason) — far more useful
            # than a bare status code when refining an order payload.
            raise RuntimeError(
                f"Kalshi {method} {endpoint} -> {resp.status_code}: {resp.text[:600]}")
        return resp.json() if resp.content else {}

    @staticmethod
    def legacy_to_v2_order(action: str, side: str, price_cents: int):
        """Map the legacy order shape (action buy/sell + side yes/no + price in
        CENTS) to the V2 single-YES-book shape (side bid/ask + price in DOLLARS).

        The V2 book is the YES book: bid = buy YES, ask = sell YES. A NO order is the
        economic inverse of a YES order at the complementary price:
            buy  NO @ p  ==  sell YES @ (100 - p)  -> ask
            sell NO @ p  ==  buy  YES @ (100 - p)  -> bid
        Returns (v2_side, yes_price_cents, price_dollars_str).
        """
        p = int(price_cents)
        if side == "yes":
            v2_side = "bid" if action == "buy" else "ask"
            yes_cents = p
        else:  # no
            v2_side = "ask" if action == "buy" else "bid"
            yes_cents = 100 - p
        return v2_side, yes_cents, f"{yes_cents / 100:.2f}"

    def create_order(self, ticker: str, action: str, side: str, count: int,
                     price_cents: int = None, order_type: str = "limit",
                     client_order_id: str = None,
                     time_in_force: str = "good_till_canceled",
                     self_trade_prevention_type: str = "taker_at_cross",
                     post_only: bool = False) -> Dict:
        """Place a limit order via the V2 event-order endpoint
        (POST /portfolio/events/orders on TRADE_BASE_URL). action='buy'|'sell',
        side='yes'|'no', price in cents — translated to the V2 bid/ask + dollar
        shape. The legacy /portfolio/orders endpoint now returns 410."""
        import uuid
        if price_cents is None:
            raise ValueError("create_order requires price_cents (limit order)")
        v2_side, _yc, price_dollars = self.legacy_to_v2_order(action, side, price_cents)
        body = {
            "ticker": ticker,
            "side": v2_side,
            "count": str(int(count)),
            "price": price_dollars,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if post_only:
            body["post_only"] = True
        return self._signed_request("POST", "/portfolio/events/orders",
                                    data=body, base_url=self.TRADE_BASE_URL)

    def get_orders(self, ticker: str = None, status: str = "resting") -> Dict:
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self._signed_request("GET", "/portfolio/events/orders",
                                    params=params, base_url=self.TRADE_BASE_URL)

    def cancel_order(self, order_id: str) -> Dict:
        return self._signed_request("DELETE", f"/portfolio/events/orders/{order_id}",
                                    base_url=self.TRADE_BASE_URL)

    # ------------------------------------------------------------------
    # SPORTS DISCOVERY — classify event vs future, find active leagues,
    # rank by volume. Kalshi exposes ~2200 sports series; this distills them
    # to the head-to-head GAME leagues with open events, ordered by traded
    # volume, so the widget menu is data-driven instead of a hardcoded big-4.
    # ------------------------------------------------------------------
    # A few series end in GAME/MATCH but are season/event PROPS, not a matchup.
    # Narrow title denylist — must NOT include league words (cup/champion/winner),
    # which appear in legit head-to-head series ("UEFA Champions League Game").
    _PROP_TITLE_DENY = ("in every game", "teams in game", "home game opponent",
                        "exact match", "goal in every")
    # Companion market-type series sharing a GAME series' league prefix.
    _COMPANION_SUFFIXES = ("SPREAD", "TOTAL", "TEAMTOTAL")
    _SERIES_CACHE_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "kalshi_event_series_cache.json")

    @staticmethod
    def classify_market(ticker: str, title: str = "") -> str:
        """Distinguish a sports series as 'event' (head-to-head GAME/MATCH),
        'event_alt' (the same matchup's SPREAD/TOTAL lines), or 'future'
        (outright/season/award/prop). The ticker suffix is the reliable signal;
        the title only feeds a narrow prop denylist on GAME/MATCH series."""
        tk = (ticker or "").upper()
        tl = (title or "").lower().strip()
        if tk.endswith(("SPREAD", "TOTAL")):
            return "event_alt"
        if tk.endswith(("GAME", "MATCH")):
            return "future" if any(p in tl for p in KalshiClient._PROP_TITLE_DENY) else "event"
        return "future"

    def get_sports_series(self) -> List[Dict]:
        """All series under the Sports category (paginated)."""
        out, cursor = [], None
        while True:
            p = {"category": "Sports", "limit": 200}
            if cursor:
                p["cursor"] = cursor
            r = self.session.get(f"{self.BASE_URL}/series", params=p, timeout=20)
            if not r.ok:
                break
            d = r.json()
            out += d.get("series", [])
            cursor = d.get("cursor")
            if not cursor:
                break
        return out

    def _open_event_count(self, ticker: str, retries: int = 4) -> int:
        for attempt in range(retries):
            try:
                r = self.session.get(f"{self.BASE_URL}/events", params={
                    "series_ticker": ticker, "status": "open", "limit": 200}, timeout=15)
                if r.status_code == 429:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return len(r.json().get("events", [])) if r.ok else 0
            except Exception:
                time.sleep(0.3)
        return 0

    def _series_volume(self, ticker: str, retries: int = 4):
        """(volume, open_interest) summed over a series' open markets (V2 _fp)."""
        vol = oi = 0.0
        cursor = None
        for _ in range(8):
            for attempt in range(retries):
                r = self.session.get(f"{self.BASE_URL}/markets", params={
                    "series_ticker": ticker, "status": "open", "limit": 1000,
                    **({"cursor": cursor} if cursor else {})}, timeout=20)
                if r.status_code == 429:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break
            if not r.ok:
                break
            d = r.json()
            for m in d.get("markets", []):
                try:
                    vol += float(m.get("volume_fp") or 0)
                    oi += float(m.get("open_interest_fp") or 0)
                except (TypeError, ValueError):
                    pass
            cursor = d.get("cursor")
            if not cursor:
                break
        return vol, oi

    def discover_event_series_ranked(self, workers: int = 4) -> List[Dict]:
        """Active head-to-head GAME series, each enriched with companion market
        series + volume, sorted by VOLUME desc. Row:
            {game, tag, title, open_events, volume, open_interest, market_series:[...]}
        Live scan (slow + rate-limited) — callers should use load_ranked_event_series."""
        all_series = self.get_sports_series()
        have = {s.get("ticker", "") for s in all_series}
        games = [s for s in all_series
                 if s.get("ticker", "").endswith("GAME")
                 and self.classify_market(s["ticker"], s.get("title", "")) == "event"]

        def build(s):
            tk = s["ticker"]
            n = self._open_event_count(tk)
            if n == 0:
                return None
            base = tk[:-4]
            companions = [tk] + [base + suf for suf in self._COMPANION_SUFFIXES
                                 if (base + suf) in have]
            vol, oi = self._series_volume(tk)
            return {"game": tk, "tag": (s.get("tags") or ["(none)"])[0],
                    "title": s.get("title", ""), "open_events": n,
                    "volume": vol, "open_interest": oi, "market_series": companions}

        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            rows = [r for r in ex.map(build, games) if r]
        rows.sort(key=lambda r: -r["volume"])
        return rows

    def read_cached_event_series(self) -> List[Dict]:
        """Pure cache read (never scans) — for instant startup. [] if no cache."""
        if os.path.exists(self._SERIES_CACHE_PATH):
            try:
                with open(self._SERIES_CACHE_PATH) as f:
                    return json.load(f).get("series", [])
            except Exception:
                return []
        return []

    def load_ranked_event_series(self, max_age_s: int = 6 * 3600,
                                 refresh: bool = False) -> List[Dict]:
        """Volume-ranked event-series config from a disk cache, rescanning when
        older than max_age_s (the scan is slow, so we don't run it every launch).
        Returns the cached list immediately if a refresh fails. refresh=True forces."""
        cached = None
        if os.path.exists(self._SERIES_CACHE_PATH):
            try:
                with open(self._SERIES_CACHE_PATH) as f:
                    cached = json.load(f)
            except Exception:
                cached = None
        fresh = cached and (time.time() - cached.get("ts", 0) < max_age_s)
        if cached and fresh and not refresh:
            return cached["series"]
        try:
            series = self.discover_event_series_ranked()
            with open(self._SERIES_CACHE_PATH, "w") as f:
                json.dump({"ts": time.time(), "series": series}, f)
            return series
        except Exception as e:
            print(f"[kalshi-discovery] refresh failed ({e}); using cached")
            return cached["series"] if cached else []

    def get_events(
        self,
        limit: int = 100,
        status: Optional[str] = None,
        series_ticker: Optional[str] = None,
        with_nested_markets: bool = False,
        cursor: Optional[str] = None,
        min_close_ts: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get list of events from Kalshi.

        Args:
            limit: Number of results per page (1-200, default 100)
            status: Filter by status ('open', 'closed', 'settled')
            series_ticker: Filter by series ticker
            with_nested_markets: Include markets nested within events
            cursor: Pagination cursor from previous response
            min_close_ts: Filter events with at least one market with close timestamp
                         greater than this Unix timestamp (in seconds)

        Returns:
            Dictionary with 'events', 'cursor', and optionally 'milestones'
        """
        params = {
            "limit": limit,
            "with_nested_markets": with_nested_markets
        }

        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts

        return self._make_request("GET", "/events", params=params)

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> Dict[str, Any]:
        """
        Get details for a specific event.

        Args:
            event_ticker: Event ticker to fetch
            with_nested_markets: Include markets within event object

        Returns:
            Dictionary with 'event' and 'markets' data
        """
        params = {"with_nested_markets": with_nested_markets}
        return self._make_request("GET", f"/events/{event_ticker}", params=params)

    def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get list of markets from Kalshi.

        Args:
            limit: Number of results per page (1-200)
            cursor: Pagination cursor
            event_ticker: Filter by event ticker
            series_ticker: Filter by series ticker
            status: Filter by market status

        Returns:
            Dictionary with 'markets' and 'cursor'
        """
        params = {"limit": limit}

        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status

        return self._make_request("GET", "/markets", params=params)

    # TODO: 'start_ts' and 'end_ts' are actually mandatory, should not have defaults
    def get_market_candlesticks(
        self,
        ticker: str,
        series_ticker: str = None,
        period_interval: int = 1440,
        start_ts: int = None,
        end_ts: int = None
    ) -> Dict[str, Any]:
        """
        Get historical candlestick data for a market.

        Args:
            ticker: Market ticker
            series_ticker: Series ticker (if None, extracted from market ticker)
            period_interval: Candlestick duration (1=minute, 60=hour, 1440=day)
            start_ts: Unix timestamp for start (not actually optional)
            end_ts: Unix timestamp for end (not actually optional)

        Returns:
            Dictionary with 'ticker' and 'candlesticks' array
        """
        # Extract series ticker from market ticker if not provided
        # Format is typically SERIES-MARKET or similar
        if not series_ticker:
            # Attempt to extract from ticker
            # This is a guess - may need adjustment based on actual ticker format
            parts = ticker.split('-')
            if len(parts) > 1:
                series_ticker = parts[0]
            else:
                raise ValueError(f"Cannot determine series_ticker from {ticker}. Please provide explicitly.")
        
        assert((start_ts is not None) and (end_ts is not None)), "missing one or more timestamp parameters"
        
        params = {
            "period_interval": period_interval,
            "start_ts": start_ts,
            "end_ts": end_ts
        }

        endpoint = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        return self._make_request("GET", endpoint, params=params)

    def print_events_summary(self, events_data: Dict[str, Any], max_events: int = 10):
        """
        Pretty print events summary.

        Args:
            events_data: Response from get_events()
            max_events: Maximum number of events to display
        """
        events = events_data.get('events', [])
        print(f"\n{'='*80}")
        print(f"KALSHI EVENTS SUMMARY ({len(events)} events)")
        print(f"{'='*80}\n")

        for i, event in enumerate(events[:max_events]):
            print(f"{i+1}. {event['title']}")
            print(f"   Ticker: {event['event_ticker']}")
            print(f"   Series: {event['series_ticker']}")
            print(f"   Category: {event.get('category', 'N/A')}")
            print(f"   Mutually Exclusive: {event.get('mutually_exclusive', 'N/A')}")

            # Show nested markets if available
            if 'markets' in event and event['markets']:
                print(f"   Markets ({len(event['markets'])}):")
                for market in event['markets'][:3]:  # Show first 3 markets
                    print(f"      - {market.get('yes_sub_title', market.get('ticker'))}")
                    print(f"        Status: {market.get('status')} | "
                          f"Last: ${market.get('last_price_dollars', 'N/A')} | "
                          f"Volume: {market.get('volume', 0)}")
                if len(event['markets']) > 3:
                    print(f"      ... and {len(event['markets']) - 3} more markets")
            print()

        if len(events) > max_events:
            print(f"... and {len(events) - max_events} more events")

        # Show cursor for pagination
        cursor = events_data.get('cursor')
        if cursor:
            print(f"\nPagination cursor available: {cursor[:50]}...")

    def print_candlesticks_summary(self, candlesticks_data: Dict[str, Any], max_candles: int = 50):
        """
        Pretty print candlestick data.

        Args:
            candlesticks_data: Response from get_market_candlesticks()
            max_candles: Maximum number of candlesticks to display
        """
        ticker = candlesticks_data.get('ticker', 'Unknown')
        candles = candlesticks_data.get('candlesticks', [])

        print(f"\n{'='*80}")
        print(f"CANDLESTICK DATA: {ticker} ({len(candles)} periods)")
        print(f"{'='*80}\n")

        if not candles:
            print("No candlestick data available.")
            return

        # Print header
        print(f"{'Date/Time':<20} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>10}")
        print(f"{'-'*20} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")

        # Print candles (most recent first)
        for candle in reversed(candles[-max_candles:]):
            ts = candle.get('end_period_ts', 0)
            dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')

            price = candle.get('price', {})
            open_price = price.get('open') or 0
            high_price = price.get('high') or 0
            low_price = price.get('low') or 0
            close_price = price.get('close') or 0
            volume = candle.get('volume') or 0

            # Handle None values gracefully
            try:
                print(f"{dt:<20} {open_price:>8.2f} {high_price:>8.2f} {low_price:>8.2f} "
                      f"{close_price:>8.2f} {volume:>10}")
            except (TypeError, ValueError):
                # If values can't be formatted, show raw
                print(f"{dt:<20} {str(open_price):>8} {str(high_price):>8} {str(low_price):>8} "
                      f"{str(close_price):>8} {str(volume):>10}")

        if len(candles) > max_candles:
            print(f"\n... and {len(candles) - max_candles} more periods")

    def get_sports_events(self, limit: int = 200, with_markets: bool = True) -> Dict[str, Any]:
        """
        Get all sports-related events.

        Args:
            limit: Number of events to fetch per request
            with_markets: Include nested market data

        Returns:
            Dictionary with sports events
        """
        all_sports_events = []
        cursor = None

        # Fetch events in batches
        while True:
            response = self.get_events(
                limit=limit,
                status='open',
                with_nested_markets=with_markets,
                cursor=cursor
            )

            events = response.get('events', [])

            # Filter for sports events
            for event in events:
                # Check category
                category = event.get('category', '')
                if category == 'Sports':
                    all_sports_events.append(event)
                    continue

                # Check event ticker for sports keywords
                ticker = event.get('event_ticker', '')
                if any(sport in ticker.upper() for sport in self.SPORTS_SERIES):
                    all_sports_events.append(event)

            # Check for more pages
            cursor = response.get('cursor')
            if not cursor or len(events) < limit:
                break

        return {'events': all_sports_events, 'count': len(all_sports_events)}

    def get_sports_markets(self, limit: int = 200) -> Dict[str, Any]:
        """
        Get all sports-related markets.

        Args:
            limit: Number of markets to fetch per request

        Returns:
            Dictionary with sports markets organized by sport
        """
        all_sports_markets = []
        cursor = None

        # Fetch markets in batches
        while True:
            response = self.get_markets(limit=limit, cursor=cursor)
            markets = response.get('markets', [])

            # Filter for sports markets
            for market in markets:
                ticker = market.get('ticker', '')
                if any(sport in ticker.upper() for sport in self.SPORTS_SERIES):
                    all_sports_markets.append(market)

            # Check for more pages
            cursor = response.get('cursor')
            if not cursor or len(markets) < limit:
                break

        # Organize by sport type
        organized = self._organize_sports_markets(all_sports_markets)
        organized['all_markets'] = all_sports_markets
        organized['total_count'] = len(all_sports_markets)

        return organized

    def _organize_sports_markets(self, markets: List[Dict]) -> Dict[str, Any]:
        """
        Organize sports markets by sport type.

        Args:
            markets: List of market dictionaries

        Returns:
            Dictionary organized by sport
        """
        organized = {
            'NFL': [],
            'NBA': [],
            'MLB': [],
            'NHL': [],
            'SOCCER': [],
            'OTHER': []
        }

        for market in markets:
            ticker = market.get('ticker', '').upper()

            categorized = False
            for sport in ['NFL', 'NBA', 'MLB', 'NHL', 'SOCCER']:
                if sport in ticker:
                    organized[sport].append(market)
                    categorized = True
                    break

            if not categorized:
                organized['OTHER'].append(market)

        return organized

    def print_sports_summary(self, sports_data: Dict[str, Any], max_per_sport: int = 5):
        """
        Pretty print sports markets summary.

        Args:
            sports_data: Response from get_sports_markets()
            max_per_sport: Maximum markets to show per sport
        """
        print(f"\n{'='*80}")
        print(f"KALSHI SPORTS MARKETS SUMMARY")
        print(f"Total Sports Markets: {sports_data.get('total_count', 0)}")
        print(f"{'='*80}\n")

        for sport in ['NFL', 'NBA', 'MLB', 'NHL', 'SOCCER', 'OTHER']:
            markets = sports_data.get(sport, [])
            if not markets:
                continue

            print(f"\n{sport} MARKETS ({len(markets)} total)")
            print(f"{'-'*80}")

            for i, market in enumerate(markets[:max_per_sport], 1):
                ticker = market.get('ticker', 'N/A')
                title = market.get('yes_sub_title', 'N/A')
                status = market.get('status', 'N/A')
                last_price = market.get('last_price_dollars', 'N/A')
                volume = market.get('volume', 0)

                print(f"{i}. {title[:100]}")
                print(f"   Ticker: {ticker}")
                print(f"   Status: {status} | Price: ${last_price} | Volume: {volume}")
                print()

            if len(markets) > max_per_sport:
                print(f"   ... and {len(markets) - max_per_sport} more {sport} markets\n")

    def get_game_markets(self, sport: str = 'NBA') -> List[Dict]:
      """
      Get all individual game markets for a specific sport.
      Supports multiple series tickers per sport.
      """
  
      series_tickers = self.GAME_SERIES.get(sport)
      if not series_tickers:
          raise ValueError(f"Unknown sport '{sport}'. Available: {list(self.GAME_SERIES.keys())}")
  
      all_game_markets = []
  
      # Loop through all series tickers belonging to this sport
      for series in series_tickers:
          cursor = None
  
          while True:
              response = self.get_events(
                  series_ticker=series,
                  status='open',
                  with_nested_markets=True,
                  limit=200,
                  cursor=cursor
              )
  
              events = response.get('events', [])
  
              # Extract markets from each event
              for event in events:
                  for market in event.get('markets', []):
                      market['event_title'] = event.get('title')
                      market['event_ticker'] = event.get('event_ticker')
                      all_game_markets.append(market)
  
              cursor = response.get('cursor')
              if not cursor or len(events) < 200:
                  break
  
      return all_game_markets


    @classmethod
    def list_available_sports(cls) -> None:
        """
        Print all available sports that can be queried for game markets.
        """
        print("="*80)
        print("AVAILABLE SPORTS FOR GAME MARKETS")
        print("="*80)
        print("\nUsage: client.get_game_events(sport='SPORT_KEY')")
        print("\nAvailable Sport Keys:\n")

        # Group by category
        categories = {
            'Basketball': ['NBA'],
            'Football': ['NFL', 'NFL_SINGLE_PROPS', 'NFL_MULTI_PROPS', 'NCAAF'],
            'Baseball': ['MLB', 'MLB_SERIES'],
            'Hockey': ['NHL'],
            'Soccer': ['EPL', 'UCL', 'LA_LIGA', 'BUNDESLIGA', 'SERIE_A', 'LIGUE_1', 'MLS'],
            'Esports': ['LOL'],
        }

        for category, sports in categories.items():
            print(f"{category}:")
            for sport_key in sports:
                description = cls.SERIES_DESCRIPTIONS.get(sport_key, sport_key)
                print(f"  '{sport_key}' - {description}")
            print()

        print("="*80)

    def get_game_events(self, sport: str = 'NBA', min_close_ts: Optional[int] = None) -> Dict[str, Any]:
      """
      Get all game events for a specific sport with nested markets.
      Supports multiple series tickers per sport.
      """
  
      series_tickers = self.GAME_SERIES.get(sport)
      if not series_tickers:
          raise ValueError(f"Unknown sport '{sport}'. Available: {list(self.GAME_SERIES.keys())}")
  
      all_events = []
  
      # Loop through every series ticker for this sport
      for series in series_tickers:
          cursor = None
  
          while True:
              response = self.get_events(
                  series_ticker=series,
                  status='open',
                  with_nested_markets=True,
                  limit=200,
                  cursor=cursor,
                  min_close_ts=min_close_ts
              )
  
              events = response.get('events', [])
              all_events.extend(events)
  
              cursor = response.get('cursor')
              if not cursor or len(events) < 200:
                  break
  
      return {'events': all_events, 'count': len(all_events)}


    # Module saved from deleted test file
    def get_all_series() -> Dict:
        """
        Fetch all series from Kalshi API.

        Returns:
            Dictionary with series data
        """

        BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
        url = f"{BASE_URL}/series"
        all_series = []
        cursor = None

        print("Fetching all series from Kalshi API...")

        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor

            try:
                response = requests.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                series = data.get('series', [])
                all_series.extend(series)

                print(f"  Fetched {len(series)} series (total: {len(all_series)})")

                cursor = data.get('cursor')
                if not cursor or len(series) < 200:
                    break

            except requests.exceptions.RequestException as e:
                print(f"Error fetching series: {e}")
                break

        return {'series': all_series, 'count': len(all_series)}

class KalshiStreamClient(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(object)
    raw_message = pyqtSignal(object)
    tick = pyqtSignal(object)
    # Sub-second feed signals (additive; ticker/`tick` path is unchanged).
    # `trade` carries individual executions; `orderbook` carries both the
    # initial orderbook_snapshot and subsequent orderbook_delta messages.
    trade = pyqtSignal(object)
    orderbook = pyqtSignal(object)
    raw_frame = pyqtSignal(str)   # original frame string, for native (C++) parsing
    # True round-trip latency (ms) from a protocol-level ping/pong exchange. Kalshi
    # has no app-level ping message; this rides the RFC6455 control frames that the
    # websocket-client library already sends on `ping_interval`. Clock-skew-free:
    # both timestamps are local, so no NTP dependency (unlike a server-stamp diff).
    latency = pyqtSignal(float)

    def __init__(
        self,
        url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2",
        reconnect: bool = True,
        reconnect_backoff_max: int = 60,
        ping_interval: int = 15,
        ping_timeout: int = 10,
        on_message_callback: Optional[Callable[[dict], None]] = None,
    ):
        """
        WebSocket streaming client for Kalshi with PyQt6 signals.
        Private key + API key ID are imported directly from Creds.py
        """

        super().__init__()

        self.api_key_id = Kalshi_Key  # This is actually the API Key ID (UUID)
        self.private_key_pem = Kalshi_Private_Key

        # Load the PEM private key from the triple-quoted string
        self._private_key = serialization.load_pem_private_key(
            self.private_key_pem.encode(),
            password=None
        )

        self.url = url
        self.reconnect = reconnect
        self.reconnect_backoff_max = reconnect_backoff_max
        self.ping_interval = ping_interval
        # Must stay < ping_interval (websocket-client enforces this) and is the
        # window we wait for the pong before declaring the connection dead.
        self.ping_timeout = ping_timeout
        self._on_message_callback = on_message_callback

        self._ws_app: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._subscriptions = set()
        self._is_running = False
        self._running_lock = threading.Lock()
        self._message_id = 1
        self._last_rtt_ms = None   # most recent ping/pong round-trip (ms)

    # -------------------------------------------------------------------
    # PUBLIC API
    # -------------------------------------------------------------------
    def start(self):
        """Start WebSocket thread."""
        with self._running_lock:
            if self._is_running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_forever,
                name="KalshiWS",
                daemon=True
            )
            self._thread.start()
            self._is_running = True

    def stop(self):
        """Stop WebSocket thread + connection."""
        self._stop_event.set()
        self.reconnect = False
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception:
                pass

        if self._thread:
            self._thread.join(timeout=5)

        with self._running_lock:
            self._is_running = False

    def send(self, message: str):
        """Send a raw JSON string to the WebSocket."""
        if (
            self._ws_app
            and getattr(self._ws_app, "sock", None)
            and self._ws_app.sock.connected
        ):
            try:
                self._ws_app.send(message)
            except Exception as e:
                self.error.emit({"action": "send_failed", "exception": e})
        else:
            self.error.emit({"action": "not_connected"})

    # -------------------------------------------------------------------
    # AUTHENTICATION (WebSocket headers)
    # -------------------------------------------------------------------
    def _build_ws_headers(self) -> list:
        """
        Build Kalshi-required WS authentication headers.
        RSA signature generated from private key imported from Creds.py.
        """

        timestamp = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        string_to_sign = timestamp + "GET" + path

        signature = self._private_key.sign(
            string_to_sign.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )

        signature_b64 = base64.b64encode(signature).decode("utf-8")

        headers = [
            f"KALSHI-ACCESS-KEY: {self.api_key_id}",
            f"KALSHI-ACCESS-SIGNATURE: {signature_b64}",
            f"KALSHI-ACCESS-TIMESTAMP: {timestamp}",
        ]

        return headers

    # -------------------------------------------------------------------
    # SUBSCRIPTION PAYLOADS
    # -------------------------------------------------------------------
    def _msg_id(self):
        mid = self._message_id
        self._message_id += 1
        return mid

    def build_subscribe(self, channels: Iterable[str], market_tickers=None):
        params = {"channels": list(channels)}
        if market_tickers:
            params["market_tickers"] = list(market_tickers)

        return {
            "id": self._msg_id(),
            "cmd": "subscribe",
            "params": params
        }

    def build_unsubscribe(self, channels: Iterable[str], market_tickers=None):
        params = {"channels": list(channels)}
        if market_tickers:
            params["market_tickers"] = list(market_tickers)

        return {
            "id": self._msg_id(),
            "cmd": "unsubscribe",
            "params": params
        }

    def subscribe(self, channels, tickers=None):
        self._subscriptions.add((tuple(channels), tuple(tickers) if tickers else None))
        self.send(json.dumps(self.build_subscribe(channels, tickers)))

    def unsubscribe(self, channels, tickers=None):
        self._subscriptions.discard((tuple(channels), tuple(tickers) if tickers else None))
        self.send(json.dumps(self.build_unsubscribe(channels, tickers)))

    def unsubscribe_sids(self, sids):
        """Unsubscribe by subscription id (Kalshi's unsubscribe keys on `sids`)."""
        sids = [s for s in sids if s is not None]
        if not sids:
            return
        self.send(json.dumps({
            "id": self._msg_id(),
            "cmd": "unsubscribe",
            "params": {"sids": list(sids)},
        }))

    def get_snapshot(self, tickers, sids=None):
        """Request a fresh `orderbook_snapshot` for `tickers` WITHOUT touching the
        subscription. This is Kalshi's intended orderbook-resync path: per the docs
        it "sends an orderbook_snapshot response for the requested markets without
        adding them to the subscription or affecting the existing delta stream", so
        the deltas keep flowing and only the local book re-baselines from the fresh
        snapshot. Far less fragile than unsubscribe+resubscribe when several markets
        interleave on one shared sid (both-sides YES, multi-contract fields), where a
        teardown can starve siblings or land them on a new sid mid-stream.

        `sids` (optional) scopes the request to specific subscriptions; the array
        `market_tickers` form is the only one the server accepts."""
        tickers = [t for t in (tickers or []) if t]
        if not tickers:
            return
        params = {"market_tickers": list(tickers)}
        sids = [s for s in (sids or []) if s is not None]
        if sids:
            params["sids"] = list(sids)
        self.send(json.dumps({
            "id": self._msg_id(),
            "cmd": "get_snapshot",
            "params": params,
        }))

    # Convenience helpers
    def subscribe_ticker(self, tickers):
        self.subscribe(["ticker"], tickers)

    def subscribe_orderbook(self, tickers):
        self.subscribe(["orderbook_delta"], tickers)

    def subscribe_trade(self, tickers):
        self.subscribe(["trade"], tickers)

    def subscribe_live(self, tickers):
        """Subscribe to the full sub-second feed: trades + orderbook deltas.

        Sent as two separate subscriptions so each channel gets its own seq
        stream (a combined subscription interleaves trade messages into the
        orderbook seq counter, which looks like constant gaps)."""
        self.subscribe(["trade"], tickers)
        self.subscribe(["orderbook_delta"], tickers)

    # -------------------------------------------------------------------
    # INTERNAL THREAD LOOP
    # -------------------------------------------------------------------
    def _run_forever(self):
        backoff = 1.0

        while not self._stop_event.is_set():
            try:
                self._ws_app = websocket.WebSocketApp(
                    self.url,
                    header=self._build_ws_headers(),
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_pong=self._on_pong
                )

                self._ws_app.run_forever(
                    ping_interval=self.ping_interval, ping_timeout=self.ping_timeout
                )

            except Exception as ex:
                self.error.emit({"action": "run_exception", "exception": ex})

            if self._stop_event.is_set() or not self.reconnect:
                break

            time.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_backoff_max)

        self._ws_app = None
        self._is_running = False

    # -------------------------------------------------------------------
    # CALLBACKS
    # -------------------------------------------------------------------
    def _on_open(self, ws):
        self.connected.emit()
        # Re-subscribe old channels
        try:
            for ch_tuple, tickers in self._subscriptions:
                ws.send(json.dumps(self.build_subscribe(ch_tuple, tickers)))
        except Exception as ex:
            self.error.emit({"action": "resubscribe_fail", "exception": ex})

    def _on_message(self, ws, message: str):
        try:
            msg = orjson.loads(message)  # ~1.5x faster decode on the sub-second path
        except Exception:
            msg = message

        self.raw_message.emit(msg)

        if self._on_message_callback:
            try:
                self._on_message_callback(msg)
            except Exception as ex:
                self.error.emit({"action": "callback_fail", "exception": ex})

        # Emit typed messages. ticker -> tick (legacy path, unchanged);
        # trade/orderbook -> dedicated signals for the sub-second Live feed.
        if isinstance(msg, dict):
            mtype = msg.get("type")
            if mtype == "ticker":
                self.tick.emit(msg)
            elif mtype == "trade":
                self.trade.emit(msg)
                self._emit_raw_frame(message)
            elif mtype in ("orderbook_snapshot", "orderbook_delta"):
                self.orderbook.emit(msg)
                self._emit_raw_frame(message)

    def _emit_raw_frame(self, message):
        """Expose the untouched frame string (book-relevant types only) so a native
        (C++) book can parse it directly, skipping json.loads + the dict build."""
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", "replace")
        if isinstance(message, str):
            try:
                self.raw_frame.emit(message)
            except Exception:
                pass

    def _on_pong(self, ws, message):
        """Protocol-level pong to one of OUR pings (the library sends a ping every
        `ping_interval` and records the send time as `ws.last_ping_tm`). The gap to
        now is the genuine round-trip time — no clock-sync dependency, both stamps
        local. Pongs the library auto-sends in reply to the SERVER's pings don't
        reach here, so this only times round trips we initiated."""
        try:
            sent = getattr(ws, "last_ping_tm", 0.0)
            if sent:
                rtt_ms = max(0.0, (time.time() - sent) * 1000.0)
                self._last_rtt_ms = rtt_ms
                self.latency.emit(rtt_ms)
        except Exception:
            pass

    def _on_error(self, ws, error):
        self.error.emit({"action": "ws_error", "error": error})

    def _on_close(self, ws, code, reason):
        self.disconnected.emit()



class KalshiLiveBook:
    """
    Maintains a live YES orderbook for one or more Kalshi markets from the
    `orderbook_snapshot` / `orderbook_delta` websocket stream, and tracks the
    last executed trade price from the `trade` stream.

    Kalshi book convention (prices in integer cents, 1-99):
      - `yes` levels are resting bids to buy YES at that price.
      - `no`  levels are resting bids to buy NO at that price, which is
        equivalently an offer to SELL YES at (100 - price).
    So for the YES outcome:
      - best YES bid  = max(yes price levels)
      - best YES ask  = 100 - max(no price levels)

    Sequence handling: Kalshi's `seq` is per-SUBSCRIPTION (per `sid`), not per
    market or per connection. The book binds to the `sid` of its most recent
    snapshot and IGNORES deltas carrying any other sid. This makes duplicate /
    stale subscriptions (e.g. left over from a market switch or a resubscribe)
    harmless instead of producing interleaved seqs that look like constant gaps.
    Within the bound sid each delta must be prev seq + 1; a real gap fires
    `on_gap(market_ticker)` (caller debounces) and we realign best-effort.
    """

    def __init__(self, on_gap: Optional[Callable[[str], None]] = None):
        # market_ticker -> {"yes": {price:int qty:int}, "no": {...}, "sid": int,
        #                   "seq": int, "last_trade": int|None, "stale": bool}
        self._books: Dict[str, Dict] = {}
        # Kalshi's `seq` is per-SID, not per market_ticker. A single subscription can
        # carry several markets (Kalshi only serves one active orderbook_delta sub per
        # connection), and their deltas interleave on one shared seq counter. Validate
        # seq at the SID level so an interleaved multi-market stream isn't mistaken for
        # per-market gaps. sid -> last seq seen on that subscription.
        self._sid_seq: Dict[int, int] = {}
        self._on_gap = on_gap

    def _blank(self):
        return {"yes": {}, "no": {}, "sid": None, "seq": None,
                "last_trade": None, "stale": False}

    @staticmethod
    def _to_cents(p):
        """Normalize a price to integer cents. Accepts int cents (1-99), a
        dollar string like '0.2300', or a float. Kalshi mixes both forms."""
        if isinstance(p, bool):
            return None
        if isinstance(p, int):
            return p
        try:
            f = float(p)
        except (TypeError, ValueError):
            return None
        # dollar-denominated (<= $1) -> cents; otherwise already cents
        return int(round(f * 100)) if f <= 1.0 else int(round(f))

    @staticmethod
    def _to_qty(q):
        """Quantities arrive as floats or float strings (e.g. '801545.83')."""
        try:
            return float(q)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _parse_levels(cls, inner, side):
        """Parse a snapshot side into {cents: qty_float}. Kalshi's live schema
        uses `<side>_dollars_fp` ([[dollar_str, qty_str], ...]); older/other forms
        (`<side>` integer-cent, `<side>_dollars`) are accepted as fallbacks."""
        arr = (inner.get(f"{side}_dollars_fp")
               or inner.get(side)
               or inner.get(f"{side}_dollars"))
        out = {}
        for entry in (arr or []):
            try:
                price, qty = entry[0], entry[1]
            except (TypeError, IndexError):
                continue
            c = cls._to_cents(price)
            if c is not None:
                out[c] = cls._to_qty(qty)
        return out

    def current_sid(self, ticker: str):
        book = self._books.get(ticker)
        return book.get("sid") if book else None

    def apply(self, msg: dict) -> Optional[dict]:
        """Apply a trade/orderbook message. Returns the normalized state for the
        affected market, or None if the message was ignored."""
        if not isinstance(msg, dict):
            return None
        mtype = msg.get("type")
        inner = msg.get("msg", {}) or {}
        ticker = inner.get("market_ticker")
        if not ticker:
            return None

        if mtype == "orderbook_snapshot":
            book = self._books.get(ticker)
            last = book.get("last_trade") if book else None
            book = self._blank()
            book["last_trade"] = last  # preserve last trade across resnapshots
            book["yes"] = self._parse_levels(inner, "yes")
            book["no"] = self._parse_levels(inner, "no")
            book["sid"] = msg.get("sid")
            book["seq"] = msg.get("seq")
            # Baseline the SID-level seq from this snapshot (the snapshot's seq is part
            # of the subscription's single sequence stream shared by all its markets).
            if book["sid"] is not None and book["seq"] is not None:
                self._sid_seq[book["sid"]] = book["seq"]
            self._books[ticker] = book
            return self.state(ticker)

        if mtype == "orderbook_delta":
            book = self._books.get(ticker)
            seq = msg.get("seq")
            sid = msg.get("sid")
            # No snapshot established yet (book absent, or only a trade has been
            # seen so seq is unset) -> can't apply a delta. Ask for a snapshot.
            if book is None or book.get("seq") is None:
                if self._on_gap:
                    self._on_gap(ticker)
                return None
            # Ignore deltas from any sid other than the one our snapshot bound to
            # (stale/duplicate subscription). This is what prevents the false-gap
            # cascade when multiple orderbook subscriptions exist for one market.
            if book.get("sid") is not None and sid is not None and sid != book["sid"]:
                return None
            # Sequence gap is evaluated at the SID level: one subscription's seq counter
            # is shared across every market it carries, so consecutive deltas for the
            # SAME market are NOT consecutive in seq (a sibling market's deltas fall in
            # between). Checking per-market would flag those interleavings as gaps and
            # freeze the book. Only a break in the SID's own monotonic sequence is real.
            prev_seq = self._sid_seq.get(sid) if sid is not None else book.get("seq")
            if prev_seq is not None and seq is not None and seq != prev_seq + 1:
                book["stale"] = True
                if self._on_gap:
                    self._on_gap(ticker)
            if sid is not None and seq is not None:
                self._sid_seq[sid] = seq
            side = inner.get("side")
            price = self._to_cents(
                inner.get("price") if inner.get("price") is not None
                else inner.get("price_dollars"))
            # Live schema uses `delta_fp` (string float); accept `delta` as fallback.
            raw_delta = inner.get("delta_fp")
            if raw_delta is None:
                raw_delta = inner.get("delta")
            if side in ("yes", "no") and price is not None and raw_delta is not None:
                levels = book[side]
                new_qty = levels.get(price, 0.0) + self._to_qty(raw_delta)
                if new_qty <= 1e-9:
                    levels.pop(price, None)
                else:
                    levels[price] = new_qty
            book["seq"] = seq
            return self.state(ticker)

        if mtype == "trade":
            book = self._books.setdefault(ticker, self._blank())
            yp = inner.get("yes_price_dollars")
            if yp is not None:
                try:
                    book["last_trade"] = int(round(float(yp) * 100))
                except (TypeError, ValueError):
                    pass
            return self.state(ticker)

        return None

    def state(self, ticker: str) -> Optional[dict]:
        """Return normalized YES-outcome state in cents: best bid/ask, mid, last."""
        book = self._books.get(ticker)
        if not book:
            return None
        best_bid = max(book["yes"]) if book["yes"] else None
        best_ask = (100 - max(book["no"])) if book["no"] else None
        mid = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            mid = best_bid
        elif best_ask is not None:
            mid = best_ask
        return {
            "market_ticker": ticker,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "last_trade": book["last_trade"],
            "stale": book["stale"],
        }

    def ladder(self, ticker: str, depth: int = 10) -> Optional[dict]:
        """YES-outcome depth ladder in cents.

        bids = resting YES bids (book['yes']) sorted best (highest) first.
        asks = YES offers derived from NO bids: ask price = 100 - no_price,
               size = no_size; sorted best (lowest) first.
        Returns {'bids': [(price, qty)], 'asks': [(price, qty)],
                 'best_bid', 'best_ask', 'last_trade', 'stale'}.
        """
        book = self._books.get(ticker)
        if not book:
            return None
        bids = sorted(book["yes"].items(), key=lambda x: -x[0])[:depth]
        asks = sorted(((100 - p, q) for p, q in book["no"].items()),
                      key=lambda x: x[0])[:depth]
        return {
            "bids": [(int(p), int(round(q))) for p, q in bids],
            "asks": [(int(p), int(round(q))) for p, q in asks],
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "last_trade": book["last_trade"],
            "stale": book["stale"],
        }

    def reset(self, ticker: str = None):
        if ticker is None:
            self._books.clear()
            self._sid_seq.clear()
        else:
            book = self._books.pop(ticker, None)
            # Drop the per-sid seq baseline only if no other book still rides this sid.
            sid = book.get("sid") if book else None
            if sid is not None and not any(b.get("sid") == sid
                                           for b in self._books.values()):
                self._sid_seq.pop(sid, None)


def main():
    """Example usage of KalshiClient."""
    client = KalshiClient()

    print("=" * 80)
    print("KALSHI API CLIENT - DEMO")
    print("=" * 80)

    # 1. Fetch open events with nested markets
    print("\n[1] Fetching open events with markets...")
    try:
        events_response = client.get_events(
            limit=10,
            status="open",
            with_nested_markets=True
        )
        client.print_events_summary(events_response, max_events=5)
    except Exception as e:
        print(f"Error fetching events: {e}")

    # 2. Get a specific event (example - you'll need to replace with actual ticker)
    print("\n[2] Fetching specific event details...")
    try:
        # Note: You need to replace this with an actual event ticker
        # event_data = client.get_event("EXAMPLE-EVENT-23")
        # print(f"Event: {event_data['event']['title']}")
        print("(Skipped - need actual event ticker)")
    except Exception as e:
        print(f"Error: {e}")

    # 3. Fetch markets (without status filter as it may not be valid)
    print("\n[3] Fetching markets...")
    try:
        markets_response = client.get_markets(limit=5)
        markets = markets_response.get('markets', [])
        print(f"Found {len(markets)} markets")
        for market in markets[:3]:
            print(f"  - {market.get('ticker')}: {market.get('yes_sub_title', 'N/A')}")
            print(f"    Status: {market.get('status')}, Last: ${market.get('last_price_dollars', 'N/A')}")
    except Exception as e:
        print(f"Error fetching markets: {e}")

    # 4. Get historical candlestick data for Mars market
    print("\n[4] Fetching candlestick data for Elon Musk Mars market...")
    try:
        # Using the Mars market we found above
        candlesticks = client.get_market_candlesticks(
            ticker="KXELONMARS-99",
            series_ticker="KXELONMARS",
            period_interval=1440,  # Daily
            days_back=30
        )
        client.print_candlesticks_summary(candlesticks, max_candles=15)
    except Exception as e:
        print(f"Error: {e}")

    print("\n" + "=" * 80)
    print("Demo complete!")
    print("=" * 80)


if __name__ == "__main__":
    main()
