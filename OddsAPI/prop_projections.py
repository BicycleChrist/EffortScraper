#!/usr/bin/env python3
"""
Player Props Projections Module
Combines multiple sources for player prop projections:
- ActionNetwork API (actionnetwork.com)
- Covers HTML Scraper (covers.com)
"""

import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import time
from typing import Optional, Dict, Any, List


class ActionNetworkScraper:
    """Scraper for ActionNetwork player prop projections via API"""

    BASE_URL = "https://api.actionnetwork.com/web/v2/leagues/{league_id}/projections/available"

    # League IDs
    LEAGUES = {
        'nfl': 1,
        'nba': 2,
        'mlb': 3,
        'nhl': 4,
        'ncaaf': 5,
        'ncaab': 6,
        'wnba': 7
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Referer': 'https://www.actionnetwork.com/',
            'Origin': 'https://www.actionnetwork.com',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'Priority': 'u=4'
        })

    def get_props(
        self,
        league: str = 'nfl',
        is_live: bool = False,
        limit: int = 50,
        state_code: str = 'AK',
        week: Optional[int] = None,
        offset: int = 0,
        date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch player prop projections from ActionNetwork API

        Args:
            league: League to fetch (nfl, nba, mlb, nhl, ncaaf, ncaab, wnba)
            is_live: Whether to fetch live games only
            limit: Number of results to return (max 50)
            state_code: State code for betting regulations
            week: Week number (NFL/NCAAF only)
            offset: Pagination offset
            date: Date in YYYYMMDD format (defaults to today)

        Returns:
            JSON response from API
        """
        league_id = self.LEAGUES.get(league.lower())
        if league_id is None:
            raise ValueError(f"Invalid league: {league}. Must be one of {list(self.LEAGUES.keys())}")

        url = self.BASE_URL.format(league_id=league_id)

        # Default to today's date if not provided
        if date is None:
            date = datetime.now().strftime('%Y%m%d')

        params = {
            'date': date,
            'isLive': str(is_live).lower(),
            'limit': limit,
            'stateCode': state_code
        }

        if week is not None:
            params['week'] = week

        if offset > 0:
            params['offset'] = offset

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 404:
                # No data available for this league/week
                return {'playerProps': [], 'players': [], 'games': [], 'market_rules': {}}
            elif response.status_code == 400 and offset > 0:
                # Pagination not supported or no more data
                return {'playerProps': [], 'players': [], 'games': [], 'market_rules': {}}
            print(f"HTTP Error fetching data: {e}")
            raise
        except requests.exceptions.RequestException as e:
            print(f"Error fetching data: {e}")
            raise

    def process_response(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process raw API response into a more usable format

        Args:
            raw_data: Raw response from API

        Returns:
            Processed data with combined player/game information
        """
        # Convert indexed objects to lists (handle both dict and list formats)
        player_props = []
        if 'playerProps' in raw_data and raw_data['playerProps']:
            if isinstance(raw_data['playerProps'], list):
                player_props = raw_data['playerProps']
            elif isinstance(raw_data['playerProps'], dict):
                player_props = list(raw_data['playerProps'].values())

        players = {}
        if 'players' in raw_data and raw_data['players']:
            if isinstance(raw_data['players'], list):
                players = {p['id']: p for p in raw_data['players']}
            elif isinstance(raw_data['players'], dict):
                players = {p['id']: p for p in raw_data['players'].values()}

        games = {}
        if 'games' in raw_data and raw_data['games']:
            if isinstance(raw_data['games'], list):
                games = {g['id']: g for g in raw_data['games']}
            elif isinstance(raw_data['games'], dict):
                games = {g['id']: g for g in raw_data['games'].values()}

        # Enrich props with player and game data
        enriched_props = []
        for prop in player_props:
            enriched_prop = prop.copy()

            # Add player info
            player_id = prop.get('player_id')
            if player_id and player_id in players:
                enriched_prop['player_info'] = players[player_id]

            # Add game info from first line if available
            if prop.get('lines') and len(prop['lines']) > 0:
                event_id = prop['lines'][0].get('event_id')
                if event_id and event_id in games:
                    enriched_prop['game_info'] = games[event_id]

            enriched_props.append(enriched_prop)

        return {
            'props': enriched_props,
            'raw_players': list(players.values()),
            'raw_games': list(games.values()),
            'market_rules': raw_data.get('market_rules', {})
        }

    def get_all_props(
        self,
        league: str = 'nfl',
        is_live: bool = False,
        state_code: str = 'AK',
        week: Optional[int] = None,
        max_results: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Fetch all available props (API appears to not support pagination)

        Args:
            league: League to fetch
            is_live: Whether to fetch live games only
            state_code: State code
            week: Week number
            max_results: Maximum number of results to fetch (None = all available)

        Returns:
            Combined JSON response with all props
        """
        # Fetch data (API likely returns all available props in one call)
        raw_data = self.get_props(
            league=league,
            is_live=is_live,
            limit=max_results or 500,  # Request larger limit
            state_code=state_code,
            week=week,
            offset=0
        )

        # Check if we got data
        player_props = raw_data.get('playerProps', {})
        if not player_props:
            return {
                'props': [],
                'players': [],
                'games': [],
                'market_rules': {},
                'metadata': {
                    'total_props': 0,
                    'total_players': 0,
                    'total_games': 0,
                    'league': league,
                    'is_live': is_live,
                    'week': week,
                    'fetched_at': datetime.now().isoformat()
                }
            }

        # Process the response
        processed = self.process_response(raw_data)

        all_players = {p['id']: p for p in processed['raw_players']}
        all_games = {g['id']: g for g in processed['raw_games']}

        props_count = len(processed['props'])
        print(f"Fetched {props_count} props")

        # Return combined results with metadata
        return {
            'props': processed['props'],
            'players': list(all_players.values()),
            'games': list(all_games.values()),
            'market_rules': processed['market_rules'],
            'metadata': {
                'total_props': len(processed['props']),
                'total_players': len(all_players),
                'total_games': len(all_games),
                'league': league,
                'is_live': is_live,
                'week': week,
                'fetched_at': datetime.now().isoformat()
            }
        }

    def get_all_leagues(
        self,
        is_live: bool = False,
        state_code: str = 'AK',
        week: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Fetch props from all available leagues

        Args:
            is_live: Whether to fetch live games only
            state_code: State code
            week: Week number (only applies to NFL/NCAAF)

        Returns:
            Combined data from all leagues
        """
        all_leagues_data = {}
        total_stats = {
            'total_props': 0,
            'total_players': 0,
            'total_games': 0,
            'leagues_with_data': []
        }

        print("Fetching props from all leagues...")
        print("-" * 60)

        for league_name in self.LEAGUES.keys():
            print(f"\nFetching {league_name.upper()}...", end=" ")

            try:
                # Only pass week for NFL and NCAAF
                league_week = week if league_name in ['nfl', 'ncaaf'] else None

                data = self.get_all_props(
                    league=league_name,
                    is_live=is_live,
                    state_code=state_code,
                    week=league_week
                )

                props_count = data['metadata']['total_props']

                if props_count > 0:
                    all_leagues_data[league_name] = data
                    total_stats['total_props'] += props_count
                    total_stats['total_players'] += data['metadata']['total_players']
                    total_stats['total_games'] += data['metadata']['total_games']
                    total_stats['leagues_with_data'].append(league_name.upper())
                    print(f"✓ {props_count} props, {data['metadata']['total_games']} games")
                else:
                    print("✗ No data available")

            except Exception as e:
                print(f"✗ Error: {e}")
                continue

        print("\n" + "-" * 60)
        print(f"Total: {total_stats['total_props']} props from {len(total_stats['leagues_with_data'])} leagues")
        print(f"Leagues with data: {', '.join(total_stats['leagues_with_data'])}")

        return {
            'leagues': all_leagues_data,
            'summary': total_stats,
            'metadata': {
                'fetched_at': datetime.now().isoformat(),
                'is_live': is_live,
                'state_code': state_code
            }
        }

    def save_to_json(self, data: Dict[str, Any], filename: Optional[str] = None) -> str:
        """
        Save data to JSON file

        Args:
            data: Data to save
            filename: Output filename (auto-generated if None)

        Returns:
            Filename that was saved to
        """
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

            # Check if this is multi-league data
            if 'leagues' in data:
                filename = f'actionnetwork_props_all_leagues_{timestamp}.json'
            else:
                league = data.get('metadata', {}).get('league', 'unknown')
                filename = f'actionnetwork_props_{league}_{timestamp}.json'

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"\nData saved to: {filename}")
        return filename


class CoversScraper:
    """Scraper for Covers.com player prop projections via HTML scraping"""

    # URLs for major sports
    SPORT_URLS = {
        'MLB': 'https://www.covers.com/sport/baseball/mlb/player-props',
        'NFL': 'https://www.covers.com/sport/football/nfl/player-props',
        'NBA': 'https://www.covers.com/sport/basketball/nba/player-props',
        'NHL': 'https://www.covers.com/sport/hockey/nhl/player-props',
        'NCAAF': 'https://www.covers.com/sport/football/ncaaf/player-props',
        'NCAAB': 'https://www.covers.com/sport/basketball/ncaab/player-props'
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })

    def scrape_player_props(self, url: str, sport_key: str) -> List[Dict[str, Any]]:
        """
        Scrape player props from Covers.com

        Args:
            url: URL to scrape
            sport_key: Sport identifier (MLB, NFL, etc.)

        Returns:
            List of player prop dictionaries
        """
        print(f"Fetching {sport_key} props from {url}...")

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch {sport_key}: {e}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        props_data = []

        # Find all player prop articles
        articles = soup.find_all('article', class_='player-prop-article')
        print(f"Found {len(articles)} player props for {sport_key}")

        for article in articles:
            try:
                prop_data = self._extract_prop_data(article, sport_key)
                if prop_data:
                    props_data.append(prop_data)
            except Exception as e:
                print(f"Error parsing article: {e}")
                continue

        return props_data

    def _extract_prop_data(self, article, sport_key: str) -> Optional[Dict[str, Any]]:
        """
        Extract data from a single player prop article

        Args:
            article: BeautifulSoup article element
            sport_key: Sport identifier

        Returns:
            Dictionary with prop data or None
        """
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
        best_odds = self._extract_best_odds(article)

        # All odds
        all_odds = self._extract_all_odds(article)

        # Star rating
        star_rating = self._count_stars(article)

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

    def _extract_best_odds(self, article) -> Dict[str, Any]:
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

    def _extract_all_odds(self, article) -> List[Dict[str, Any]]:
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

    def _count_stars(self, article) -> int:
        """Count the number of gold stars in the rating"""
        star_div = article.find('div', class_='star-rating')
        if star_div:
            gold_stars = star_div.find_all('span', class_='gold-star')
            return len(gold_stars)
        return 0

    def scrape_all_sports(self, rate_limit_seconds: int = 2) -> Dict[str, List[Dict[str, Any]]]:
        """
        Scrape props for all available sports

        Args:
            rate_limit_seconds: Seconds to wait between requests

        Returns:
            Dictionary mapping sport to list of props
        """
        all_data = {}

        for sport, url in self.SPORT_URLS.items():
            props = self.scrape_player_props(url, sport)
            all_data[sport] = props

            # Be respectful with rate limiting
            if props:
                time.sleep(rate_limit_seconds)

        return all_data

    def save_to_json(self, data: Dict[str, Any], filename: Optional[str] = None) -> str:
        """
        Save data to JSON file

        Args:
            data: Data to save
            filename: Output filename (auto-generated if None)

        Returns:
            Filename that was saved to
        """
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'covers_props_{timestamp}.json'

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"\nData saved to: {filename}")
        return filename


def main():
    """Main entry point - demonstrates usage of both scrapers"""
    import argparse

    parser = argparse.ArgumentParser(description='Scrape player prop projections from multiple sources')
    parser.add_argument(
        '--source',
        choices=['actionnetwork', 'covers', 'both'],
        default='both',
        help='Which source to scrape (default: both)'
    )
    parser.add_argument(
        '--league',
        type=str,
        help='Specific league to fetch (ActionNetwork only)'
    )
    parser.add_argument(
        '--output',
        type=str,
        help='Output filename (auto-generated if not specified)'
    )

    args = parser.parse_args()

    # ActionNetwork scraper
    if args.source in ['actionnetwork', 'both']:
        print("\n" + "=" * 60)
        print("ACTIONNETWORK SCRAPER")
        print("=" * 60)

        an_scraper = ActionNetworkScraper()

        try:
            if args.league:
                # Fetch specific league
                data = an_scraper.get_all_props(league=args.league)
                filename = f'actionnetwork_{args.league}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json' if not args.output else args.output
            else:
                # Fetch all leagues
                data = an_scraper.get_all_leagues()
                filename = args.output

            an_scraper.save_to_json(data, filename)

        except Exception as e:
            print(f"\nActionNetwork Error: {e}")

    # Covers scraper
    if args.source in ['covers', 'both']:
        print("\n" + "=" * 60)
        print("COVERS SCRAPER")
        print("=" * 60)

        covers_scraper = CoversScraper()

        try:
            all_data = covers_scraper.scrape_all_sports()

            # Print summary
            print("\n" + "=" * 50)
            print("Scraping Complete!")
            print("\nSummary:")
            for sport, props in all_data.items():
                print(f"  {sport}: {len(props)} props")

            total_props = sum(len(props) for props in all_data.values())
            print(f"\nTotal props scraped: {total_props}")

            # Save to file
            filename = args.output if args.output and args.source == 'covers' else None
            covers_scraper.save_to_json(all_data, filename)

        except Exception as e:
            print(f"\nCovers Error: {e}")

    return 0


if __name__ == '__main__':
    exit(main())
