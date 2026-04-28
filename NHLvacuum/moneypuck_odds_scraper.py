#!/usr/bin/env python3
"""
Multithreaded headless Selenium scraper for MoneyPuck game lines.
Scrapes opening and closing odds from MoneyPuck preview pages.
"""

import csv
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Set
from datetime import datetime
import argparse
import glob

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Import game list generation functions
from game_list import scrape_season_game_ids



# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('moneypuck_odds_scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


#


@dataclass
class GameLine:
    """Data structure for game line information from a specific sportsbook"""
    game_id: str
    traditional_game_id: str
    season: str
    sportsbook: str  # The sportsbook this data is from

    # Teams (same for opening and closing)
    away_team: Optional[str] = None
    home_team: Optional[str] = None

    # Opening line
    opening_timestamp: Optional[str] = None
    opening_away_odds: Optional[str] = None
    opening_home_odds: Optional[str] = None

    # Closing line
    closing_timestamp: Optional[str] = None
    closing_away_odds: Optional[str] = None
    closing_home_odds: Optional[str] = None

    # MoneyPuck win probabilities (same across all sportsbooks)
    mp_away_win_prob: Optional[str] = None
    mp_home_win_prob: Optional[str] = None

    # Status
    scrape_status: str = 'pending'
    error_message: Optional[str] = None


class MoneyPuckScraper:
    """Scraper for MoneyPuck game lines"""

    def __init__(self, headless: bool = True, timeout: int = 20, max_retries: int = 3):
        self.headless = headless
        self.timeout = timeout
        self.max_retries = max_retries

    def _create_driver(self) -> webdriver.Firefox:
        """Create and configure Firefox WebDriver"""
        options = Options()
        if self.headless:
            options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')

        # Optional: specify geckodriver path if not in PATH
        # service = Service('/path/to/geckodriver')
        # driver = webdriver.Firefox(service=service, options=options)

        driver = webdriver.Firefox(options=options)
        # Set page load timeout to be generous (double the element wait timeout)
        driver.set_page_load_timeout(self.timeout * 2)
        return driver

    def _extract_team_from_img(self, img_element) -> str:
        """Extract team abbreviation from image src URL"""
        try:
            src = img_element.get_attribute('src')
            # URL format: https://peter-tanner.com/moneypuck/logos/FLA.png
            team = src.split('/')[-1].replace('.png', '')
            return team
        except Exception as e:
            logger.error(f"Error extracting team from image: {e}")
            return None

    def _parse_win_probabilities(self, driver) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse MoneyPuck win probabilities from the header table.
        Returns: (away_win_prob, home_win_prob)

        HTML structure:
        <div class="header">
            <table id="headerTable">
                <tbody>
                    <tr>
                        <td><p style="font-size:15pt">Chance of Winning:</p><p style="font-size:30pt">56.3%</p></td>
                        <td><img src="...FLA.png" ...></td>
                        <td>...</td>
                        <td><img src="...OTT.png" ...></td>
                        <td><p style="font-size:15pt">Chance of Winning:</p><p style="font-size:30pt">43.7%</p></td>
                    </tr>
                </tbody>
            </table>
        </div>
        """
        try:
            # Find the header table
            header_table = driver.find_element(By.ID, 'headerTable')

            # Get all td elements in the first row
            tds = header_table.find_elements(By.TAG_NAME, 'td')

            if len(tds) < 5:
                logger.warning(f"Expected 5 tds in header table, found {len(tds)}")
                return (None, None)

            # First td contains away team win probability
            # Last td contains home team win probability
            away_td = tds[0]
            home_td = tds[4]

            # Extract probability from p tags (looking for the one with font-size:30pt containing %)
            away_p_tags = away_td.find_elements(By.TAG_NAME, 'p')
            home_p_tags = home_td.find_elements(By.TAG_NAME, 'p')

            away_win_prob = None
            home_win_prob = None

            # Find the p tag containing the percentage
            for p in away_p_tags:
                text = p.text.strip()
                if '%' in text:
                    away_win_prob = text
                    break

            for p in home_p_tags:
                text = p.text.strip()
                if '%' in text:
                    home_win_prob = text
                    break

            logger.info(f"Extracted win probabilities - Away: {away_win_prob}, Home: {home_win_prob}")
            return (away_win_prob, home_win_prob)

        except NoSuchElementException as e:
            logger.error(f"Error parsing win probabilities: {e}")
            return (None, None)
        except Exception as e:
            logger.error(f"Unexpected error parsing win probabilities: {e}")
            return (None, None)

    def _get_available_sportsbooks(self, driver) -> List[str]:
        """
        Get list of available sportsbooks from the dropdown.
        Returns: List of sportsbook values (e.g., ['draftkings', 'pinnacle', 'fanduel', ...])
        """
        try:
            dropdown = driver.find_element(By.ID, 'odds_source')
            options = dropdown.find_elements(By.TAG_NAME, 'option')
            sportsbooks = [opt.get_attribute('value') for opt in options]
            logger.debug(f"Found {len(sportsbooks)} sportsbooks: {sportsbooks}")
            return sportsbooks
        except NoSuchElementException as e:
            logger.error(f"Error finding sportsbook dropdown: {e}")
            return []

    def _select_sportsbook(self, driver, sportsbook: str):
        """
        Select a specific sportsbook from the dropdown and wait for table to update.

        Args:
            driver: Selenium WebDriver instance
            sportsbook: Sportsbook value to select (e.g., 'pinnacle')
        """
        try:
            # Get current odds before changing (to detect when update completes)
            try:
                table = driver.find_element(By.ID, 'preGameOdds')
                old_text = table.text
            except:
                old_text = None

            # Scroll dropdown into view first
            dropdown = driver.find_element(By.ID, 'odds_source')
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", dropdown)
            time.sleep(0.2)  # Brief pause after scrolling

            # Use JavaScript to set the value directly (avoids scrolling issues with options)
            driver.execute_script(f"""
                var dropdown = document.getElementById('odds_source');
                dropdown.value = '{sportsbook}';

                // Trigger change event
                var event = new Event('change', {{ bubbles: true }});
                dropdown.dispatchEvent(event);

                // Call the onChange function directly
                if (typeof changeOddsSource === 'function') {{
                    changeOddsSource(true);
                }}
            """)

            # Wait for the table to update
            time.sleep(0.5)  # Initial wait for JS to execute

            # Wait up to 2 seconds for content to change
            if old_text:
                for _ in range(10):  # Check 10 times over 2 seconds
                    try:
                        table = driver.find_element(By.ID, 'preGameOdds')
                        new_text = table.text
                        if new_text != old_text:
                            logger.debug(f"Table content changed for {sportsbook}")
                            break  # Content changed, update complete
                    except:
                        pass
                    time.sleep(0.2)
            else:
                # If we couldn't get old text, just wait a bit longer
                time.sleep(0.7)

            logger.debug(f"Selected sportsbook: {sportsbook}")
        except Exception as e:
            logger.error(f"Error selecting sportsbook {sportsbook}: {e}")
            raise

    def _parse_game_line(self, td_element, extract_teams: bool = False) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        Parse a single game line (opening or closing) from a table cell.
        Returns: (timestamp, away_odds, home_odds, away_team, home_team)
        If extract_teams is False, away_team and home_team will be None
        """
        try:
            # Extract timestamp from h3 (format: "Sportsbook\nTimestamp")
            h3 = td_element.find_element(By.TAG_NAME, 'h3')
            h3_text = h3.text.strip()
            lines = h3_text.split('\n')
            timestamp = lines[1] if len(lines) > 1 else None

            # Extract odds and teams from h2
            h2 = td_element.find_element(By.TAG_NAME, 'h2')

            # Extract odds - they appear as text between images
            h2_text = h2.text.strip()
            # Format is typically: "-130\n+108" or similar
            odds_parts = [part.strip() for part in h2_text.split('\n') if part.strip() and (part.strip().startswith('+') or part.strip().startswith('-'))]

            away_odds = odds_parts[0] if len(odds_parts) > 0 else None
            home_odds = odds_parts[1] if len(odds_parts) > 1 else None

            # Only extract teams if requested (for opening line)
            away_team = None
            home_team = None
            if extract_teams:
                imgs = h2.find_elements(By.TAG_NAME, 'img')
                if len(imgs) < 2:
                    logger.warning(f"Expected 2 team images, found {len(imgs)}")
                else:
                    away_team = self._extract_team_from_img(imgs[0])
                    home_team = self._extract_team_from_img(imgs[1])

            return (timestamp, away_odds, home_odds, away_team, home_team)

        except NoSuchElementException as e:
            logger.error(f"Error parsing game line: {e}")
            return (None, None, None, None, None)

    def _scrape_single_sportsbook(self, driver, sportsbook: str, traditional_game_id: str,
                                   game_id: str, season: str, away_team: str, home_team: str,
                                   mp_away_win_prob: str, mp_home_win_prob: str) -> GameLine:
        """
        Scrape odds for a single sportsbook (assumes sportsbook is already selected).

        Args:
            driver: Selenium WebDriver instance
            sportsbook: Sportsbook identifier
            traditional_game_id: Traditional game ID
            game_id: Simplified game ID
            season: Season identifier
            away_team: Away team abbreviation
            home_team: Home team abbreviation
            mp_away_win_prob: MoneyPuck away win probability
            mp_home_win_prob: MoneyPuck home win probability

        Returns:
            GameLine object for this sportsbook
        """
        game_line = GameLine(
            game_id=game_id,
            traditional_game_id=traditional_game_id,
            season=season,
            sportsbook=sportsbook,
            away_team=away_team,
            home_team=home_team,
            mp_away_win_prob=mp_away_win_prob,
            mp_home_win_prob=mp_home_win_prob
        )

        try:
            # Find the preGameOdds table
            table = driver.find_element(By.ID, 'preGameOdds')
            nested_tables = table.find_elements(By.TAG_NAME, 'table')

            if not nested_tables:
                game_line.scrape_status = 'no_data'
                game_line.error_message = f'No odds table found for {sportsbook}'
                return game_line

            odds_table = nested_tables[0]
            rows = odds_table.find_elements(By.TAG_NAME, 'tr')

            if len(rows) < 2:
                game_line.scrape_status = 'no_data'
                game_line.error_message = f'Insufficient rows for {sportsbook}'
                return game_line

            data_row = rows[1]
            tds = data_row.find_elements(By.TAG_NAME, 'td')

            if len(tds) < 2:
                game_line.scrape_status = 'no_data'
                game_line.error_message = f'Insufficient columns for {sportsbook}'
                return game_line

            # Parse opening line (first td) - don't extract teams (already have them)
            (game_line.opening_timestamp,
             game_line.opening_away_odds,
             game_line.opening_home_odds,
             _,
             _) = self._parse_game_line(tds[0], extract_teams=False)

            # Parse closing line (second td)
            (game_line.closing_timestamp,
             game_line.closing_away_odds,
             game_line.closing_home_odds,
             _,
             _) = self._parse_game_line(tds[1], extract_teams=False)

            game_line.scrape_status = 'success'
            logger.debug(f"Successfully scraped {sportsbook} for game {traditional_game_id}")
            return game_line

        except NoSuchElementException as e:
            game_line.scrape_status = 'error'
            game_line.error_message = f'{sportsbook}: {str(e)}'
            logger.error(f"Error scraping {sportsbook} for game {traditional_game_id}: {e}")
            return game_line

    def _scrape_game_attempt(self, traditional_game_id: str, game_id: str, season: str) -> List[GameLine]:
        """
        Internal method: Single attempt at scraping a game for ALL sportsbooks.

        Args:
            traditional_game_id: The traditional game ID (e.g., "2023020001")
            game_id: The simplified game ID from CSV
            season: The season identifier

        Returns:
            List of GameLine objects, one for each sportsbook

        Raises:
            TimeoutException: If page load times out (retryable)
            Exception: For other errors (may or may not be retryable)
        """
        url = f"https://moneypuck.com/preview.htm?id={traditional_game_id}"
        driver = None
        game_lines = []

        try:
            driver = self._create_driver()
            driver.get(url)

            # Wait for both critical elements to load
            wait = WebDriverWait(driver, self.timeout)

            # Wait for the preGameOdds table (opening/closing lines)
            logger.debug(f"Waiting for preGameOdds table to load...")
            wait.until(
                EC.presence_of_element_located((By.ID, 'preGameOdds'))
            )

            # Wait for the headerTable (MoneyPuck win probabilities)
            logger.debug(f"Waiting for headerTable to load...")
            wait.until(
                EC.presence_of_element_located((By.ID, 'headerTable'))
            )

            # Wait for the odds source dropdown
            wait.until(
                EC.presence_of_element_located((By.ID, 'odds_source'))
            )

            # Extract teams and MoneyPuck win probabilities (same for all sportsbooks)
            # Get teams from the first available data
            table = driver.find_element(By.ID, 'preGameOdds')
            nested_tables = table.find_elements(By.TAG_NAME, 'table')

            if not nested_tables:
                # Return empty list with error status
                error_line = GameLine(
                    game_id=game_id,
                    traditional_game_id=traditional_game_id,
                    season=season,
                    sportsbook='unknown',
                    scrape_status='no_data',
                    error_message='No nested odds table found'
                )
                return [error_line]

            odds_table = nested_tables[0]
            rows = odds_table.find_elements(By.TAG_NAME, 'tr')

            if len(rows) < 2:
                error_line = GameLine(
                    game_id=game_id,
                    traditional_game_id=traditional_game_id,
                    season=season,
                    sportsbook='unknown',
                    scrape_status='no_data',
                    error_message='Insufficient rows in odds table'
                )
                return [error_line]

            # Extract teams from first row
            data_row = rows[1]
            tds = data_row.find_elements(By.TAG_NAME, 'td')

            if len(tds) < 1:
                error_line = GameLine(
                    game_id=game_id,
                    traditional_game_id=traditional_game_id,
                    season=season,
                    sportsbook='unknown',
                    scrape_status='no_data',
                    error_message='Insufficient columns in odds table'
                )
                return [error_line]

            # Extract teams
            (_, _, _, away_team, home_team) = self._parse_game_line(tds[0], extract_teams=True)

            # Extract MoneyPuck win probabilities
            (mp_away_win_prob, mp_home_win_prob) = self._parse_win_probabilities(driver)

            # Get available sportsbooks
            sportsbooks = self._get_available_sportsbooks(driver)

            if not sportsbooks:
                error_line = GameLine(
                    game_id=game_id,
                    traditional_game_id=traditional_game_id,
                    season=season,
                    sportsbook='unknown',
                    away_team=away_team,
                    home_team=home_team,
                    mp_away_win_prob=mp_away_win_prob,
                    mp_home_win_prob=mp_home_win_prob,
                    scrape_status='no_data',
                    error_message='No sportsbooks found in dropdown'
                )
                return [error_line]

            # Iterate through each sportsbook
            for sportsbook in sportsbooks:
                logger.debug(f"Scraping {sportsbook} for game {traditional_game_id}")

                # Select the sportsbook
                self._select_sportsbook(driver, sportsbook)

                # Scrape data for this sportsbook
                game_line = self._scrape_single_sportsbook(
                    driver, sportsbook, traditional_game_id, game_id, season,
                    away_team, home_team, mp_away_win_prob, mp_home_win_prob
                )

                game_lines.append(game_line)

            return game_lines

        finally:
            if driver:
                driver.quit()

    def scrape_game(self, traditional_game_id: str, game_id: str, season: str) -> List[GameLine]:
        """
        Scrape opening and closing lines for a single game across ALL sportsbooks with retry logic.

        Args:
            traditional_game_id: The traditional game ID (e.g., "2023020001")
            game_id: The simplified game ID from CSV
            season: The season identifier

        Returns:
            List of GameLine objects, one for each sportsbook
        """
        last_exception = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"Scraping game {traditional_game_id} (attempt {attempt}/{self.max_retries})")
                game_lines = self._scrape_game_attempt(traditional_game_id, game_id, season)

                # Check if we got successful results
                if game_lines and any(gl.scrape_status == 'success' for gl in game_lines):
                    if attempt > 1:
                        logger.info(f"Successfully scraped game {traditional_game_id} after {attempt} attempts")
                    else:
                        logger.info(f"Successfully scraped game {traditional_game_id}")
                    return game_lines

                # If all results are 'no_data', don't retry
                if game_lines and all(gl.scrape_status == 'no_data' for gl in game_lines):
                    logger.warning(f"No data available for game {traditional_game_id}")
                    return game_lines

            except TimeoutException as e:
                last_exception = e
                logger.warning(f"Timeout loading game {traditional_game_id} (attempt {attempt}/{self.max_retries})")

                # If not the last attempt, wait with exponential backoff before retrying
                if attempt < self.max_retries:
                    backoff_time = 2 ** attempt  # 2, 4, 8 seconds
                    logger.info(f"Waiting {backoff_time}s before retry...")
                    time.sleep(backoff_time)

            except Exception as e:
                last_exception = e
                logger.error(f"Error scraping game {traditional_game_id} (attempt {attempt}/{self.max_retries}): {e}")

                # For non-timeout errors, only retry if it might be transient
                if attempt < self.max_retries:
                    backoff_time = 2 ** attempt
                    logger.info(f"Waiting {backoff_time}s before retry...")
                    time.sleep(backoff_time)

        # All retries exhausted - return error result
        error_line = GameLine(
            game_id=game_id,
            traditional_game_id=traditional_game_id,
            season=season,
            sportsbook='unknown'
        )

        if isinstance(last_exception, TimeoutException):
            error_line.scrape_status = 'timeout'
            error_line.error_message = f'Page load timeout after {self.max_retries} attempts'
            logger.error(f"Failed to scrape game {traditional_game_id} after {self.max_retries} attempts (timeout)")
        else:
            error_line.scrape_status = 'error'
            error_line.error_message = f'{str(last_exception)} (after {self.max_retries} attempts)'
            logger.error(f"Failed to scrape game {traditional_game_id} after {self.max_retries} attempts: {last_exception}")

        return [error_line]


def load_game_ids(csv_file: str) -> List[Tuple[str, str, str]]:
    """
    Load game IDs from CSV file.

    Returns:
        List of tuples: (traditional_game_id, game_id, season)
    """
    games = []
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            games.append((
                row['traditional_game_id'],
                row['game_id'],
                row['season']
            ))
    logger.info(f"Loaded {len(games)} games from {csv_file}")
    return games


def load_playoff_games_from_db(db_path: str, season: Optional[str] = None) -> List[Tuple[str, str, str]]:
    """
    Load playoff game IDs directly from the database.
    Playoff games have game_id with format YYYY03XXXX where the '03' indicates playoffs.
    Regular season games use '02', preseason uses '01', all-star is '04'.

    Args:
        db_path: Path to the nhl_analytics.db database
        season: Optional season filter (e.g., "2024-2025"). If None, loads all playoff games.

    Returns:
        List of tuples: (traditional_game_id, game_id, season)
    """
    games = []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        if season:
            # Extract the first year from the season (e.g., "2024" from "2024-2025")
            year_prefix = season.split('-')[0]
            query = """
                SELECT game_id, season
                FROM games
                WHERE game_id LIKE ?
                AND season = ?
                ORDER BY game_id
            """
            # Pattern: year + '03' + any 4 digits (e.g., '202403____')
            pattern = f"{year_prefix}03%"
            cursor.execute(query, (pattern, season))
        else:
            # Load all playoff games
            # Use SUBSTR to check positions 5-6 are '03'
            query = """
                SELECT game_id, season
                FROM games
                WHERE SUBSTR(game_id, 5, 2) = '03'
                ORDER BY game_id
            """
            cursor.execute(query)

        rows = cursor.fetchall()

        for row in rows:
            game_id_full = row[0]  # e.g., "2024030176"
            season_val = row[1]    # e.g., "2024-2025"

            # The traditional_game_id is the same as game_id from the database
            traditional_game_id = game_id_full

            # For consistency with CSV format, we might want a simplified game_id
            # Extract the last 5 digits (e.g., "30176" from "2024030176")
            simplified_game_id = game_id_full[-5:]

            games.append((
                traditional_game_id,
                simplified_game_id,
                season_val
            ))

        logger.info(f"Loaded {len(games)} playoff games from database" +
                   (f" for season {season}" if season else ""))

    finally:
        conn.close()

    return games


def save_results(results: List[GameLine], output_file: str):
    """
    Save scraping results to CSV file in wide format.
    Each game gets one row with columns for each sportsbook's data.
    """
    # Group results by game
    games_dict = {}
    all_sportsbooks = set()

    for result in results:
        game_key = (result.game_id, result.traditional_game_id, result.season)

        if game_key not in games_dict:
            games_dict[game_key] = {
                'game_id': result.game_id,
                'traditional_game_id': result.traditional_game_id,
                'season': result.season,
                'away_team': result.away_team,
                'home_team': result.home_team,
                'mp_away_win_prob': result.mp_away_win_prob,
                'mp_home_win_prob': result.mp_home_win_prob,
                'scrape_status': result.scrape_status,
                'error_message': result.error_message,
                'sportsbooks': {}
            }

        # Update game-level status if there's an error
        # Priority: error > timeout > no_data > success
        # This ensures we capture the most severe issue for the game
        current_status = games_dict[game_key]['scrape_status']
        if result.scrape_status == 'error' or \
           (result.scrape_status == 'timeout' and current_status not in ['error']) or \
           (result.scrape_status == 'no_data' and current_status not in ['error', 'timeout']):
            games_dict[game_key]['scrape_status'] = result.scrape_status
            games_dict[game_key]['error_message'] = result.error_message

        # Add sportsbook data
        if result.sportsbook and result.sportsbook != 'unknown':
            all_sportsbooks.add(result.sportsbook)
            games_dict[game_key]['sportsbooks'][result.sportsbook] = {
                'opening_timestamp': result.opening_timestamp,
                'opening_away_odds': result.opening_away_odds,
                'opening_home_odds': result.opening_home_odds,
                'closing_timestamp': result.closing_timestamp,
                'closing_away_odds': result.closing_away_odds,
                'closing_home_odds': result.closing_home_odds
            }

    # Sort sportsbooks alphabetically for consistent column ordering
    sorted_sportsbooks = sorted(all_sportsbooks)

    # Build header
    fieldnames = [
        'game_id', 'traditional_game_id', 'season',
        'away_team', 'home_team',
        'mp_away_win_prob', 'mp_home_win_prob',
        'scrape_status', 'error_message'
    ]

    # Add columns for each sportsbook
    for sportsbook in sorted_sportsbooks:
        fieldnames.extend([
            f'{sportsbook}_opening_timestamp',
            f'{sportsbook}_opening_away_odds',
            f'{sportsbook}_opening_home_odds',
            f'{sportsbook}_closing_timestamp',
            f'{sportsbook}_closing_away_odds',
            f'{sportsbook}_closing_home_odds'
        ])

    # Write CSV
    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        # Sort games by game_id for consistent ordering
        sorted_games = sorted(games_dict.items(), key=lambda x: x[0])

        for game_key, game_data in sorted_games:
            row = {
                'game_id': game_data['game_id'],
                'traditional_game_id': game_data['traditional_game_id'],
                'season': game_data['season'],
                'away_team': game_data['away_team'],
                'home_team': game_data['home_team'],
                'mp_away_win_prob': game_data['mp_away_win_prob'],
                'mp_home_win_prob': game_data['mp_home_win_prob'],
                'scrape_status': game_data['scrape_status'],
                'error_message': game_data['error_message'] or ''
            }

            # Add sportsbook data
            for sportsbook in sorted_sportsbooks:
                if sportsbook in game_data['sportsbooks']:
                    sb_data = game_data['sportsbooks'][sportsbook]
                    row[f'{sportsbook}_opening_timestamp'] = sb_data['opening_timestamp'] or ''
                    row[f'{sportsbook}_opening_away_odds'] = sb_data['opening_away_odds'] or ''
                    row[f'{sportsbook}_opening_home_odds'] = sb_data['opening_home_odds'] or ''
                    row[f'{sportsbook}_closing_timestamp'] = sb_data['closing_timestamp'] or ''
                    row[f'{sportsbook}_closing_away_odds'] = sb_data['closing_away_odds'] or ''
                    row[f'{sportsbook}_closing_home_odds'] = sb_data['closing_home_odds'] or ''
                else:
                    # No data for this sportsbook - leave empty
                    row[f'{sportsbook}_opening_timestamp'] = ''
                    row[f'{sportsbook}_opening_away_odds'] = ''
                    row[f'{sportsbook}_opening_home_odds'] = ''
                    row[f'{sportsbook}_closing_timestamp'] = ''
                    row[f'{sportsbook}_closing_away_odds'] = ''
                    row[f'{sportsbook}_closing_home_odds'] = ''

            writer.writerow(row)

    logger.info(f"Saved {len(games_dict)} games ({len(results)} total sportsbook entries) to {output_file}")


def update_unique_game_ids(season: str) -> str:
    """
    Update the unique_game_ids CSV file for a given season.

    Args:
        season: Season string (e.g., "2024-25")

    Returns:
        Path to the updated CSV file
    """
    # Extract start year from season string
    start_year = int(season.split('-')[0])

    logger.info(f"Updating unique game IDs for season {season}...")

    # Use game_list.py logic to scrape and save
    # scrape_season_game_ids now returns (dataframe, filename)
    df, output_file = scrape_season_game_ids(start_year)

    logger.info(f"Updated {output_file} with {len(df)} games")

    return output_file


def get_scraped_game_ids(moneypuck_csv: str) -> Set[str]:
    """
    Get set of traditional_game_ids that have already been scraped.

    Args:
        moneypuck_csv: Path to existing moneypuck_odds CSV file

    Returns:
        Set of traditional_game_ids that exist in the file
    """
    if not Path(moneypuck_csv).exists():
        logger.info(f"File {moneypuck_csv} does not exist, will scrape all games")
        return set()

    scraped_ids = set()
    with open(moneypuck_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            scraped_ids.add(row['traditional_game_id'])

    logger.info(f"Found {len(scraped_ids)} already scraped games in {moneypuck_csv}")
    return scraped_ids


def identify_timeout_games(moneypuck_odds_csv: str) -> List[Tuple[str, str, str]]:
    """
    Identify games with timeout or error status in the moneypuck_odds CSV.

    Args:
        moneypuck_odds_csv: Path to moneypuck_odds CSV file

    Returns:
        List of tuples: (traditional_game_id, game_id, season) for timed-out/errored games
    """
    if not Path(moneypuck_odds_csv).exists():
        logger.error(f"File {moneypuck_odds_csv} does not exist")
        return []

    timeout_games = []
    with open(moneypuck_odds_csv, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('scrape_status') in ('timeout', 'error'):
                timeout_games.append((
                    row['traditional_game_id'],
                    row['game_id'],
                    row['season']
                ))

    logger.info(f"Found {len(timeout_games)} timed-out/errored games in {moneypuck_odds_csv}")
    return timeout_games


def identify_missing_games_from_lists(unique_game_ids_csv: str, moneypuck_odds_csv: str) -> List[Tuple[str, str, str]]:
    """
    Identify games that are in unique_game_ids but not yet in moneypuck_odds.

    Args:
        unique_game_ids_csv: Path to unique_game_ids CSV file
        moneypuck_odds_csv: Path to moneypuck_odds CSV file

    Returns:
        List of tuples: (traditional_game_id, game_id, season) for missing games
    """
    # Get already scraped games
    scraped_ids = get_scraped_game_ids(moneypuck_odds_csv)

    # Load all games from unique_game_ids
    all_games = load_game_ids(unique_game_ids_csv)

    # Filter to only those not yet scraped
    missing_games = [
        (trad_id, game_id, season)
        for trad_id, game_id, season in all_games
        if trad_id not in scraped_ids
    ]

    logger.info(f"Found {len(missing_games)} games to scrape (not yet in {moneypuck_odds_csv})")
    return missing_games


def scrape_season(csv_file: Optional[str] = None, games_list: Optional[List[Tuple[str, str, str]]] = None,
                  output_file: str = None, max_workers: int = 10,
                  headless: bool = True, delay: float = 0.33, max_retries: int = 3,
                  timeout: int = 20):
    """
    Scrape all games from a season using multithreading.

    Args:
        csv_file: Path to CSV file with game IDs (optional if games_list provided)
        games_list: List of game tuples (traditional_game_id, game_id, season) (optional if csv_file provided)
        output_file: Path to output CSV file
        max_workers: Maximum number of concurrent threads
        headless: Whether to run browser in headless mode
        delay: Delay between requests (seconds) to be respectful
        max_retries: Maximum number of retry attempts for failed requests
        timeout: Timeout in seconds for element waits (page load timeout is 2x this)
    """
    if csv_file:
        logger.info(f"Starting scrape of {csv_file}")
        games = load_game_ids(csv_file)
    elif games_list:
        logger.info(f"Starting scrape of {len(games_list)} games from provided list")
        games = games_list
    else:
        raise ValueError("Either csv_file or games_list must be provided")

    logger.info(f"Max workers: {max_workers}, Headless: {headless}, Delay: {delay}s, Max retries: {max_retries}, Timeout: {timeout}s")

    results = []

    scraper = MoneyPuckScraper(headless=headless, max_retries=max_retries, timeout=timeout)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_game = {
            executor.submit(scraper.scrape_game, trad_id, game_id, season): (trad_id, game_id, season)
            for trad_id, game_id, season in games
        }

        # Process completed tasks
        for i, future in enumerate(as_completed(future_to_game), 1):
            trad_id, game_id, season = future_to_game[future]
            try:
                game_lines = future.result()  # Now returns a list of GameLines
                results.extend(game_lines)  # Add all sportsbook results

                # Log summary
                success_count = sum(1 for gl in game_lines if gl.scrape_status == 'success')
                logger.info(f"Progress: {i}/{len(games)} - Game {trad_id}: {success_count}/{len(game_lines)} sportsbooks successful")
            except Exception as e:
                logger.error(f"Unexpected error for game {trad_id}: {e}")
                results.append(GameLine(
                    game_id=game_id,
                    traditional_game_id=trad_id,
                    season=season,
                    sportsbook='unknown',
                    scrape_status='error',
                    error_message=str(e)
                ))

            # Add delay between requests
            if i < len(games):
                time.sleep(delay)

    # Save results
    save_results(results, output_file)

    # Print summary
    status_counts = {}
    for result in results:
        status_counts[result.scrape_status] = status_counts.get(result.scrape_status, 0) + 1

    logger.info("=" * 60)
    logger.info("SCRAPING SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total games: {len(results)}")
    for status, count in sorted(status_counts.items()):
        logger.info(f"  {status}: {count}")
    logger.info(f"Output saved to: {output_file}")
    logger.info("=" * 60)


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Scrape MoneyPuck game lines')
    parser.add_argument('--season', type=str, default='2025-26', # set to current season
                        help='Specific season to scrape (e.g., 2023-24). If not specified, scrapes all available seasons.')
    parser.add_argument('--update-season', action='store_true',
                        help='Update unique_game_ids for the season, then scrape only games not yet in moneypuck_odds CSV')
    parser.add_argument('--retry-timeouts', action='store_true',
                        help='Re-scrape games with timeout or error status in the existing moneypuck_odds CSV')
    parser.add_argument('--playoffs', action='store_true',
                        help='Scrape playoff games from database instead of regular season games from CSV')
    parser.add_argument('--db-path', type=str, default='nhl_analytics.db',
                        help='Path to nhl_analytics.db database (default: nhl_analytics.db)')
    parser.add_argument('--workers', type=int, default=4,
                        help='Number of concurrent threads (default: 4)')
    parser.add_argument('--delay', type=float, default=0.5,
                        help='Delay between requests in seconds (default: 0.5)')
    parser.add_argument('--retries', type=int, default=3,
                        help='Maximum number of retry attempts for failed requests (default: 3)')
    parser.add_argument('--timeout', type=int, default=20,
                        help='Timeout in seconds for element waits; page load timeout is 2x this (default: 20)')
    parser.add_argument('--no-headless', action='store_true',
                        help='Run browser in non-headless mode (visible)')

    args = parser.parse_args()

    # If update-season mode, handle it and exit
    if args.update_season:
        if not args.season:
            logger.error("--season is required when using --update-season")
            exit(1)

        logger.info("=" * 60)
        logger.info(f"UPDATE SEASON MODE - Season {args.season}")
        logger.info("=" * 60)

        # Step 1: Update unique_game_ids
        unique_game_ids_file = update_unique_game_ids(args.season)

        # Step 2: Identify missing games
        moneypuck_odds_file = f"moneypuck_odds_{args.season}.csv"
        missing_games = identify_missing_games_from_lists(unique_game_ids_file, moneypuck_odds_file)

        if not missing_games:
            logger.info("No missing games to scrape. All games in unique_game_ids are already in moneypuck_odds.")
            exit(0)

        logger.info(f"Found {len(missing_games)} games to scrape")

        # Step 3: Scrape missing games
        scraper = MoneyPuckScraper(headless=not args.no_headless, max_retries=args.retries, timeout=args.timeout)
        results = []

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_game = {
                executor.submit(scraper.scrape_game, trad_id, game_id, season): (trad_id, game_id, season)
                for trad_id, game_id, season in missing_games
            }

            for i, future in enumerate(as_completed(future_to_game), 1):
                trad_id, game_id, season = future_to_game[future]
                try:
                    game_lines = future.result()
                    results.extend(game_lines)

                    success_count = sum(1 for gl in game_lines if gl.scrape_status == 'success')
                    logger.info(f"Progress: {i}/{len(missing_games)} - Game {trad_id}: {success_count}/{len(game_lines)} sportsbooks successful")
                except Exception as e:
                    logger.error(f"Unexpected error for game {trad_id}: {e}")
                    results.append(GameLine(
                        game_id=game_id,
                        traditional_game_id=trad_id,
                        season=season,
                        sportsbook='unknown',
                        scrape_status='error',
                        error_message=str(e)
                    ))

                if i < len(missing_games):
                    time.sleep(args.delay)

        # Step 4: Merge with existing data or create new file
        if Path(moneypuck_odds_file).exists():
            # Load existing data
            existing_data = {}
            with open(moneypuck_odds_file, 'r') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    game_key = (row['game_id'], row['traditional_game_id'], row['season'])
                    existing_data[game_key] = row

            # Add new results
            # Group new results by game
            new_games_dict = {}
            all_sportsbooks = set()

            for result in results:
                game_key = (result.game_id, result.traditional_game_id, result.season)

                if game_key not in new_games_dict:
                    new_games_dict[game_key] = {
                        'game_id': result.game_id,
                        'traditional_game_id': result.traditional_game_id,
                        'season': result.season,
                        'away_team': result.away_team,
                        'home_team': result.home_team,
                        'mp_away_win_prob': result.mp_away_win_prob,
                        'mp_home_win_prob': result.mp_home_win_prob,
                        'scrape_status': result.scrape_status,
                        'error_message': result.error_message,
                        'sportsbooks': {}
                    }

                if result.sportsbook and result.sportsbook != 'unknown':
                    all_sportsbooks.add(result.sportsbook)
                    new_games_dict[game_key]['sportsbooks'][result.sportsbook] = {
                        'opening_timestamp': result.opening_timestamp,
                        'opening_away_odds': result.opening_away_odds,
                        'opening_home_odds': result.opening_home_odds,
                        'closing_timestamp': result.closing_timestamp,
                        'closing_away_odds': result.closing_away_odds,
                        'closing_home_odds': result.closing_home_odds
                    }

            # Convert new games to row format
            for game_key, game_data in new_games_dict.items():
                row = {field: '' for field in fieldnames}
                row['game_id'] = game_data['game_id']
                row['traditional_game_id'] = game_data['traditional_game_id']
                row['season'] = game_data['season']
                row['away_team'] = game_data['away_team'] or ''
                row['home_team'] = game_data['home_team'] or ''
                row['mp_away_win_prob'] = game_data['mp_away_win_prob'] or ''
                row['mp_home_win_prob'] = game_data['mp_home_win_prob'] or ''
                row['scrape_status'] = game_data['scrape_status']
                row['error_message'] = game_data['error_message'] or ''

                for sportsbook, sb_data in game_data['sportsbooks'].items():
                    row[f'{sportsbook}_opening_timestamp'] = sb_data['opening_timestamp'] or ''
                    row[f'{sportsbook}_opening_away_odds'] = sb_data['opening_away_odds'] or ''
                    row[f'{sportsbook}_opening_home_odds'] = sb_data['opening_home_odds'] or ''
                    row[f'{sportsbook}_closing_timestamp'] = sb_data['closing_timestamp'] or ''
                    row[f'{sportsbook}_closing_away_odds'] = sb_data['closing_away_odds'] or ''
                    row[f'{sportsbook}_closing_home_odds'] = sb_data['closing_home_odds'] or ''

                existing_data[game_key] = row

            # Rebuild fieldnames from all rows to include any new sportsbooks
            # (e.g., fanduel/betano may not exist in older CSV but appear in new scrapes)
            base_fields = [
                'game_id', 'traditional_game_id', 'season',
                'away_team', 'home_team',
                'mp_away_win_prob', 'mp_home_win_prob',
                'scrape_status', 'error_message'
            ]
            all_keys = set()
            for row in existing_data.values():
                all_keys.update(row.keys())
            sportsbook_fields = sorted(k for k in all_keys if k not in base_fields)
            fieldnames = base_fields + sportsbook_fields

            # Write merged data back
            with open(moneypuck_odds_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                for game_key in sorted(existing_data.keys()):
                    writer.writerow(existing_data[game_key])

            logger.info(f"Merged {len(new_games_dict)} new games into {moneypuck_odds_file}")
        else:
            # No existing file, just save new results
            save_results(results, moneypuck_odds_file)

        logger.info("=" * 60)
        logger.info("UPDATE SEASON MODE COMPLETE")
        logger.info("=" * 60)
        exit(0)

    # Handle retry-timeouts mode
    if args.retry_timeouts:
        season = args.season  # defaults to '2025-26'
        moneypuck_odds_file = f"moneypuck_odds_{season}.csv"

        logger.info("=" * 60)
        logger.info(f"RETRY TIMEOUTS MODE - Season {season}")
        logger.info("=" * 60)

        # Step 1: Identify timed-out/errored games
        timeout_games = identify_timeout_games(moneypuck_odds_file)

        if not timeout_games:
            logger.info("No timed-out or errored games to retry.")
            exit(0)

        logger.info(f"Found {len(timeout_games)} games to retry")

        # Step 2: Re-scrape those games
        scraper = MoneyPuckScraper(headless=not args.no_headless, max_retries=args.retries, timeout=args.timeout)
        results = []

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_game = {
                executor.submit(scraper.scrape_game, trad_id, game_id, season_str): (trad_id, game_id, season_str)
                for trad_id, game_id, season_str in timeout_games
            }

            for i, future in enumerate(as_completed(future_to_game), 1):
                trad_id, game_id, season_str = future_to_game[future]
                try:
                    game_lines = future.result()
                    results.extend(game_lines)

                    success_count = sum(1 for gl in game_lines if gl.scrape_status == 'success')
                    logger.info(f"Progress: {i}/{len(timeout_games)} - Game {trad_id}: {success_count}/{len(game_lines)} sportsbooks successful")
                except Exception as e:
                    logger.error(f"Unexpected error for game {trad_id}: {e}")
                    results.append(GameLine(
                        game_id=game_id,
                        traditional_game_id=trad_id,
                        season=season_str,
                        sportsbook='unknown',
                        scrape_status='error',
                        error_message=str(e)
                    ))

                if i < len(timeout_games):
                    time.sleep(args.delay)

        # Step 3: Merge back into existing CSV, replacing old timed-out rows
        existing_data = {}
        with open(moneypuck_odds_file, 'r') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                game_key = (row['game_id'], row['traditional_game_id'], row['season'])
                existing_data[game_key] = row

        # Group new results by game
        new_games_dict = {}
        all_sportsbooks = set()

        for result in results:
            game_key = (result.game_id, result.traditional_game_id, result.season)

            if game_key not in new_games_dict:
                new_games_dict[game_key] = {
                    'game_id': result.game_id,
                    'traditional_game_id': result.traditional_game_id,
                    'season': result.season,
                    'away_team': result.away_team,
                    'home_team': result.home_team,
                    'mp_away_win_prob': result.mp_away_win_prob,
                    'mp_home_win_prob': result.mp_home_win_prob,
                    'scrape_status': result.scrape_status,
                    'error_message': result.error_message,
                    'sportsbooks': {}
                }

            if result.sportsbook and result.sportsbook != 'unknown':
                all_sportsbooks.add(result.sportsbook)
                new_games_dict[game_key]['sportsbooks'][result.sportsbook] = {
                    'opening_timestamp': result.opening_timestamp,
                    'opening_away_odds': result.opening_away_odds,
                    'opening_home_odds': result.opening_home_odds,
                    'closing_timestamp': result.closing_timestamp,
                    'closing_away_odds': result.closing_away_odds,
                    'closing_home_odds': result.closing_home_odds
                }

        # Replace old timed-out rows with new results
        for game_key, game_data in new_games_dict.items():
            row = {field: '' for field in fieldnames}
            row['game_id'] = game_data['game_id']
            row['traditional_game_id'] = game_data['traditional_game_id']
            row['season'] = game_data['season']
            row['away_team'] = game_data['away_team'] or ''
            row['home_team'] = game_data['home_team'] or ''
            row['mp_away_win_prob'] = game_data['mp_away_win_prob'] or ''
            row['mp_home_win_prob'] = game_data['mp_home_win_prob'] or ''
            row['scrape_status'] = game_data['scrape_status']
            row['error_message'] = game_data['error_message'] or ''

            for sportsbook, sb_data in game_data['sportsbooks'].items():
                row[f'{sportsbook}_opening_timestamp'] = sb_data['opening_timestamp'] or ''
                row[f'{sportsbook}_opening_away_odds'] = sb_data['opening_away_odds'] or ''
                row[f'{sportsbook}_opening_home_odds'] = sb_data['opening_home_odds'] or ''
                row[f'{sportsbook}_closing_timestamp'] = sb_data['closing_timestamp'] or ''
                row[f'{sportsbook}_closing_away_odds'] = sb_data['closing_away_odds'] or ''
                row[f'{sportsbook}_closing_home_odds'] = sb_data['closing_home_odds'] or ''

            existing_data[game_key] = row

        # Rebuild fieldnames to include any new sportsbooks
        base_fields = [
            'game_id', 'traditional_game_id', 'season',
            'away_team', 'home_team',
            'mp_away_win_prob', 'mp_home_win_prob',
            'scrape_status', 'error_message'
        ]
        all_keys = set()
        for row in existing_data.values():
            all_keys.update(row.keys())
        sportsbook_fields = sorted(k for k in all_keys if k not in base_fields)
        fieldnames = base_fields + sportsbook_fields

        # Write merged data back
        with open(moneypuck_odds_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for game_key in sorted(existing_data.keys()):
                writer.writerow(existing_data[game_key])

        # Summary
        retry_success = sum(1 for gd in new_games_dict.values() if gd['scrape_status'] == 'success')
        retry_still_failed = len(new_games_dict) - retry_success
        logger.info(f"Retried {len(new_games_dict)} games: {retry_success} now successful, {retry_still_failed} still failed")
        logger.info(f"Results merged back into {moneypuck_odds_file}")

        logger.info("=" * 60)
        logger.info("RETRY TIMEOUTS MODE COMPLETE")
        logger.info("=" * 60)
        exit(0)

    # Handle playoff mode
    if args.playoffs:
        logger.info("=" * 60)
        logger.info("PLAYOFF MODE - Loading games from database")
        logger.info("=" * 60)

        # Check database exists
        if not Path(args.db_path).exists():
            logger.error(f"Database not found: {args.db_path}")
            exit(1)

        # Load playoff games
        playoff_games = load_playoff_games_from_db(args.db_path, season=args.season)

        if not playoff_games:
            logger.error(f"No playoff games found in database" +
                        (f" for season {args.season}" if args.season else ""))
            exit(1)

        # Determine output filename
        if args.season:
            output_file = f"moneypuck_odds_{args.season}_playoffs.csv"
            season_label = args.season
        else:
            output_file = "moneypuck_odds_all_playoffs.csv"
            season_label = "all seasons"

        logger.info("")
        logger.info("=" * 60)
        logger.info(f"STARTING PLAYOFF SCRAPE: {season_label}")
        logger.info(f"Found {len(playoff_games)} playoff games")
        logger.info("=" * 60)

        scrape_season(
            games_list=playoff_games,
            output_file=output_file,
            max_workers=args.workers,
            headless=not args.no_headless,
            delay=args.delay,
            max_retries=args.retries,
            timeout=args.timeout
        )

        logger.info("")
        logger.info("=" * 60)
        logger.info("PLAYOFF SCRAPE COMPLETE")
        logger.info("=" * 60)
        exit(0)

    # Regular season mode - determine which seasons to scrape
    if args.season:
        # Specific season requested
        seasons_to_scrape = [args.season]
    else:
        # Find all existing season files to determine available seasons
        season_files = sorted(glob.glob("unique_game_ids_20*.csv"))

        if not season_files:
            logger.error("No unique_game_ids CSV files found. Please specify a season with --season")
            exit(1)

        # Extract season strings from filenames
        seasons_to_scrape = []
        for file in season_files:
            filename = Path(file).stem
            if '_full' in filename or '_missing' in filename:
                continue  # Skip variants

            season_part = filename.replace('unique_game_ids_', '')
            # Only process 2023-24 and later (when MoneyPuck started including this data)
            if season_part >= '2023-24':
                seasons_to_scrape.append(season_part)

        if not seasons_to_scrape:
            logger.error("No valid season files found (2023-24 or later)")
            exit(1)

    logger.info("=" * 60)
    logger.info(f"Found {len(seasons_to_scrape)} season(s) to scrape:")
    for season in seasons_to_scrape:
        logger.info(f"  - {season}")
    logger.info("=" * 60)

    # Scrape each season
    for season in seasons_to_scrape:
        logger.info("")
        logger.info("=" * 60)
        logger.info(f"STARTING SEASON: {season}")
        logger.info("=" * 60)

        # Always update unique_game_ids file first to ensure fresh data
        logger.info(f"Updating unique_game_ids for {season}...")
        input_file = update_unique_game_ids(season)

        output_file = f"moneypuck_odds_{season}.csv"

        scrape_season(
            csv_file=input_file,
            output_file=output_file,
            max_workers=args.workers,
            headless=not args.no_headless,
            delay=args.delay,
            max_retries=args.retries,
            timeout=args.timeout
        )

    logger.info("")
    logger.info("=" * 60)
    logger.info("ALL SEASONS COMPLETE")
    logger.info("=" * 60)
