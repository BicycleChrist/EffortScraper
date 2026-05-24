#!/usr/bin/env python3
"""
ProphetX Exchange Scraper + SGP Scanner

Two responsibilities live here:
  1. Sync orderbook scraping via cash.api.prophetx.co (Bearer auth) —
     GetTournaments, GetEventMarkets, ScrapeAllMarkets, GetBestLines.
  2. Async SGP-quote scanner via www.prophetx.co/parlay (cookie auth) —
     GetSGPQuote/GetSGPQuoteAsync, BuildSGPLegs, SGPScanner. See the comment
     block above class SGPScanner for the implication-chain logic and the
     auto-bet / LiquidityWidget TODOs.

Auth state:
  - Bearer JWT lives in Creds.PROPHETX_AUTH_TOKEN (refreshed by Playwright).
  - Cookie jar is dumped to prophetx_session.json by refresh_prophetx_token
    and read back by load_prophetx_cookies() for the parlay endpoint.

CLI: see argparse block at bottom (--scan toggles scanner mode).

TODO: Automated Token Refresh Integration
----------------------------------------
1. Modify GetTournaments/GetEventMarkets to return 401/403 status distinctly
2. Add PyQt6 QDialog in LiquidityWidget for credential entry + 2FA
3. Split refresh_prophetx_token into:
   - start_login(email, pw) -> opens browser, logs in, returns page object
   - submit_2fa(page, code) -> enters code, captures token, closes browser
4. Run Playwright in QThread to avoid blocking UI
5. On auth error signal, show dialog -> collect creds -> start_login -> prompt 2FA -> submit_2fa -> update token
"""


import pathlib
import requests
import json
import re
import csv
import base64
import threading
import time
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from playwright.sync_api import sync_playwright
from Creds import PROPHETX_AUTH_TOKEN, PROPHETX_EMAIL, PROPHETX_PASSWORD


# ============================================================================
# Bearer-token manager
# ----------------------------------------------------------------------------
# PX issues two JWTs:
#   * accessToken  — short-lived (~hours). Used as Authorization: Bearer.
#   * refreshToken — long-lived (~30 days). Rotates on each refresh call.
#
# Refresh endpoint:
#   POST https://www.prophetx.co/api/v1/auth/extend-session
#   body: {"refreshToken": "<jwt>", "device_id": "<uuid or empty>"}
#   reply: {"accessToken": "...", "refreshToken": "...", "exp": <unix>, ...}
#
# State lives in prophetx_auth_state.json next to this file. Bootstrap:
#   1. If state file exists, use it.
#   2. Otherwise seed access from Creds.PROPHETX_AUTH_TOKEN + refresh from
#      the `refreshToken` cookie in prophetx_session.json.
#   3. If neither is available, downstream calls will fail until the user
#      pastes a fresh localStorage.auth blob into prophetx_auth_state.json.
# ============================================================================

_PX_AUTH_BASE_DIR = pathlib.Path(__file__).parent
_PX_AUTH_STATE_PATH = _PX_AUTH_BASE_DIR / "prophetx_auth_state.json"
_PX_AUTH_SESSION_PATH = _PX_AUTH_BASE_DIR / "prophetx_session.json"
_PX_EXTEND_URL = "https://www.prophetx.co/api/v1/auth/extend-session"

# Refresh proactively this many seconds before hard expiry.
_PX_REFRESH_LEEWAY = 120

# Backoff after a failed refresh. Stops the hot path from re-attempting
# (and re-logging) every request when the server is returning 401 due to
# isOtpExpired or any other persistent error.
_PX_REFRESH_BACKOFF_SECONDS = 120

# In-memory cache of the persisted state. `get_token()` reads from here
# on the hot path — disk + refresh HTTP only fire when the cache is
# explicitly invalidated or `ensure_fresh()` is called. Keeps the market
# scraper from blocking on disk IO every request.
_px_cached_state: Optional[dict] = None
_px_last_refresh_failure_ts: float = 0.0
_px_refresh_in_flight: bool = False

# Concurrent-refresh guard.
_px_token_lock = threading.Lock()


def _px_decode_jwt_exp(jwt: str) -> Optional[int]:
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("exp")) if data.get("exp") is not None else None
    except Exception:
        return None


def _px_load_state_from_disk() -> dict:
    """Read the state file (or bootstrap from Creds + session cookies).
    Hot-path callers should use _px_get_state() instead — it caches and
    avoids the disk hit per call."""
    if _PX_AUTH_STATE_PATH.exists():
        try:
            return json.loads(_PX_AUTH_STATE_PATH.read_text())
        except Exception:
            pass
    state = {"accessToken": None, "refreshToken": None,
             "exp": None, "device_id": ""}
    if PROPHETX_AUTH_TOKEN:
        state["accessToken"] = PROPHETX_AUTH_TOKEN
        state["exp"] = _px_decode_jwt_exp(PROPHETX_AUTH_TOKEN)
    if _PX_AUTH_SESSION_PATH.exists():
        try:
            session = json.loads(_PX_AUTH_SESSION_PATH.read_text())
            for c in session.get("cookies", []):
                if c.get("name") == "refreshToken":
                    state["refreshToken"] = c.get("value")
                    break
        except Exception:
            pass
    _px_save_state(state)
    return state


def _px_get_state() -> dict:
    """Return the in-memory state, loading from disk on first access.
    Caller must hold _px_token_lock when calling this if they intend to
    mutate the result."""
    global _px_cached_state
    if _px_cached_state is None:
        _px_cached_state = _px_load_state_from_disk()
    return _px_cached_state


def reload_state_from_disk() -> None:
    """Drop the in-memory cache so the next get_token() call re-reads
    prophetx_auth_state.json. Call after the user manually pastes a fresh
    localStorage.auth blob so the widget picks it up without a restart."""
    global _px_cached_state
    with _px_token_lock:
        _px_cached_state = None


def _px_save_state(state: dict) -> None:
    try:
        _PX_AUTH_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"[prophetx_token] WARN: failed to persist state: {e}")


def _px_persist_access_to_creds(access_token: str) -> None:
    """Mirror the freshly-issued access token back to Creds.py so other
    modules that read it at import time stay in sync. Best-effort."""
    creds_path = _PX_AUTH_BASE_DIR / "Creds.py"
    try:
        content = creds_path.read_text()
        new = re.sub(
            r'PROPHETX_AUTH_TOKEN\s*=\s*"[^"]*"',
            f'PROPHETX_AUTH_TOKEN = "{access_token}"',
            content, count=1,
        )
        if new != content:
            creds_path.write_text(new)
    except Exception as e:
        print(f"[prophetx_token] WARN: failed to update Creds.py: {e}")


def _px_refresh_headers(state: dict) -> dict:
    """Headers for extend-session. The endpoint requires the (possibly
    soon-stale) accessToken as Bearer too — refresh token alone isn't
    sufficient; that's how the SPA does it."""
    h = {
        "Content-Type": "application/json",
        "Origin": "https://www.prophetx.co",
        "Referer": "https://www.prophetx.co/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) "
                      "Gecko/20100101 Firefox/151.0",
        "Accept": "application/json, text/plain, */*",
        "__source": "web",
        "X-Currency": "cash",
    }
    if state.get("accessToken"):
        h["Authorization"] = f"Bearer {state['accessToken']}"
    return h


def _px_needs_refresh(state: dict) -> bool:
    if not state.get("accessToken"):
        return True
    exp = state.get("exp")
    if not exp:
        return True
    return exp - time.time() < _PX_REFRESH_LEEWAY


def _px_do_refresh_sync(state: dict) -> dict:
    refresh = state.get("refreshToken")
    if not refresh:
        raise RuntimeError(
            "no refreshToken available — bootstrap prophetx_auth_state.json "
            "from a logged-in browser's localStorage.auth")
    body = {"refreshToken": refresh, "device_id": state.get("device_id") or ""}
    r = requests.post(_PX_EXTEND_URL, json=body,
                      headers=_px_refresh_headers(state), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(
            f"extend-session HTTP {r.status_code}: {r.text[:200]}")
    blob = r.json()
    data = blob.get("data") if isinstance(blob, dict) and "data" in blob else blob
    access = data.get("accessToken")
    if not access:
        raise RuntimeError(f"extend-session: no accessToken in {blob}")
    state["accessToken"] = access
    state["refreshToken"] = data.get("refreshToken") or refresh
    exp = data.get("exp") or _px_decode_jwt_exp(access)
    state["exp"] = int(exp) if exp else None
    _px_save_state(state)
    _px_persist_access_to_creds(access)
    return state


def _px_kick_background_refresh() -> None:
    """Fire-and-forget refresh on a daemon thread. Coalesces concurrent
    requests (only one refresh in flight at a time) and applies a
    backoff after failures so a persistent server-side rejection (e.g.
    isOtpExpired=true) doesn't log-spam or stampede the endpoint."""
    global _px_refresh_in_flight, _px_last_refresh_failure_ts
    with _px_token_lock:
        if _px_refresh_in_flight:
            return
        if (time.time() - _px_last_refresh_failure_ts
                < _PX_REFRESH_BACKOFF_SECONDS):
            return
        state = _px_get_state()
        if not _px_needs_refresh(state):
            return
        _px_refresh_in_flight = True
        snap = dict(state)  # work on a copy so the lock can be released

    def _run():
        global _px_refresh_in_flight, _px_last_refresh_failure_ts, _px_cached_state
        try:
            _px_do_refresh_sync(snap)
            with _px_token_lock:
                _px_cached_state = snap
                _px_last_refresh_failure_ts = 0.0
        except Exception as e:
            # One log line per backoff window — chatty enough to notice,
            # not enough to spam.
            print(f"[prophetx_token] refresh failed, using cached "
                  f"(retry in {_PX_REFRESH_BACKOFF_SECONDS}s): {e}")
            with _px_token_lock:
                _px_last_refresh_failure_ts = time.time()
        finally:
            with _px_token_lock:
                _px_refresh_in_flight = False

    threading.Thread(target=_run, name="px-token-refresh",
                     daemon=True).start()


def get_token() -> str:
    """Return the cached bearer. Cheap — no disk IO, no HTTP — safe to
    call on hot paths (every market request). If the cached token is
    stale, a background refresh is kicked off but we still return the
    current (about-to-expire or expired) token; downstream calls will
    surface a 401 if it's truly dead. Use _px_get_with_retry to recover
    on that."""
    with _px_token_lock:
        state = _px_get_state()
        stale = _px_needs_refresh(state)
        token = state.get("accessToken") or ""
    if stale:
        _px_kick_background_refresh()
    return token


async def get_token_async() -> str:
    """Awaitable variant — same cheap path as get_token(). The refresh
    runs on a background thread (not the calling event loop) so QThread-
    hosted loops never block on HTTP."""
    return get_token()


def invalidate_token() -> None:
    """Force the next get_token call to refresh. Called by the 401-retry
    helper when a data endpoint rejects the cached token despite its exp
    claim looking valid. Also clears the failure-backoff so the refresh
    actually fires."""
    global _px_last_refresh_failure_ts, _px_cached_state
    with _px_token_lock:
        if _px_cached_state is not None:
            _px_cached_state["exp"] = 0
        _px_last_refresh_failure_ts = 0.0


# ============================================================================
# www.prophetx.co/api request helpers
# ----------------------------------------------------------------------------
# /api/v1/wallet, /api/v2/transaction/wagers/cursor, /parlay/api/v1/user/list
# all live on the www. host and require the Bearer token. Wrap calls in a
# 401-retry that invalidates the token cache and tries once more so a stale
# bearer recovers without surfacing to the UI (when refresh works at all).
# ============================================================================

_PX_WWW_BASE = "https://www.prophetx.co"


async def _px_www_headers() -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) "
                      "Gecko/20100101 Firefox/151.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {await get_token_async()}",
        "Origin": "https://www.prophetx.co",
        "Referer": "https://www.prophetx.co/",
        "__source": "web",
        "X-Currency": "cash",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
    }


async def _px_get_with_retry(session: aiohttp.ClientSession, url: str,
                             *, params: Optional[dict] = None) -> dict:
    """GET helper that retries once on 401, invalidating the token cache
    in between so the second attempt forces a refresh."""
    for attempt in (1, 2):
        async with session.get(url, headers=await _px_www_headers(),
                               params=params) as r:
            if r.status == 401 and attempt == 1:
                invalidate_token()
                continue
            r.raise_for_status()
            return await r.json()
    raise RuntimeError("unreachable")


# --- PX wallet / wagers / parlays --------------------------------------

async def fetch_px_wallet(session: aiohttp.ClientSession) -> dict:
    """Account balance snapshot. Once-per-session is fine."""
    body = await _px_get_with_retry(session, f"{_PX_WWW_BASE}/api/v1/wallet")
    return body.get("data") or {}


async def fetch_px_open_wagers(session: aiohttp.ClientSession) -> List[dict]:
    """Open single-bet wagers (partially or fully matched) over the last
    year. Returns the raw row list — caller normalizes."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    params = {
        "cursor": "",
        "status": "open",
        "matchingStatus": "partially_matched,fully_matched",
        "dateFrom": date_from,
        "sortField": "placed_date:desc",
        "limit": 50,
        "group": 1,
    }
    body = await _px_get_with_retry(
        session, f"{_PX_WWW_BASE}/api/v2/transaction/wagers/cursor",
        params=params)
    return body.get("data") or []


async def fetch_px_open_parlays(session: aiohttp.ClientSession) -> List[dict]:
    """Open parlay orders from the My Plays → Parlays tab."""
    params = {"limit": 20, "type": "confirmed", "settlementType": "pending"}
    body = await _px_get_with_retry(
        session, f"{_PX_WWW_BASE}/parlay/api/v1/user/list", params=params)
    return ((body or {}).get("data") or {}).get("orders") or []


def refresh_prophetx_token(email: str = PROPHETX_EMAIL, password: str = PROPHETX_PASSWORD, headless: bool = True) -> Optional[str]:
    """
    Refresh ProphetX JWT token via Playwright. Updates Creds.py automatically.
    Requires: pip install playwright && playwright install chromium
    """
    if not email or not password:
        print("[!] Set PROPHETX_EMAIL and PROPHETX_PASSWORD in Creds.py")
        return None

    token = None

    def capture_token(response):
        nonlocal token
        auth = response.request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and auth.count(".") == 2 and len(auth) > 100:
            token = auth[7:]
            print(f"[*] Captured token from {response.url[:50]}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.on("response", capture_token)

        try:
            page.goto("https://www.prophetx.co/?currency=cash", wait_until="load")
            page.wait_for_timeout(3000)
            # Dismiss promo popup - try close button, X, or Escape
            for selector in ["button[aria-label='Close']", "button.close", "[class*='close']", "svg[class*='close']"]:
                if page.locator(selector).first.is_visible():
                    page.locator(selector).first.click()
                    break
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
            # Click the Sign In button (btn--plain class)
            page.click("button.btn--plain:has-text('Sign In')")
            page.wait_for_selector("input#email", timeout=5000)
            page.fill("input#email", email)
            page.fill("input#password", password)
            page.click("button:has-text('Login')")
            page.wait_for_timeout(3000)

            # Handle 2FA if prompted
            otp_input = page.locator("input[placeholder='Enter Code']")
            if otp_input.is_visible():
                code = input("[?] Enter 2FA code from SMS: ").strip()
                otp_input.fill(code)
                page.click("button:has-text('Proceed')")
                page.wait_for_timeout(5000)

            if token:
                creds_path = pathlib.Path(__file__).parent / "Creds.py"
                content = creds_path.read_text()
                new_content = re.sub(r'PROPHETX_AUTH_TOKEN\s*=\s*"[^"]*"', f'PROPHETX_AUTH_TOKEN = "{token}"', content)
                creds_path.write_text(new_content)
                print(f"[+] ProphetX token refreshed")

            # Save full browser storage_state (cookies + localStorage) for the
            # parlay service which authenticates by cookie, not bearer token.
            state_path = pathlib.Path(__file__).parent / "prophetx_session.json"
            context.storage_state(path=str(state_path))
            print(f"[+] ProphetX session state saved to {state_path.name}")
        except Exception as e:
            print(f"[!] Token refresh failed: {e}")
        finally:
            browser.close()

    return token

# Be careful to avoid Auth Token containing Elipsys; could be dependent on text editor one is using

BASE_URL = "https://cash.api.prophetx.co/trade/public/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "Accept": "application/json, text/plain, */*",
    "__source": "web",
    "X-Currency": "cash",
    "Authorization": f"Bearer {PROPHETX_AUTH_TOKEN}",
}


def GetTournaments(type_filter: str = "highlight", limit: int = 150) -> Tuple[Optional[Dict], Optional[requests.Response]]:
    """
    Fetch all tournaments with their events.

    Args:
        type_filter: Tournament type filter (default: 'highlight')
        limit: Maximum number of results (default: 150)

    Returns:
        Tuple of (response_data, response_object)
    """
    url = f"{BASE_URL}/v1/tournaments"
    params = {
        "expand": "events",
        "type": type_filter,
        "limit": limit
    }

    try:
        response = requests.get(url, headers=HEADERS, params=params)
        # Manually decode content as UTF-8 to avoid encoding issues
        data = json.loads(response.content.decode('utf-8'))
        return data, response
    except Exception as e:
        print(f"ERROR: Failed to fetch tournaments: {e}")
        return None, None


def GetEventMarkets(event_id: int) -> Tuple[Optional[Dict], Optional[requests.Response]]:
    """
    Fetch all markets (orderbook) for a specific event.

    Args:
        event_id: The event ID to fetch markets for

    Returns:
        Tuple of (response_data, response_object)
    """
    url = f"{BASE_URL}/v2/events/{event_id}/markets"

    try:
        response = requests.get(url, headers=HEADERS)

        # Check if response is valid before parsing JSON
        if response.status_code != 200:
            return None, response

        if not response.content.strip():
            # Empty response
            return None, response

        # Manually decode content as UTF-8 to avoid encoding issues
        data = json.loads(response.content.decode('utf-8'))
        return data, response
    except json.JSONDecodeError as e:
        # Invalid JSON response
        return None, response if 'response' in locals() else None
    except Exception as e:
        print(f"ERROR: Failed to fetch markets for event {event_id}: {e}")
        return None, None


def SaveResponse(filename: str, content: Dict, subdirectory: str = "prophetx_dumps"):
    """Save API response to JSON file."""
    cwd = pathlib.Path.cwd()
    savedir = cwd / subdirectory
    if not savedir.exists():
        savedir.mkdir()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dumpfile = savedir / f"{filename}_{timestamp}.json"
    print(f"Saving to: {dumpfile}")

    with dumpfile.open('w', encoding="utf-8") as f:
        json.dump(content, f, indent=2)
    return dumpfile


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


def ScrapeAllMarkets(save_individual: bool = False, save_combined: bool = True) -> Dict:
    """
    Complete scrape: fetch all tournaments, events, and their markets.

    Args:
        save_individual: Save each event's markets to separate file
        save_combined: Save all results to single combined file

    Returns:
        Dictionary mapping event_id -> market_data
    """
    print("=" * 60)
    print("ProphetX Exchange Full Scrape")
    print("=" * 60)

    # Step 1: Get all tournaments and events
    print("\n[1/2] Fetching tournaments and events...")
    tournaments_data, response = GetTournaments()

    if not tournaments_data or response.status_code != 200:
        print(f"ERROR: Failed to fetch tournaments (status: {response.status_code if response else 'N/A'})")
        return {}

    events = ExtractEventList(tournaments_data)
    print(f"Found {len(events)} events across all tournaments\n")

    # Save tournaments data
    SaveResponse("tournaments", tournaments_data)

    # Step 2: Fetch markets for each event
    print(f"[2/2] Fetching markets for {len(events)} events...")
    all_markets = {}

    for i, event in enumerate(events, 1):
        event_id = event['id']
        event_name = event['name']

        print(f"  [{i}/{len(events)}] {event_name} (ID: {event_id})...", end=" ")

        markets_data, response = GetEventMarkets(event_id)

        if not markets_data:
            status = response.status_code if response else 'N/A'
            print(f"SKIPPED (no markets, status: {status})")
            continue

        if response.status_code != 200:
            print(f"FAILED (status: {response.status_code})")
            continue

        num_markets = len(markets_data.get('data', {}).get('markets', []))
        total_stake = sum(m.get('totalStake', 0) for m in markets_data.get('data', {}).get('markets', []))
        print(f"OK ({num_markets} markets, ${total_stake:,.0f} total stake)")

        # Add event metadata to markets data
        markets_data['event_metadata'] = event
        all_markets[event_id] = markets_data

        # Save individual event markets
        if save_individual:
            SaveResponse(f"event_{event_id}_{event_name.replace(' ', '_')}", markets_data)

    print(f"\nSuccessfully scraped {len(all_markets)}/{len(events)} events")

    # Save combined results
    if save_combined and all_markets:
        combined_file = SaveResponse("all_markets_combined", all_markets)
        print(f"\nCombined data saved to: {combined_file}")

    print("=" * 60)
    return all_markets


def GetBestLines(markets_data: Dict) -> Dict:
    """
    Extract best available lines from market data.

    Args:
        markets_data: Raw markets data from GetEventMarkets()

    Returns:
        Dictionary with best lines for each market type
    """
    best_lines = {}

    if 'data' not in markets_data or 'markets' not in markets_data['data']:
        return best_lines

    for market in markets_data['data']['markets']:
        market_name = market['name']
        market_type = market['type']

        # For simple markets (moneyline)
        if 'selections' in market and market['selections']:
            selections_summary = []
            for side in market['selections']:
                if side and len(side) > 0:
                    best = side[0]  # First selection is best price
                    selections_summary.append({
                        'name': best.get('displayName'),
                        'odds': best.get('displayOdds'),
                        'value': best.get('value'),
                        'stake': best.get('stake'),
                    })

            best_lines[market_name] = {
                'type': market_type,
                'status': market['status'],
                'totalStake': market.get('totalStake', 0),
                'selections': selections_summary
            }

        # For markets with multiple lines (spread, total)
        elif 'marketLines' in market:
            favourite_line = None
            for ml in market['marketLines']:
                if ml.get('favourite'):
                    favourite_line = ml
                    break

            if not favourite_line and market['marketLines']:
                favourite_line = market['marketLines'][0]

            if favourite_line:
                line_summary = []
                for side in favourite_line.get('selections', []):
                    if side and len(side) > 0:
                        best = side[0]
                        line_summary.append({
                            'name': best.get('displayName'),
                            'odds': best.get('displayOdds'),
                            'value': best.get('value'),
                            'stake': best.get('stake'),
                        })

                best_lines[market_name] = {
                    'type': market_type,
                    'status': market['status'],
                    'totalStake': market.get('totalStake', 0),
                    'mainLine': favourite_line.get('name'),
                    'selections': line_summary
                }

    return best_lines


# ============================================================================
# SGP (Same-Game Parlay) Quotes
#
# Endpoint:  POST https://www.prophetx.co/parlay/public/api/v1/user/request
# Auth:      session cookie (NOT the cash.api Bearer token).
#            Cookies are persisted by refresh_prophetx_token via Playwright
#            storage_state -> prophetx_session.json.
# Body:      {"marketLines":[{sportEventId,marketId,outcomeId,lineId,line},...],
#             "stake": <float>}
# Response:  data.offers[] -- multiple stake tiers, each with combined `odds`
#            (American) and per-leg `estimatedPrices`.
# ============================================================================

PARLAY_URL = "https://www.prophetx.co/parlay/public/api/v1/user/request"
SESSION_STATE_PATH = pathlib.Path(__file__).parent / "prophetx_session.json"

PARLAY_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "__source": "web",
    "X-Currency": "cash",
    "Origin": "https://www.prophetx.co",
}


def load_prophetx_cookies() -> Dict[str, str]:
    """Load cookies from Playwright storage_state into a {name: value} dict."""
    if not SESSION_STATE_PATH.exists():
        raise FileNotFoundError(
            f"{SESSION_STATE_PATH.name} not found. Run refresh_prophetx_token() first "
            f"to populate the session state."
        )
    state = json.loads(SESSION_STATE_PATH.read_text())
    return {c["name"]: c["value"] for c in state.get("cookies", [])
            if "prophetx.co" in c.get("domain", "")}


async def GetSGPQuoteAsync(session: aiohttp.ClientSession,
                           market_lines: List[Dict],
                           stake: float = 1.0) -> Optional[Dict]:
    """Async SGP quote. Session must carry the cookie jar (cookies kwarg)."""
    body = {"marketLines": market_lines, "stake": stake}
    try:
        async with session.post(PARLAY_URL, headers=PARLAY_HEADERS, json=body) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
            if not text.strip():
                return None
            return json.loads(text)
    except Exception as e:
        print(f"ERROR: SGP quote failed: {e}")
        return None


# Single-leg market-order endpoint. Distinct from the parlay /confirm path:
# this places a direct take against a specific orderbook line. Cookie auth
# is enough (mirrors the rest of the prophetx.co paths).
MARKET_ORDERS_URL = "https://www.prophetx.co/trade/private/api/v1/market-orders"


async def PlaceMarketOrderAsync(session: aiohttp.ClientSession,
                                line_id: str,
                                american_odds: int,
                                stake: float,
                                expected_avg_odds: Optional[int] = None,
                                ) -> Optional[Dict]:
    """Take a resting offer on ProphetX.

    Args:
        session: aiohttp session with the prophetx.co cookie jar
        line_id: target orderbook line (`lineID` on the selection dict)
        american_odds: the price the trader is willing to accept; the
            server may fill across multiple price levels if size at the
            named odds is exhausted.
        stake: dollar stake (server-side dollars, not centi-units)
        expected_avg_odds: drift guard. If the avg fill odds drift worse
            than this, the server rejects. Defaults to american_odds.

    Returns the order response (status, fill info) or None on transport
    / non-200 failure.
    """
    body = {
        "lineId": line_id,
        "oddsList": [int(american_odds)],
        "expectedAverageOdds": int(expected_avg_odds
                                   if expected_avg_odds is not None
                                   else american_odds),
        "stake": float(stake),
    }
    # /market-orders needs Bearer auth in addition to the cookie jar —
    # /confirm accepts cookie-only, but the private trade endpoints
    # require both (401 otherwise).
    headers = dict(PARLAY_HEADERS)
    try:
        from Creds import PROPHETX_AUTH_TOKEN as _tok
        if _tok:
            headers["Authorization"] = f"Bearer {_tok}"
    except ImportError:
        pass
    try:
        async with session.post(MARKET_ORDERS_URL, headers=headers,
                                json=body) as resp:
            text = await resp.text()
            if resp.status not in (200, 201) or not text.strip():
                print(f"ERROR: ProphetX market-orders HTTP {resp.status}: "
                      f"{text[:300]}")
                return None
            return json.loads(text)
    except Exception as e:
        print(f"ERROR: ProphetX market-order failed: {e}")
        return None


# Placement endpoint — different host/path than the quote. Captured live: the
# web app POSTs {odds, parlayId, stake} here after the user confirms an
# offer tier from the quote response. Auth piggybacks on the prophetx.co
# cookie jar; PARLAY_HEADERS already carries the rest.
CONFIRM_URL = "https://www.prophetx.co/confirm"


async def PlaceSGPAsync(session: aiohttp.ClientSession,
                        parlay_id: str,
                        odds: int,
                        stake: float) -> Optional[Dict]:
    """Confirm and place a previously-quoted ProphetX SGP.

    Args:
        session: aiohttp session carrying the prophetx.co cookie jar
            (same one used for GetSGPQuoteAsync)
        parlay_id: `data.parlayId` from the quote response
        odds: American odds of the chosen `data.offers[i]` tier
        stake: dollar stake matching that offer tier

    Returns the parsed JSON response on success
    ({"data": {"parlayId", "createdAt"}, "success": True}), or None on
    transport / non-200 failure. Caller is responsible for sanity-checking
    the stake against the offer tier before invoking this — server will
    reject mismatches.
    """
    body = {"odds": int(odds), "parlayId": parlay_id, "stake": stake}
    try:
        async with session.post(CONFIRM_URL, headers=PARLAY_HEADERS,
                                json=body) as resp:
            text = await resp.text()
            if resp.status != 200 or not text.strip():
                print(f"ERROR: ProphetX /confirm HTTP {resp.status}: "
                      f"{text[:300]}")
                return None
            return json.loads(text)
    except Exception as e:
        print(f"ERROR: ProphetX SGP placement failed: {e}")
        return None


def GetSGPQuote(market_lines: List[Dict], stake: float = 1.0) -> Tuple[Optional[Dict], Optional[requests.Response]]:
    """
    Request a same-game parlay quote from ProphetX.

    Args:
        market_lines: list of leg dicts, each with keys
            sportEventId, marketId, outcomeId, lineId, line
        stake: requested stake (server returns multiple offers at different tiers)

    Returns:
        (response_data, response_object). On success, response_data has shape:
            {"success": True, "data": {"parlayId", "parlayRequestId",
              "offers": [{"odds": <american>, "stake": <float>,
                          "estimatedPrices": [{"lineId","odds"},...]}, ...]}}
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "__source": "web",
        "X-Currency": "cash",
        "Origin": "https://www.prophetx.co",
    }
    body = {"marketLines": market_lines, "stake": stake}
    try:
        cookies = load_prophetx_cookies()
        response = requests.post(PARLAY_URL, headers=headers, cookies=cookies, json=body)
        if response.status_code != 200 or not response.content.strip():
            return None, response
        data = json.loads(response.content.decode("utf-8"))
        return data, response
    except Exception as e:
        print(f"ERROR: SGP quote failed: {e}")
        return None, None


def BuildSGPLegs(markets_data: Dict, picks: List[Tuple[int, int]]) -> List[Dict]:
    """
    Translate (market_id, selection_id) picks into the leg shape required by
    GetSGPQuote, using the existing GetEventMarkets payload as the source of
    sportEventId / lineId / line.

    Args:
        markets_data: response from GetEventMarkets()
        picks: list of (market_id, selection_id) tuples. selection_id matches
               selection['id'] (typically 12 = over, 13 = under, etc.)

    Returns:
        List of leg dicts ready for GetSGPQuote(market_lines=...)
    """
    legs: List[Dict] = []
    markets = markets_data.get("data", {}).get("markets", []) if markets_data else []
    by_id = {m.get("id"): m for m in markets}

    for market_id, selection_id in picks:
        market = by_id.get(market_id)
        if not market:
            print(f"[!] market_id {market_id} not in markets_data")
            continue
        sport_event_id = market.get("sportEventId")

        # Walk marketLines -> selections (list-of-lists) and outcomes to find
        # the selection whose id matches. Player-prop markets carry lineID on
        # the selection itself; line-based markets (spread/total) carry it on
        # the outcome entry of the favourite marketLine.
        found = None
        for ml in market.get("marketLines") or []:
            for side in ml.get("selections") or []:
                if not isinstance(side, list):
                    continue
                for sel in side:
                    if sel and sel.get("id") == selection_id:
                        found = (ml, sel)
                        break
                if found:
                    break
            if found:
                break

        if not found:
            # Fall back to top-level selections (markets without marketLines)
            for side in market.get("selections") or []:
                if not isinstance(side, list):
                    continue
                for sel in side:
                    if sel and sel.get("id") == selection_id:
                        found = (None, sel)
                        break
                if found:
                    break

        if not found:
            print(f"[!] selection_id {selection_id} not found in market {market_id}")
            continue

        _, sel = found
        legs.append({
            "sportEventId": sport_event_id,
            "marketId": market_id,
            "outcomeId": sel.get("id"),
            "lineId": sel.get("lineID"),  # source uses uppercase ID
            "line": sel.get("line"),
        })

    return legs


# ============================================================================
# SGP Implication Scanner (async)
#
# Hunts mispriced SGPs where legs are deterministically implied by an anchor
# leg (so fair SGP odds == anchor odds, but the pricer often pays more).
#
# Implication chains anchored on HR Over 0.5:
#   HR  =>  RBI  >= 1   (over 0.5)
#   HR  =>  Run  >= 1
#   HR  =>  Hit  >= 1
#   HR  =>  HRR  >= 3   (Hits + Runs + RBIs; HR contributes 3)
#   HR  =>  TB   >= 4   (any TB Over line <= 3.5 is satisfied)
#
# Concurrency: per-player coroutine, capped by an asyncio.Semaphore.
# The first chain (RBI O0.5) is a probe -- if its edge is <= 0, the rest of
# the player's chains are skipped (the misprice is per-player, not per-chain).
#
# ----------------------------------------------------------------------------
# TODO: Auto bet placement
# ----------------------------------------------------------------------------
# /parlay/public/api/v1/user/request returns ONLY a quote
# ({parlayId, parlayRequestId, offers}). Placing requires a second POST that
# fires when the user clicks "Confirm Play". To capture it:
#   1. Firefox DevTools -> Network, filter "prophetx.co".
#   2. Build SGP, click Confirm Play, watch for the place-time POST.
#   3. Grab URL + JSON body + headers (Request tab).
# Then add PlaceSGPAsync(session, parlay_id, parlay_request_id, stake, ...)
# and wrap into auto_fire() that:
#   - pulls scanner-flagged rows above an EV threshold
#   - re-quotes each row immediately before placing (orderbook drift guard;
#     abort if live edge < placement threshold)
#   - sizes stake = min(live_offer.stake, per_ticket_cap)
#   - posts the place call, parses ack
#   - logs (timestamp, legs, quoted_odds, live_odds, stake, ack, settle)
#     to a sqlite db so we can audit limit-flagging / voids over time
#   - varies cadence (jittered sleep) and per-ticket size to avoid an
#     obvious pure-arb fingerprint -- account longevity > per-bet EV.
#
# ----------------------------------------------------------------------------
# TODO: LiquidityWidget integration
# ----------------------------------------------------------------------------
# Feed flagged rows into LiquidityWidget as a new tab/panel. Per-row UI:
#   player | chain | hr_odds | sgp_odds | edge | stake | [Place $N]
# Clicking Place triggers re-quote + PlaceSGPAsync. Status column shows
# live edge vs. snapshot edge so it's obvious when the book has moved.
# A "Max EV" auto-mode toggle would walk top-N rows and fire each (still
# respecting per-ticket cap and a global per-session bankroll cap).
# ============================================================================


class SGPScanner:
    """Async SGP implication-chain scanner. See module-level comment block above
    for the chain logic and auto-bet / widget integration TODOs. Entry point:
    `await SGPScanner(concurrency=N).scan(event_ids=None)` returns a list of
    row dicts; pair with `annotate_and_save_scan()` for EV ranking + CSV.
    """

    CHAIN_SUFFIXES = {
        "hr":  " Total Home Runs",
        "rbi": " Total RBIs",
        "run": " Total Runs",
        "hit": " Total Hits",
        "tb":  " Total Bases",
        "hrr": " Total Hits, Runs & RBIs",
    }
    # (chain_key, max_line_inclusive). First entry is the probe.
    IMPLIED_CHAINS = [
        ("rbi", 0.5),  # PROBE
        ("run", 0.5),
        ("hit", 0.5),
        ("hrr", 0.5),
        ("tb",  3.5),
    ]
    PROBE_THRESHOLD = 0.0

    def __init__(self, concurrency: int = 6):
        self.sem = asyncio.Semaphore(concurrency)
        self.cookies = load_prophetx_cookies()

    @staticmethod
    def amer_to_dec(a: int) -> float:
        return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)

    @staticmethod
    def find_over_at_line(market: Dict, line: float) -> Optional[Dict]:
        for ml in market.get("marketLines") or []:
            for side in ml.get("selections") or []:
                if not isinstance(side, list):
                    continue
                for sel in side:
                    if not sel:
                        continue
                    if "over" not in (sel.get("name") or "").lower():
                        continue
                    if abs((sel.get("line") or -999) - line) < 1e-6:
                        return sel
        return None

    @staticmethod
    def all_overs_up_to(market: Dict, max_line: float) -> List[Dict]:
        out: List[Dict] = []
        for ml in market.get("marketLines") or []:
            for side in ml.get("selections") or []:
                if not isinstance(side, list):
                    continue
                for sel in side:
                    if not sel or "over" not in (sel.get("name") or "").lower():
                        continue
                    line = sel.get("line")
                    if line is None or line > max_line:
                        continue
                    if not sel.get("stake"):
                        continue
                    out.append(sel)
        seen = set()
        uniq = []
        for s in out:
            lid = s.get("lineID")
            if lid in seen:
                continue
            seen.add(lid)
            uniq.append(s)
        return uniq

    @classmethod
    def index_player_markets(cls, markets: List[Dict]) -> Dict[str, Dict[str, Dict]]:
        idx: Dict[str, Dict[str, Dict]] = {}
        for m in markets:
            name = m.get("name") or ""
            for key, suffix in cls.CHAIN_SUFFIXES.items():
                if name.endswith(suffix):
                    player = name[: -len(suffix)]
                    idx.setdefault(player, {})[key] = m
                    break
        return idx

    def build_row(self, event_name, player, chain_key, line, hr_odds, hr_dec,
                  implied_over, sgp_odds, sgp_stake,
                  event_id=None, legs=None) -> Dict:
        sgp_dec = self.amer_to_dec(sgp_odds)
        edge_dec = sgp_dec - hr_dec
        edge_pct = (sgp_dec / hr_dec - 1) * 100 if hr_dec else 0
        return {
            "event": event_name,
            "event_id": event_id,
            "player": player,
            "chain": f"HR + {chain_key.upper()} O{line}",
            "hr_odds": hr_odds,
            "implied_odds": implied_over.get("odds"),
            "implied_stake": implied_over.get("stake"),
            "sgp_odds": sgp_odds,
            "sgp_stake": sgp_stake,
            "edge_decimal": round(edge_dec, 2),
            "edge_pct": round(edge_pct, 1),
            # Placement metadata: the leg list the bet slip re-quotes and
            # confirms with. None when the scanner runs without keeping
            # legs around (legacy CLI path).
            "legs": legs,
        }

    @staticmethod
    def print_row(r: Dict):
        tag = "★" if r["edge_decimal"] > 0 else " "
        print(f"  {tag} {r['player']:25s} {r['chain']:20s} "
              f"HR {r['hr_odds']:+5d}  SGP {r['sgp_odds']:+6d}  "
              f"stake ${r['sgp_stake']:>7.2f}  edge {r['edge_decimal']:+6.2f} "
              f"({r['edge_pct']:+.1f}%)")

    async def quote_one(self, session, markets_data, hr_market, implied_market,
                        implied_over, stake=1.0):
        legs = BuildSGPLegs(markets_data, [
            (hr_market["id"], 12),
            (implied_market["id"], 12),
        ])
        if len(legs) != 2:
            return None
        legs[1]["line"] = implied_over.get("line")
        legs[1]["lineId"] = implied_over.get("lineID")
        async with self.sem:
            quote = await GetSGPQuoteAsync(session, legs, stake=stake)
        if not quote or not quote.get("success"):
            return None
        offers = quote["data"].get("offers") or []
        if not offers:
            return None
        o = offers[0]
        if o.get("odds") is None:
            return None
        return o.get("odds"), o.get("stake"), legs

    async def scan_player(self, session, markets_data, event_name, player, chains,
                          event_id=None) -> List[Dict]:
        hr_market = chains.get("hr")
        if not hr_market:
            return []
        hr_over = self.find_over_at_line(hr_market, 0.5)
        if not hr_over or not hr_over.get("stake"):
            return []
        hr_odds = hr_over.get("odds")
        hr_dec = self.amer_to_dec(hr_odds)

        rows: List[Dict] = []

        # Probe
        probe_key, probe_line = self.IMPLIED_CHAINS[0]
        probe_market = chains.get(probe_key)
        if not probe_market:
            return []
        probe_over = self.find_over_at_line(probe_market, probe_line)
        if not probe_over or not probe_over.get("stake"):
            return []
        probe_res = await self.quote_one(session, markets_data, hr_market,
                                         probe_market, probe_over)
        if probe_res is None:
            return []
        probe_odds, probe_stake, probe_legs = probe_res
        probe_row = self.build_row(event_name, player, probe_key,
                                   probe_over.get("line"), hr_odds, hr_dec,
                                   probe_over, probe_odds, probe_stake,
                                   event_id=event_id, legs=probe_legs)
        rows.append(probe_row)
        self.print_row(probe_row)
        if probe_row["edge_decimal"] <= self.PROBE_THRESHOLD:
            return rows

        # Fan-out
        tasks = []
        leg_meta = []
        for chain_key, max_line in self.IMPLIED_CHAINS[1:]:
            implied_market = chains.get(chain_key)
            if not implied_market:
                continue
            for implied_over in self.all_overs_up_to(implied_market, max_line):
                tasks.append(self.quote_one(session, markets_data, hr_market,
                                            implied_market, implied_over))
                leg_meta.append((chain_key, implied_over))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (chain_key, implied_over), res in zip(leg_meta, results):
            if isinstance(res, Exception) or res is None:
                continue
            sgp_odds, sgp_stake, fan_legs = res
            row = self.build_row(event_name, player, chain_key,
                                 implied_over.get("line"), hr_odds, hr_dec,
                                 implied_over, sgp_odds, sgp_stake,
                                 event_id=event_id, legs=fan_legs)
            rows.append(row)
            self.print_row(row)
        return rows

    async def scan_event(self, session, event_id, event_name) -> List[Dict]:
        # Event-markets fetch goes through the async path so the scan never
        # blocks the event loop — required when the scanner runs as a task
        # on the widget's qasync loop instead of in a dedicated thread.
        # FetchSingleEventAsync manages its own bearer-auth session; the
        # `session` arg here is the cookie-auth session used only for the
        # SGP quote requests (different host, different auth scheme).
        # Imported locally so ProphetXQuery's CLI path doesn't pull in
        # prophetx_async's PyQt/qasync dependency at module load.
        from prophetx_async import FetchSingleEventAsync
        markets_data = await FetchSingleEventAsync(event_id)
        if not markets_data or not markets_data.get("data"):
            print(f"  [skip] {event_name}: no markets")
            return []
        by_player = self.index_player_markets(markets_data["data"]["markets"])
        tasks = [self.scan_player(session, markets_data, event_name, p, c,
                                  event_id=event_id)
                 for p, c in by_player.items()]
        nested = await asyncio.gather(*tasks)
        return [r for sub in nested for r in sub]

    async def scan(self, event_ids: Optional[List[int]] = None) -> List[Dict]:
        if event_ids:
            events = [{"id": eid, "name": f"event_{eid}"} for eid in event_ids]
        else:
            data, _ = GetTournaments()
            events = ExtractEventList(data or {})
            events = [e for e in events if e.get("status") == "not_started"]
            print(f"Scanning {len(events)} not_started events")

        all_rows: List[Dict] = []
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(cookies=self.cookies, timeout=timeout) as session:
            for ev in events:
                print(f"\n=== {ev['name']} (id={ev['id']}) ===")
                rows = await self.scan_event(session, ev["id"], ev["name"])
                all_rows.extend(rows)
        return all_rows


def annotate_and_save_scan(rows: List[Dict], cap: float, min_edge: float,
                           out_path: str) -> List[Dict]:
    """Add ev_at_cap, filter, sort, write CSV. Returns sorted+filtered rows."""
    for r in rows:
        usable = min(r["sgp_stake"] or 0, cap)
        r["ev_at_cap"] = round(r["edge_decimal"] * usable, 2)
    flagged = [r for r in rows if r["edge_decimal"] >= min_edge]
    flagged.sort(key=lambda r: r["ev_at_cap"], reverse=True)
    if flagged:
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(flagged[0].keys()))
            w.writeheader()
            w.writerows(flagged)
        print(f"\nSaved {len(flagged)} rows to {out_path}")
    return flagged


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ProphetX Exchange Scraper")
    parser.add_argument("--event-id", type=int, help="Scrape/scan specific event ID only")
    parser.add_argument("--save-individual", action="store_true", help="Save individual event files (default: only combined)")
    parser.add_argument("--no-combined", action="store_true", help="Don't save combined file")
    parser.add_argument("--best-lines", action="store_true", help="Extract and display best lines only")
    parser.add_argument("--scan", action="store_true", help="Run SGP implication scanner instead of orderbook scrape")
    parser.add_argument("--concurrency", type=int, default=6, help="Max concurrent SGP requests (scanner)")
    parser.add_argument("--cap", type=float, default=100.0, help="Stake cap for EV calc (scanner)")
    parser.add_argument("--min-edge", type=float, default=0.0, help="Min edge_decimal to keep (scanner)")
    parser.add_argument("--out", default="prophetx_sgp_arbs.csv", help="CSV output path (scanner)")

    args = parser.parse_args()

    if args.scan:
        scanner = SGPScanner(concurrency=args.concurrency)
        event_ids = [args.event_id] if args.event_id else None
        rows = asyncio.run(scanner.scan(event_ids))
        flagged = annotate_and_save_scan(rows, args.cap, args.min_edge, args.out)
        print(f"\n=== TOP 15 by EV at ${args.cap:.0f} cap ===")
        for r in flagged[:15]:
            print(f"  {r['player']:25s} {r['chain']:20s}  HR {r['hr_odds']:+5d}  "
                  f"SGP {r['sgp_odds']:+6d}  stake ${r['sgp_stake']:>7.2f}  "
                  f"edge {r['edge_decimal']:+6.2f}  EV@cap ${r['ev_at_cap']:>7.2f}  "
                  f"[{r['event']}]")
    elif args.event_id:
        # Single event scrape
        print(f"Fetching markets for event {args.event_id}...")
        markets_data, response = GetEventMarkets(args.event_id)

        if markets_data and response.status_code == 200:
            SaveResponse(f"event_{args.event_id}", markets_data)

            if args.best_lines:
                best = GetBestLines(markets_data)
                print("\n=== BEST AVAILABLE LINES ===")
                for market_name, data in best.items():
                    print(f"\n{market_name} ({data['type']})")
                    if 'mainLine' in data:
                        print(f"  Main Line: {data['mainLine']}")
                    for sel in data['selections']:
                        print(f"  {sel['name']}: {sel['odds']} (${sel['value']:.2f} @ ${sel['stake']:.2f})")
        else:
            print("ERROR: Failed to fetch event")
    else:
        # Full scrape
        all_markets = ScrapeAllMarkets(
            save_individual=args.save_individual,
            save_combined=not args.no_combined
        )

        print(f"\nTotal events scraped: {len(all_markets)}")
        print("\nFinished scraping ProphetX exchange")
