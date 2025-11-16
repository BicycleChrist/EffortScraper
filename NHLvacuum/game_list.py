import requests
from bs4 import BeautifulSoup
import pandas as pd
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# Jam all of the game ids in one season .csv file instead of per team
def create_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.3, status_forcelist=(500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return session


def build_traditional_game_id(season, game_id):
    """Format: YYYY0game_id"""
    if not season or len(str(season)) != 8:
        return ""
    start_year = str(season)[:4]
    return f"{start_year}0{game_id}"


def parse_main_header(text):
    """
    Example:
        '2024-10-04 - Devils 4, Sabres 1'
    Returns:
        date, away_team, away_score, home_team, home_score
    """
    date_part, game_part = text.split(" - ", 1)
    away_chunk, home_chunk = game_part.split(",", 1)

    away_team, away_score = away_chunk.strip().rsplit(" ", 1)
    home_team, home_score = home_chunk.strip().rsplit(" ", 1)

    return (
        date_part.strip(),
        away_team.strip(),
        int(away_score),
        home_team.strip(),
        int(home_score),
    )


def scrape_unique_game_ids(url):
    session = create_session()
    print(f"Fetching game list from:\n{url}\n")

    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")

    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError("No tables found on NST page.")

    table = tables[0]
    rows = table.find_all("tr")[1:]  # skip header

    unique_records = {}

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        # ===== First column contains date + teams + scores =====
        header_text = cells[0].get_text(strip=True)
        if " - " not in header_text:
            continue

        (
            game_date,
            away_team,
            away_score,
            home_team,
            home_score,
        ) = parse_main_header(header_text)

        # ===== Get Full Report link =====
        links = cells[2].find_all("a")
        full_link = None
        for a in links:
            if "Full" in a.get_text():
                full_link = a.get("href")
                break

        if not full_link:
            continue

        params = dict(p.split("=") for p in full_link.split("?")[1].split("&") if "=" in p)
        game_id = params.get("game")
        season = params.get("season")

        if not game_id:
            continue

        # ===== Deduplicate by game_id =====
        if game_id not in unique_records:
            unique_records[game_id] = {
                "game_id": game_id,
                "season": season,
                "traditional_game_id": build_traditional_game_id(season, game_id),
                "game_date": game_date,
                "away_team": away_team,
                "home_team": home_team,
                "away_score": away_score,
                "home_score": home_score,
                "full_url": "https://www.naturalstattrick.com/" + full_link,
            }

    session.close()

    print(f"Found {len(unique_records)} unique games.")
    df = pd.DataFrame(unique_records.values()).sort_values(
        by="game_id", key=lambda x: x.astype(int)
    )
    return df


def scrape_season_game_ids(start_year):
    """
    Example: start_year=2024 → season 2024-2025
    """
    fromseason = int(f"{start_year}{start_year+1:02d}")
    thruseason = fromseason  # only using one season window

    url = (
        "https://www.naturalstattrick.com/games.php?"
        f"fromseason={fromseason}&thruseason={thruseason}"
        "&stype=2&sit=5v5&loc=B&team=All&rate=n"
    )

    print(f"\n=== Scraping Season {start_year}-{(start_year+1)%100:02d} ===\n")
    df = scrape_unique_game_ids(url)
    out_name = f"unique_game_ids_{start_year}-{(start_year+1)%100:02d}.csv"
    df.to_csv(out_name, index=False)
    print(f"\nSaved to {out_name}\n")
    return df


if __name__ == "__main__":
    # Change this to scrape any season
    scrape_season_game_ids(2022)
