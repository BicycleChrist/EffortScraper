#!/usr/bin/env python3
"""
ProphetX Exchange Scraper
Fetches orderbook data from ProphetX betting exchange API

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
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from playwright.sync_api import sync_playwright
from Creds import PROPHETX_AUTH_TOKEN, PROPHETX_EMAIL, PROPHETX_PASSWORD


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
        page = browser.new_page()
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


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ProphetX Exchange Scraper")
    parser.add_argument("--event-id", type=int, help="Scrape specific event ID only")
    parser.add_argument("--save-individual", action="store_true", help="Save individual event files (default: only combined)")
    parser.add_argument("--no-combined", action="store_true", help="Don't save combined file")
    parser.add_argument("--best-lines", action="store_true", help="Extract and display best lines only")

    args = parser.parse_args()

    if args.event_id:
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
