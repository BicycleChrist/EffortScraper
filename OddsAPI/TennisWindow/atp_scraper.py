#!/usr/bin/env python3
"""
ATP Historical Rankings Scraper
Scrapes last 3 years of ATP rankings and stores in tennis_rankings.db
Single table design with date column for efficient storage.
"""

import sqlite3
import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime, timedelta
import logging
from typing import List, Tuple, Optional
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ATPRankingsScraper:
    def __init__(self, db_path: str = "tennis_rankings.db"):
        self.db_path = db_path
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.init_database()
    
    def init_database(self):
        """Initialize the tennis rankings database with a single table."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Single table for all rankings data
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS rankings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ranking_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            points INTEGER NOT NULL,
            age INTEGER DEFAULT 0,
            tournaments_played INTEGER DEFAULT 0,
            rank_change INTEGER DEFAULT NULL,
            UNIQUE(ranking_date, rank) ON CONFLICT REPLACE
        )
        ''')
        
        # Index for efficient queries
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_date_rank ON rankings(ranking_date, rank)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_player_date ON rankings(player_name, ranking_date)')
        
        conn.commit()
        conn.close()
        logger.info(f"Database initialized: {self.db_path}")
    
    def get_monday_dates_last_3_years(self) -> List[str]:
        """Generate list of Monday dates for last 3 years (ATP updates on Mondays)."""
        dates = []
        today = datetime.now()
        
        # Start from 3 years ago
        start_date = today - timedelta(days=3*365)
        
        # Find the first Monday
        days_ahead = 0 - start_date.weekday()  # Monday is 0
        if days_ahead <= 0:
            days_ahead += 7
        start_monday = start_date + timedelta(days=days_ahead)
        
        current_date = start_monday
        while current_date <= today:
            dates.append(current_date.strftime('%Y-%m-%d'))
            current_date += timedelta(days=7)  # Next Monday
        
        return dates
    
    def check_existing_data(self, date: str) -> bool:
        """Check if data already exists for a given date."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM rankings WHERE ranking_date = ?', (date,))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    
    def scrape_rankings_for_date(self, date: str, max_players: int = 2000) -> List[Tuple]:
        """Scrape ATP rankings for a specific date."""
        # Convert date to ATP URL format
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        url = f"https://www.atptour.com/en/rankings/singles?dateWeek={date}&rankRange=0-{max_players}"
        
        logger.info(f"Scraping rankings for {date}: {url}")
        
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            rankings_data = []
            ranking_rows = soup.find_all('tr')
            
            for row in ranking_rows:
                try:
                    cells = row.find_all(['td', 'th'])
                    if len(cells) < 3:
                        continue
                    
                    # Get rank from first cell
                    rank_text = cells[0].get_text(strip=True)
                    if not rank_text.isdigit():
                        continue
                    rank_num = int(rank_text)
                    
                    # Get player name
                    name = ""
                    for cell in cells[1:4]:
                        player_link = cell.find('a', href=True)
                        if player_link and '/players/' in player_link.get('href', ''):
                            name = player_link.get_text(strip=True)
                            break
                    
                    if not name:
                        continue
                    
                    # Get points
                    points = 0
                    for cell in cells[2:]:
                        cell_text = cell.get_text(strip=True)
                        clean_text = cell_text.replace(',', '').replace('.', '')
                        if clean_text.isdigit() and int(clean_text) > 100:
                            points = int(clean_text)
                            break
                    
                    # Get rank change (from ATP's weekly change indicator)
                    rank_change = None
                    for cell in cells:
                        rank_up_span = cell.find('span', class_='rank-up')
                        rank_down_span = cell.find('span', class_='rank-down')
                        if rank_up_span:
                            change_text = rank_up_span.get_text(strip=True)
                            if change_text.startswith('+'):
                                try:
                                    rank_change = int(change_text[1:])
                                except ValueError:
                                    pass
                        elif rank_down_span:
                            change_text = rank_down_span.get_text(strip=True)
                            if change_text.startswith('-'):
                                try:
                                    rank_change = int(change_text)  # Already negative
                                except ValueError:
                                    pass
                    
                    if name and points > 0:
                        rankings_data.append((date, rank_num, name, points, rank_change))
                        
                        if len(rankings_data) >= max_players:
                            break
                
                except (ValueError, AttributeError) as e:
                    continue
            
            logger.info(f"Scraped {len(rankings_data)} rankings for {date}")
            return rankings_data
        
        except Exception as e:
            logger.error(f"Error scraping rankings for {date}: {e}")
            return []
    
    def save_rankings_to_db(self, rankings_data: List[Tuple]):
        """Save rankings data to the database."""
        if not rankings_data:
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Insert rankings data
        cursor.executemany('''
        INSERT OR REPLACE INTO rankings (ranking_date, rank, player_name, points, rank_change)
        VALUES (?, ?, ?, ?, ?)
        ''', rankings_data)
        
        conn.commit()
        conn.close()
        logger.info(f"Saved {len(rankings_data)} rankings to database")
    
    def scrape_all_historical_data(self, delay_seconds: float = 1.0):
        """Scrape all historical data for the last 3 years."""
        dates = self.get_monday_dates_last_3_years()
        logger.info(f"Will scrape {len(dates)} weeks of rankings data")
        
        successful_scrapes = 0
        skipped_scrapes = 0
        
        for i, date in enumerate(dates):
            # Check if we already have data for this date
            if self.check_existing_data(date):
                logger.info(f"Skipping {date} - data already exists ({i+1}/{len(dates)})")
                skipped_scrapes += 1
                continue
            
            rankings_data = self.scrape_rankings_for_date(date)
            
            if rankings_data:
                self.save_rankings_to_db(rankings_data)
                successful_scrapes += 1
                logger.info(f"✅ Completed {date} ({i+1}/{len(dates)}) - {successful_scrapes} successful, {skipped_scrapes} skipped")
            else:
                logger.warning(f"❌ Failed to scrape {date} ({i+1}/{len(dates)})")
            
            # Rate limiting
            if i < len(dates) - 1:  # Don't sleep after the last request
                time.sleep(delay_seconds)
        
        logger.info(f"Scraping completed! {successful_scrapes} successful, {skipped_scrapes} skipped")
    
    def get_player_ranking_history(self, player_name: str, limit: int = 52) -> List[Tuple]:
        """Get ranking history for a specific player (for testing)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT ranking_date, rank, points, rank_change 
        FROM rankings 
        WHERE player_name LIKE ? 
        ORDER BY ranking_date DESC 
        LIMIT ?
        ''', (f'%{player_name}%', limit))
        
        results = cursor.fetchall()
        conn.close()
        return results
    
    def get_database_stats(self):
        """Print some database statistics."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Total records
        cursor.execute('SELECT COUNT(*) FROM rankings')
        total_records = cursor.fetchone()[0]
        
        # Date range
        cursor.execute('SELECT MIN(ranking_date), MAX(ranking_date) FROM rankings')
        date_range = cursor.fetchone()
        
        # Unique players
        cursor.execute('SELECT COUNT(DISTINCT player_name) FROM rankings')
        unique_players = cursor.fetchone()[0]
        
        # Weeks of data
        cursor.execute('SELECT COUNT(DISTINCT ranking_date) FROM rankings')
        weeks_of_data = cursor.fetchone()[0]
        
        conn.close()
        
        print(f"\n📊 Database Statistics:")
        print(f"  Total records: {total_records:,}")
        print(f"  Date range: {date_range[0]} to {date_range[1]}")
        print(f"  Unique players: {unique_players:,}")
        print(f"  Weeks of data: {weeks_of_data}")

def main():
    """Main function to run the historical scraper."""
    scraper = ATPRankingsScraper()
    
    print("🎾 ATP Historical Rankings Scraper")
    print("This will scrape the last 3 years of ATP rankings data...")
    
    # Check current database state
    scraper.get_database_stats()
    
    # Ask user if they want to proceed
    response = input("\nProceed with scraping? (y/n): ").lower().strip()
    if response != 'y':
        print("Scraping cancelled.")
        return
    
    # Start scraping
    start_time = time.time()
    scraper.scrape_all_historical_data(delay_seconds=1.5)  # Be nice to ATP servers
    end_time = time.time()
    
    print(f"\n⏱️ Scraping completed in {end_time - start_time:.1f} seconds")
    
    # Show final stats
    scraper.get_database_stats()
    
    # Test with a sample player
    print("\n🧪 Testing with sample player data:")
    djokovic_history = scraper.get_player_ranking_history("Djokovic", limit=10)
    if djokovic_history:
        print("Last 10 Djokovic rankings:")
        for date, rank, points, rank_change in djokovic_history:
            change_str = f" ({rank_change:+d})" if rank_change is not None else ""
            print(f"  {date}: #{rank} ({points:,} points){change_str}")

if __name__ == "__main__":
    main()