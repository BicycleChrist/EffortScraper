#!/usr/bin/env python3
"""
Covers.com Player Props Scraper
Scrapes player prop projections and odds from Covers.com
"""

import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import time


# URLs for major sports
SPORT_URLS = {
    'MLB': 'https://www.covers.com/sport/baseball/mlb/player-props',
    'NFL': 'https://www.covers.com/sport/football/nfl/player-props',
    'NBA': 'https://www.covers.com/sport/basketball/nba/player-props',
    'NHL': 'https://www.covers.com/sport/hockey/nhl/player-props',
    'NCAAF': 'https://www.covers.com/sport/football/ncaaf/player-props',
    'NCAAB': 'https://www.covers.com/sport/basketball/ncaab/player-props'
}


def scrape_player_props(url, sport_key):
    """
    Scrape player props from Covers.com

    Args:
        url: URL to scrape
        sport_key: Sport identifier (MLB, NFL, etc.)

    Returns:
        List of player prop dictionaries
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    print(f"Fetching {sport_key} props from {url}...")
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print(f"Failed to fetch {sport_key}: Status {response.status_code}")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    props_data = []

    # Find all player prop articles
    articles = soup.find_all('article', class_='player-prop-article')
    print(f"Found {len(articles)} player props for {sport_key}")

    for article in articles:
        try:
            prop_data = extract_prop_data(article, sport_key)
            if prop_data:
                props_data.append(prop_data)
        except Exception as e:
            print(f"Error parsing article: {e}")
            continue

    return props_data


def extract_prop_data(article, sport_key):
    """Extract data from a single player prop article"""

    # Player name
    player_link = article.find('a', class_='player-link')
    player_name = player_link.get_text(strip=True) if player_link else None

    # Player position
    position_div = article.find('div', class_='player-position')
    position = position_div.get_text(strip=True) if position_div else None

    # Team matchup
    team_logos_div = article.find('div', class_='team-logos')
    away_team = None
    home_team = None
    if team_logos_div:
        shortnames = team_logos_div.find_all('span', class_='shortname')
        if len(shortnames) >= 2:
            away_team = shortnames[0].get_text(strip=True)
            home_team = shortnames[1].get_text(strip=True)

    # Prop type
    prop_event = article.find('div', class_='player-event')
    prop_type = prop_event.get_text(strip=True) if prop_event else None

    # Prop line (the o/u number)
    prop_line = None
    other_over_odds = article.find('div', class_='other-over-odds')
    if other_over_odds:
        # The line number appears as text node before the prop type
        line_text = other_over_odds.get_text(strip=True)
        # Extract just the number
        for text in line_text.split():
            try:
                prop_line = float(text)
                break
            except ValueError:
                continue

    # Projection
    projection_div = article.find('div', class_='player-props-projection-bestOdds-div')
    projection = None
    if projection_div:
        proj_divs = projection_div.find_all('div', class_='other-over-odds', attrs={'data-num-col': '2'})
        for div in proj_divs:
            strong = div.find('strong')
            if strong:
                try:
                    projection = float(strong.get_text(strip=True))
                except ValueError:
                    pass

    # Best odds
    best_odds = extract_best_odds(article)

    # All odds
    all_odds = extract_all_odds(article)

    # Star rating
    star_rating = count_stars(article)

    # Analysis text
    analysis_p = article.find('p', class_='player-analysis')
    analysis = analysis_p.get_text(strip=True) if analysis_p else None

    return {
        'sport': sport_key,
        'player_name': player_name,
        'position': position,
        'away_team': away_team,
        'home_team': home_team,
        'matchup': f"{away_team} @ {home_team}" if away_team and home_team else None,
        'prop_type': prop_type,
        'prop_line': prop_line,
        'projection': projection,
        'projection_rating': star_rating,
        'best_odds': best_odds,
        'all_odds': all_odds,
        'analysis': analysis,
        'scraped_at': datetime.now().isoformat()
    }


def extract_best_odds(article):
    """Extract best odds from the article"""
    best_odds = {}

    # Find best over odds
    over_row = article.find('div', class_='player-bestOdds-row over')
    if over_row:
        odds_span = over_row.find('span', recursive=False)
        book_figure = over_row.find('figure')

        if odds_span and book_figure:
            odds_text = odds_span.get_text(strip=True)
            book_class = book_figure.get('class', [])
            book_name = next((c.replace('bg-', '') for c in book_class if c.startswith('bg-')), None)

            best_odds['over'] = {
                'odds': odds_text,
                'sportsbook': book_name
            }

    # Find best under odds (if exists)
    under_row = article.find('div', class_='player-bestOdds-row under')
    if under_row:
        odds_span = under_row.find('span', recursive=False)
        book_figure = under_row.find('figure')

        if odds_span and book_figure:
            odds_text = odds_span.get_text(strip=True)
            book_class = book_figure.get('class', [])
            book_name = next((c.replace('bg-', '') for c in book_class if c.startswith('bg-')), None)

            best_odds['under'] = {
                'odds': odds_text,
                'sportsbook': book_name
            }

    return best_odds


def extract_all_odds(article):
    """Extract all odds from the comparison section"""
    all_odds = []

    # Find the collapse section with all odds
    collapse_div = article.find('div', class_='collapse')
    if not collapse_div:
        return all_odds

    # Find all odds rows
    odds_rows = collapse_div.find_all('div', class_='other-odds-row')

    for row in odds_rows:
        # Skip placeholder rows
        if 'placeholder-row' in row.get('class', []):
            continue

        # Get sportsbook
        book_img = row.find('img')
        sportsbook = book_img.get('alt', '') if book_img else None
        if not sportsbook:
            continue

        # Get over odds
        over_div = row.find('div', class_='other-over-odds')
        over_odds = None
        if over_div:
            odds_span = over_div.find('span', class_='oddtype')
            if odds_span:
                over_odds = odds_span.get_text(strip=True)

        # Get under odds
        under_div = row.find('div', class_='other-under-odds')
        under_odds = None
        if under_div:
            # Check if it's a placeholder
            if 'placeholder-cell' not in under_div.get('class', []):
                odds_span = under_div.find('span', class_='oddtype')
                if odds_span:
                    under_odds = odds_span.get_text(strip=True)

        all_odds.append({
            'sportsbook': sportsbook,
            'over': over_odds,
            'under': under_odds
        })

    return all_odds


def count_stars(article):
    """Count the number of gold stars in the rating"""
    star_div = article.find('div', class_='star-rating')
    if star_div:
        gold_stars = star_div.find_all('span', class_='gold-star')
        return len(gold_stars)
    return 0


def scrape_all_sports():
    """Scrape props for all available sports"""
    all_data = {}

    for sport, url in SPORT_URLS.items():
        props = scrape_player_props(url, sport)
        all_data[sport] = props

        # Be respectful with rate limiting
        if props:
            time.sleep(2)

    return all_data


def main():
    """Main execution"""
    print("Starting Covers.com Props Scraper...")
    print("=" * 50)

    all_data = scrape_all_sports()

    # Save to JSON
    output_file = 'prop_projections.json'
    with open(output_file, 'w') as f:
        json.dump(all_data, f, indent=2)

    # Print summary
    print("\n" + "=" * 50)
    print("Scraping Complete!")
    print(f"Data saved to: {output_file}")
    print("\nSummary:")
    for sport, props in all_data.items():
        print(f"  {sport}: {len(props)} props")

    total_props = sum(len(props) for props in all_data.values())
    print(f"\nTotal props scraped: {total_props}")


if __name__ == "__main__":
    main()
