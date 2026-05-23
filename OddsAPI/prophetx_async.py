#!/usr/bin/env python3
"""
ProphetX Exchange - Combined Async Module
Async API queries + Background worker for non-blocking orderbook updates
"""

import aiohttp
import json
import asyncio
import orjson
from datetime import datetime
from typing import Optional, Dict, List
from PyQt6.QtCore import QObject, pyqtSignal, QThread, QTimer
import qasync
from Creds import PROPHETX_AUTH_TOKEN

BASE_URL = "https://cash.api.prophetx.co/trade/public/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "Accept": "application/json, text/plain, */*",
    "__source": "web",
    "X-Currency": "cash",
    "Authorization": f"Bearer {PROPHETX_AUTH_TOKEN}",
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)


# ============================================================================
# ASYNC API FUNCTIONS
# ============================================================================

async def GetTournamentsAsync(session: aiohttp.ClientSession, type_filter: str = "highlight", limit: int = 150) -> Optional[Dict]:
    """
    Async fetch all tournaments with their events.

    Args:
        session: aiohttp ClientSession
        type_filter: Tournament type filter (default: 'highlight')
        limit: Maximum number of results (default: 150)

    Returns:
        Response data dictionary or None on error
    """
    url = f"{BASE_URL}/v1/tournaments"
    params = {
        "expand": "events",
        "type": type_filter,
        "limit": limit
    }

    try:
        async with session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                print(f"ERROR: Failed to fetch tournaments (status: {response.status})")
                return None

            text = await response.text()
            data = json.loads(text)
            return data
    except Exception as e:
        print(f"ERROR: Failed to fetch tournaments: {e}")
        return None


# Cooldown for v2 fallback: once v2 returns 403 (CloudFront WAF) we
# avoid the wasted round-trip for this many seconds, then probe again.
# v1 lacks lineID on moneyline/simple markets (only marketLines-shaped
# player props carry them), which breaks single-bet placement — so we
# must recover to v2 as soon as the WAF rule clears.
import time as _time
_PX_V2_BLOCK_UNTIL = 0.0
_PX_V2_BLOCK_SECONDS = 300  # 5 min


async def GetEventMarketsAsync(session: aiohttp.ClientSession, event_id: int) -> Optional[Dict]:
    """
    Async fetch all markets (orderbook) for a specific event.

    Tries v2 first; transparently falls back to v1 if CloudFront blocks
    v2 with a 403. Once any v2 call has been blocked we stop trying it
    for the rest of the session so we don't keep hammering a WAF rule.

    Args:
        session: aiohttp ClientSession
        event_id: The event ID to fetch markets for

    Returns:
        Response data dictionary or None on error
    """
    global _PX_V2_BLOCK_UNTIL
    v2_blocked_now = _time.time() < _PX_V2_BLOCK_UNTIL
    versions = ("v1",) if v2_blocked_now else ("v2", "v1")

    for version in versions:
        url = f"{BASE_URL}/{version}/events/{event_id}/markets"
        try:
            async with session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT) as response:
                if response.status == 200:
                    data_bytes = await response.read()
                    if not data_bytes:
                        return None
                    return orjson.loads(data_bytes)

                if response.status == 403 and version == "v2":
                    # CloudFront edge block — arm the TTL and try v1 for
                    # this event. Subsequent calls will probe v2 again
                    # once the cooldown expires.
                    _PX_V2_BLOCK_UNTIL = _time.time() + _PX_V2_BLOCK_SECONDS
                    print(f"ProphetX /v2 blocked by CloudFront — falling back to /v1 for {_PX_V2_BLOCK_SECONDS}s",
                          flush=True)
                    continue

                # Any other non-200: surface it and stop.
                body_preview = ""
                try:
                    body_preview = (await response.text())[:200]
                except Exception:
                    pass
                print(f"ERROR: ProphetX markets HTTP {response.status} ({version}) for event {event_id}: {body_preview}",
                      flush=True)
                return None
        except orjson.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON for event {event_id} ({version}): {e}")
            return None
        except Exception as e:
            print(f"ERROR: Failed to fetch markets for event {event_id} ({version}): {e}")
            return None

    return None


def precompute_market_caches(markets_data: Dict) -> Dict:
    """Walk every market once and stamp two derived caches on each:

      - ``_active_liquidity``: sum of every order's ``value``. Read by
        LiquidityWidget.calculateActiveMarketLiquidity to populate the
        combo box's dollar figures without re-walking the orders.
      - ``_content_signature``: blake2b digest of the market's name +
        line names + every (odds, value, displayName). Read by
        ProphetXBrowser._marketContentSignature to short-circuit the
        orderbook re-render when nothing visible has changed.

    Designed to run in a ThreadPoolExecutor via run_in_executor() so the
    ~20-40ms of order-walking work for a 199-market payload lands in a
    worker thread instead of the qasync (main) loop. CPython releases
    the GIL every ~5ms during pure-Python work, which gives the Qt
    event loop frequent breathing room and prevents the visible
    tickertape stutter on each refresh tick.

    Mutates ``markets_data`` in place and returns it so the caller can
    write ``markets_data = await loop.run_in_executor(...)``.
    """
    import hashlib
    markets = (markets_data or {}).get('data', {}).get('markets', []) or []

    for market in markets:
        total = 0.0
        h = hashlib.blake2b(digest_size=12)
        h.update((market.get('name', '') or '').encode())

        def _walk_orders(side_orders):
            nonlocal total
            if not side_orders:
                return
            for o in side_orders:
                v = o.get('value', 0)
                if v:
                    try:
                        total += float(v)
                    except (TypeError, ValueError):
                        pass
                h.update(b"|")
                h.update(str(o.get('odds', '')).encode())
                h.update(b",")
                h.update(str(v).encode())
                h.update(b",")
                h.update((o.get('displayName') or '').encode())
                h.update(b",")
                h.update(str(o.get('lineID') or '').encode())

        market_lines = market.get('marketLines') or []
        if market_lines:
            for ml in market_lines:
                h.update(b"\n")
                h.update((ml.get('name', '') or '').encode())
                for side_orders in ml.get('selections', []) or []:
                    _walk_orders(side_orders)
        else:
            for side_orders in market.get('selections', []) or []:
                _walk_orders(side_orders)

        market['_active_liquidity'] = total
        market['_content_signature'] = h.hexdigest()

    return markets_data


def ExtractEventList(tournaments_data: Dict) -> List[Dict]:
    """Extract flat list of events from tournaments response."""
    events = []

    if not tournaments_data or 'data' not in tournaments_data:
        return events

    tournaments = tournaments_data['data'].get('tournaments', [])

    for tournament in tournaments:
        if 'sportEvents' not in tournament:
            continue

        for event in tournament['sportEvents']:
            events.append({
                'id': event.get('id'),
                'name': event.get('displayName') or event.get('name'),
                'startTime': event.get('scheduled'),
                'status': event.get('status'),
                'tournament': tournament.get('name'),
                'sport': event.get('sport', {}).get('name'),
                'stake': event.get('stake', 0),
            })

    return events


# ============================================================================
# BACKGROUND WORKER
# ============================================================================

class ProphetXWorker(QObject):
    """
    Background worker for async ProphetX data fetching.
    Uses signals to trigger async operations in the main event loop.
    Periodically refreshes the selected event's orderbook.
    """

    # Signals
    data_ready = pyqtSignal(dict)  # Emitted when event markets data is ready
    tournaments_ready = pyqtSignal(list)  # Emitted when tournament/event list is ready
    error_occurred = pyqtSignal(str)  # Emitted on error
    status_update = pyqtSignal(str)  # Emitted for status messages
    fetch_requested = pyqtSignal(int)  # Internal signal to trigger fetch

    def __init__(self, refresh_interval: int = 30):
        super().__init__()
        self.running = False
        self.current_event_id = None
        self.refresh_interval = refresh_interval  # seconds (default: 30 seconds)
        self.refresh_timer = None

    def start(self):
        """Start the worker and periodic refresh timer"""
        self.running = True

        # Set up periodic refresh timer
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.trigger_refresh)
        self.refresh_timer.start(self.refresh_interval * 1000)  # Convert to milliseconds

        print(f"ProphetX worker started with {self.refresh_interval}s refresh interval")

    def stop(self):
        """Stop the worker and timer"""
        self.running = False
        if self.refresh_timer:
            self.refresh_timer.stop()

    def set_event(self, event_id: int):
        """Set the current event to track and trigger immediate fetch"""
        self.current_event_id = event_id
        # Trigger immediate fetch when event changes
        self.trigger_refresh()

    def trigger_refresh(self):
        """Trigger async refresh of current event (called by timer or manually)"""
        if self.current_event_id and self.running:
            # Emit signal to request fetch (will be handled in main thread)
            self.fetch_requested.emit(self.current_event_id)

    async def fetch_tournaments(self):
        """Fetch all tournaments and extract event list"""
        try:
            self.status_update.emit("Fetching ProphetX tournaments...")

            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                tournaments_data = await GetTournamentsAsync(session)

                if not tournaments_data:
                    self.error_occurred.emit("Failed to fetch tournaments")
                    return

                events = ExtractEventList(tournaments_data)
                self.tournaments_ready.emit(events)
                self.status_update.emit(f"Loaded {len(events)} ProphetX events")

        except Exception as e:
            error_msg = f"Error fetching tournaments: {e}"
            self.error_occurred.emit(error_msg)
            print(error_msg)

    async def fetch_event_markets(self):
        """Fetch markets for the currently selected event"""
        if not self.current_event_id:
            return

        try:
            self.status_update.emit(f"Updating ProphetX event {self.current_event_id}...")

            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                markets_data = await GetEventMarketsAsync(session, self.current_event_id)

                if not markets_data:
                    self.error_occurred.emit(f"No markets data for event {self.current_event_id}")
                    return

                # Emit the data
                self.data_ready.emit(markets_data)

                num_markets = len(markets_data.get('data', {}).get('markets', []))
                self.status_update.emit(f"Updated {num_markets} markets")

        except Exception as e:
            error_msg = f"Error fetching event markets: {e}"
            self.error_occurred.emit(error_msg)
            print(error_msg)


# ============================================================================
# STANDALONE UTILITY FUNCTIONS
# ============================================================================

async def FetchSingleEventAsync(event_id: int, event_metadata: Optional[Dict] = None) -> Optional[Dict]:
    """
    Fetch a single event's markets asynchronously.

    Args:
        event_id: Event ID to fetch
        event_metadata: Optional event metadata to include

    Returns:
        Complete event data with markets, or None on error
    """
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        markets_data = await GetEventMarketsAsync(session, event_id)

        if not markets_data:
            return None

        # Add metadata if provided
        if event_metadata:
            markets_data['event_metadata'] = event_metadata

        return markets_data


async def FetchAllEventsAsync(save_combined: bool = False, max_concurrent: int = 10) -> Dict:
    """
    Fetch all events and their markets asynchronously.
    Uses true concurrency with semaphore to limit simultaneous requests.

    Args:
        save_combined: Save results to combined JSON file
        max_concurrent: Maximum number of concurrent requests (default 10)

    Returns:
        Dictionary mapping event_id -> market_data
    """
    print("=" * 60)
    print("ProphetX Exchange Async Full Scrape")
    print("=" * 60)

    # Semaphore to limit concurrent requests
    semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch_with_semaphore(session, event):
        """Fetch single event with semaphore limiting"""
        async with semaphore:
            try:
                markets_data = await GetEventMarketsAsync(session, event['id'])
                if markets_data:
                    markets_data['event_metadata'] = event
                return (event, markets_data)
            except Exception as e:
                print(f"  Error fetching {event['name']}: {e}")
                return (event, None)

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        # Step 1: Get all tournaments and events
        print("\n[1/2] Fetching tournaments and events...")
        tournaments_data = await GetTournamentsAsync(session)

        if not tournaments_data:
            print("ERROR: Failed to fetch tournaments")
            return {}

        events = ExtractEventList(tournaments_data)
        print(f"Found {len(events)} events across all tournaments\n")

        # Step 2: Fetch markets for all events TRULY concurrently
        print(f"[2/2] Fetching markets for {len(events)} events (max {max_concurrent} concurrent)...")

        # Create actual concurrent tasks
        tasks = [fetch_with_semaphore(session, event) for event in events]

        # Gather all results concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        all_markets = {}
        success_count = 0
        for i, result in enumerate(results, 1):
            if isinstance(result, Exception):
                print(f"  [{i}/{len(events)}] Task exception: {result}")
                continue

            event, markets_data = result
            if markets_data:
                num_markets = len(markets_data.get('data', {}).get('markets', []))
                total_stake = sum(m.get('totalStake', 0) for m in markets_data.get('data', {}).get('markets', []))
                print(f"  [{i}/{len(events)}] {event['name']}: OK ({num_markets} markets, ${total_stake:,.0f})")
                all_markets[event['id']] = markets_data
                success_count += 1
            else:
                print(f"  [{i}/{len(events)}] {event['name']}: SKIPPED (no markets)")

    print(f"\nSuccessfully scraped {success_count}/{len(events)} events")

    # Save combined results if requested (run in executor to not block)
    if save_combined and all_markets:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _save_combined_sync, all_markets)

    print("=" * 60)
    return all_markets


def _save_combined_sync(all_markets: Dict):
    """Synchronous save helper to run in executor"""
    from ProphetXQuery import SaveResponse
    combined_file = SaveResponse("all_markets_combined", all_markets)
    print(f"\nCombined data saved to: {combined_file}")


if __name__ == "__main__":
    # Example: Fetch all events
    all_data = asyncio.run(FetchAllEventsAsync(save_combined=True))
    print(f"Fetched {len(all_data)} events")
