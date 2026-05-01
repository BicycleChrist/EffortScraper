"""
savant_bbe_scraper.py
---------------------
Pulls every batter's individual BBE (Batted Ball Event) data from Baseball
Savant for a given season, mirroring the per-player event rows visible when
you click a name on the EV & Barrels leaderboard.

Two-step approach — no HTML scraping required:

  Step 1:  Leaderboard CSV endpoint
           /leaderboard/statcast?type=batter&year=YEAR&min=1&csv=true
           Returns one row per batter (min 1 BBE) including player_id.

  Step 2:  Statcast Search CSV endpoint  (per player, threaded)
           /statcast_search/csv?...&player_id=ID&type=details
           Returns one row per BBE: date, pitcher, event type, exit velocity,
           launch angle, hit distance — the same data as evLeaderboard-trSub_<id>.

Threading:
  Each player request is independent I/O, so we use a ThreadPoolExecutor.
  Default workers=10, delay=1.2s per thread.  At those settings ~700 batters
  complete in roughly 90 seconds instead of ~14 minutes serially.

Output:
    savant_bbe_YEAR.csv  — all BBEs for all batters, tagged with player_id.

Usage:
    python savant_bbe_scraper.py                         # current season
    python savant_bbe_scraper.py --year 2024
    python savant_bbe_scraper.py --year 2024 --workers 6 --delay 1.5
    python savant_bbe_scraper.py --year 2024 --player_id 592450   # single player test
"""

import argparse
import time
import datetime
import io
import threading
import concurrent.futures
import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
LEADERBOARD_URL = (
    "https://baseballsavant.mlb.com/leaderboard/statcast"
    "?type={player_type}&year={year}&position=&team=&min=1&csv=true"
)

BBE_URL = (
    "https://baseballsavant.mlb.com/statcast_search/csv"
    "?hfPT=&hfAB=&hfGT=R%7C&hfPR=&hfZ=&hfStadium=&hfBBL=&hfNewZones=&hfPull="
    "&hfC=&hfSea={year}%7C&hfSit=&player_type={player_type}"
    "&hfOuts=&hfOpponent=&pitcher_throws=&batter_stands=&hfSA=&min_pitches=0"
    "&min_results=0&group_by=name&sort_col=pitches&player_event_sort=api_p_release_speed"
    "&sort_order=desc&min_abs=0&type=details&player_id={player_id}"
)

BBE_KEEP_COLS = [
    "game_date",
    "player_name",      # batter name
    "batter",           # MLBAM batter id
    "pitcher",          # MLBAM pitcher id
    "events",           # field_out, home_run, single, double, etc.
    "bb_type",          # ground_ball, fly_ball, line_drive, popup
    "launch_speed",     # exit velocity mph
    "launch_angle",     # degrees
    "hit_distance_sc",  # projected distance ft
    "hc_x",             # spray chart x (Savant pixel grid, HP ≈ 125.42)
    "hc_y",             # spray chart y (Savant pixel grid, HP ≈ 198.27)
    # ── Pitch trajectory (Hawk-Eye / Trackman state vector) ──
    "release_speed",    # pitch velocity at release (mph)
    "release_spin_rate", # pitch spin rate (rpm)
    "spin_axis",        # pitch spin axis (degrees)
    "pitch_type",       # pitch type code (FF, SL, CH, CU, etc.)
    "pitch_name",       # pitch type full name
    "release_pos_x",    # horizontal release position (ft, catcher POV: neg=1B side)
    "release_pos_y",    # distance from plate at release (ft, ~50-55)
    "release_pos_z",    # release height (ft)
    "release_extension", # extension past rubber toward plate (ft)
    "vx0",              # initial x-velocity (ft/s, Savant coordinate system)
    "vy0",              # initial y-velocity (ft/s, toward plate)
    "vz0",              # initial z-velocity (ft/s, vertical)
    "ax",               # x-acceleration (ft/s², includes Magnus + drag)
    "ay",               # y-acceleration (ft/s², toward plate)
    "az",               # z-acceleration (ft/s², vertical, includes gravity)
    "pfx_x",            # horizontal movement vs no-spin (in, catcher POV)
    "pfx_z",            # induced vertical break vs no-spin (in)
    "plate_x",          # horizontal plate crossing location (ft, catcher POV)
    "plate_z",          # vertical plate crossing location (ft)
    "sz_top",           # top of batter's strike zone (ft)
    "sz_bot",           # bottom of batter's strike zone (ft)
    "effective_speed",  # perceived velocity (mph)
    # ── Context ──
    "home_team",
    "away_team",
    "inning",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://baseballsavant.mlb.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 30  # seconds

# Thread-safe print lock so progress lines don't interleave
_print_lock = threading.Lock()

def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Step 1 - leaderboard
# ---------------------------------------------------------------------------

def fetch_leaderboard(year: int, player_type: str = "batter") -> pd.DataFrame:
    """Pull the full leaderboard (min=1 BBE) and return a DataFrame."""
    url = LEADERBOARD_URL.format(year=year, player_type=player_type)
    print(f"[Step 1] Fetching {player_type} list for {year} (min 1 BBE)...")
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))
    print(f"         {len(df)} {player_type}s found.\n")

    # Normalise column names - Savant occasionally tweaks them
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    if "player_id" not in df.columns:
        for candidate in ("batter", "mlbam_id", "id"):
            if candidate in df.columns:
                df = df.rename(columns={candidate: "player_id"})
                break

    if "player_id" not in df.columns:
        raise ValueError(
            "Could not find a player_id column in the leaderboard CSV. "
            f"Columns present: {list(df.columns)}"
        )

    return df


# ---------------------------------------------------------------------------
# Step 2 - per-player BBE fetch (runs inside thread pool)
# ---------------------------------------------------------------------------

def fetch_player_bbe(player_id: int, year: int, name: str,
                     delay: float, counter: list, total: int,
                     player_type: str = "batter") -> pd.DataFrame:
    """
    Fetch all BBE rows for one player. Designed to be called from a thread.
    counter is a shared [int] so we can show thread-safe progress.
    """
    # Per-thread polite delay - staggers requests across workers naturally
    time.sleep(delay)

    url = BBE_URL.format(year=year, player_id=player_id, player_type=player_type)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        if not resp.text.strip():
            result = pd.DataFrame()
        else:
            df = pd.read_csv(io.StringIO(resp.text))

            # Drop rows with no tracked contact (pitches not put in play at all)
            if "launch_speed" in df.columns:
                df = df[df["launch_speed"].notna()].copy()

            # Foul balls mid-PA have launch data but no events value since the
            # PA has not ended yet. Label them explicitly rather than leaving
            # the field blank -- they carry real contact quality signal.
            if "events" in df.columns:
                df["events"] = df["events"].fillna("foul")

            keep = [c for c in BBE_KEEP_COLS if c in df.columns]
            df = df[keep].copy()
            df["player_id"] = player_id
            result = df

    except Exception as exc:
        tprint(f"  WARNING: failed for {name} (id={player_id}): {exc}")
        result = pd.DataFrame()

    # Thread-safe progress counter
    with _print_lock:
        counter[0] += 1
        n = counter[0]
        bbe_count = len(result) if not result.empty else 0
        print(f"  [{n:>4}/{total}] {name:<30} (id={player_id})  -- {bbe_count} BBEs")

    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(year: int, delay: float, workers: int,
        single_player_id, output_path: str, player_type: str = "batter"):

    leaderboard = fetch_leaderboard(year, player_type=player_type)

    if single_player_id:
        subset = leaderboard[leaderboard["player_id"] == single_player_id]
        if subset.empty:
            tprint(f"  player_id {single_player_id} not in leaderboard -- pulling anyway.")
            subset = pd.DataFrame({"player_id": [single_player_id]})
        leaderboard = subset

    # Build (player_id, display_name) pairs
    name_cols = ("last_name,_first_name", "player_name", "last_name")
    def get_name(row):
        for col in name_cols:
            if col in leaderboard.columns:
                return str(row[col])
        return "unknown"

    players = [
        (int(row["player_id"]), get_name(row))
        for _, row in leaderboard.dropna(subset=["player_id"]).iterrows()
    ]

    # Deduplicate while preserving order
    seen = set()
    unique_players = []
    for pid, name in players:
        if pid not in seen:
            seen.add(pid)
            unique_players.append((pid, name))
    players = unique_players

    total = len(players)
    effective_workers = min(workers, total)
    est_seconds = (total * delay) / effective_workers
    print(f"[Step 2] Fetching BBE detail for {total} {player_type}s "
          f"using {effective_workers} workers (delay={delay}s per thread).")
    print(f"         Estimated time: ~{est_seconds:.0f}s\n")

    # Shared mutable counter for progress display
    counter = [0]
    all_bbe = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as pool:
        futures = {
            pool.submit(
                fetch_player_bbe,
                pid, year, name, delay, counter, total, player_type
            ): pid
            for pid, name in players
        }

        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is not None and not result.empty:
                all_bbe.append(result)

    if not all_bbe:
        print("\nNo BBE data retrieved.")
        return

    print(f"\n[Step 3] Assembling and writing output...")
    combined = pd.concat(all_bbe, ignore_index=True)
    combined.sort_values(["player_id", "game_date"], inplace=True)
    combined.reset_index(drop=True, inplace=True)

    combined.to_csv(output_path, index=False)
    print(f"Done.  {len(combined):,} total BBEs from {len(all_bbe)} {player_type}s "
          f"written to: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    current_year = datetime.datetime.now().year
    parser = argparse.ArgumentParser(
        description="Scrape per-player BBE data from Baseball Savant (all batters, threaded)."
    )
    parser.add_argument(
        "--year", type=int, default=current_year,
        help=f"MLB season year (default: {current_year})"
    )
    parser.add_argument(
        "--workers", type=int, default=10,
        help="Number of concurrent threads (default: 10)"
    )
    parser.add_argument(
        "--delay", type=float, default=1.2,
        help="Per-thread delay in seconds between requests (default: 1.2)"
    )
    parser.add_argument(
        "--player_id", type=int, default=None,
        help="Pull a single player by MLBAM id for testing (e.g. 592450)"
    )
    parser.add_argument(
        "--player_type", type=str, default="batter",
        choices=["batter", "pitcher"],
        help="Player type: 'batter' (default) or 'pitcher'"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output CSV path (default: savant_bbe_YEAR.csv or savant_pitcher-bbe_YEAR.csv)"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.output:
        out = args.output
    elif args.player_type == "pitcher":
        out = f"savant_pitcher-bbe_{args.year}.csv"
    else:
        out = f"savant_bbe_{args.year}.csv"
    run(
        year=args.year,
        delay=args.delay,
        workers=args.workers,
        single_player_id=args.player_id,
        output_path=out,
        player_type=args.player_type,
    )
