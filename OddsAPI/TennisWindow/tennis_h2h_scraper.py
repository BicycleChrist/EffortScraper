import asyncio
import requests
from bs4 import BeautifulSoup
import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from atp_scraper import ATPRankingsScraper
import aiohttp

@dataclass
class TournamentMatch:
    tournament_name: str
    opponent: str
    score: str
    round: str
    date: str
    result: str  # "Win" or "Loss"

@dataclass
class PlayerRanking:
    rank: int
    name: str
    points: int
    age: int = 0
    tournaments_played: int = 0
    next_best: Optional[int] = None

@dataclass
class PlayerStats:
    name: str
    win_loss_record: str
    recent_form: List[str]  # ['W', 'L', 'W', ...]
    yearly_stats: Dict[str, Dict[str, str]]  # {year: {surface: record}}
    recent_tournaments: List[TournamentMatch]
    current_ranking: Optional[PlayerRanking] = None

@dataclass
class H2HMatch:
    date: str
    tournament: str
    surface: str
    winner: str
    loser: str
    score: str
    round: str

@dataclass
class H2HComparison:
    player1: PlayerStats
    player2: PlayerStats
    head_to_head: str  # "4:9"
    matches_combined: Dict[str, Tuple[str, str]]  # {stat_name: (p1_value, p2_value)}
    match_history: List[Dict[str, str]]  # Recent matches (not H2H)
    h2h_history: List[H2HMatch]  # Actual head-to-head match history
    ranking_gap: Optional[int] = None
    points_gap: Optional[int] = None

class TennisScraper:
    def __init__(self, use_async=None, db_path="tennis_rankings.db"):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        self.atp_rankings_cache = {}
        self.cache_timestamp = 0
        self.cache_duration = 3600  # Cache rankings for 1 hour
        self.db_path = db_path
        
        # Determine if we should use async (aiohttp is installed)
        if use_async is None:
            self.use_async = True
        else:
            self.use_async = use_async
            
        # Setup sync session for fallback
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        
        # Initialize ATP rankings scraper for automatic updates
        self.atp_scraper = ATPRankingsScraper(db_path=db_path)
        
    def get_current_monday(self) -> str:
        """Get the Monday of the current week in YYYY-MM-DD format."""
        today = datetime.now()
        days_since_monday = today.weekday()  # Monday is 0
        current_monday = today - timedelta(days=days_since_monday)
        return current_monday.strftime('%Y-%m-%d')
    
    def get_latest_db_date(self) -> Optional[str]:
        """Get the latest ranking date from the database."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT MAX(ranking_date) FROM rankings')
            result = cursor.fetchone()
            conn.close()
            return result[0] if result and result[0] else None
        except Exception as e:
            self.logger.error(f"Error getting latest DB date: {e}")
            return None
    
    def update_current_rankings(self):
        """Check and update rankings using incremental scraping."""
        latest_db_date = self.get_latest_db_date()
        
        if latest_db_date is None:
            self.logger.info("No rankings data found, skipping automatic update")
            return
        
        current_monday = self.get_current_monday()
        
        # Check if we need to update
        if latest_db_date >= current_monday:
            self.logger.info(f"Rankings up to date (latest: {latest_db_date}, current week: {current_monday})")
            return
        
        # Use incremental scraping to get current rankings
        self.logger.info("Updating rankings using incremental scraping")
        self.atp_scraper.scrape_incremental_data(delay_seconds=0.5)
    
    def create_h2h_url(self, player1_name: str, player2_name: str) -> str:
        """Create H2H URL from player names."""
        p1_formatted = player1_name.replace(' ', '-')
        p2_formatted = player2_name.replace(' ', '-')
        return f"https://tennistonic.com/head-to-head-compare/{p1_formatted}-Vs-{p2_formatted}/"
    
    def get_atp_rankings_sync(self, top_n: int = 5000) -> List[PlayerRanking]:
        """Get ATP rankings from database with automatic current week update."""
        current_time = time.time()
        
        # Check cache first
        if (self.atp_rankings_cache and 
            current_time - self.cache_timestamp < self.cache_duration):
            cached_rankings = list(self.atp_rankings_cache.values())[:top_n]
            self.logger.info(f"Using cached ATP rankings ({len(cached_rankings)} players)")
            return cached_rankings
        
        # Update current week rankings if needed
        self.update_current_rankings()
        
        # Get latest rankings from database
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Get the most recent ranking date
            cursor.execute('SELECT MAX(ranking_date) FROM rankings')
            latest_date = cursor.fetchone()[0]
            
            if not latest_date:
                self.logger.error("No rankings data found in database")
                conn.close()
                return []
            
            # Get rankings for the latest date
            cursor.execute('''
            SELECT rank, player_name, points, age, tournaments_played 
            FROM rankings 
            WHERE ranking_date = ? 
            ORDER BY rank 
            LIMIT ?
            ''', (latest_date, top_n))
            
            rankings = []
            for row in cursor.fetchall():
                rank, name, points, age, tournaments_played = row
                player_ranking = PlayerRanking(
                    rank=rank,
                    name=name,
                    points=points,
                    age=age or 0,
                    tournaments_played=tournaments_played or 0
                )
                rankings.append(player_ranking)
            
            conn.close()
            
            # Update cache
            self.atp_rankings_cache = {r.name.lower(): r for r in rankings}
            self.cache_timestamp = current_time
            
            self.logger.info(f"Loaded {len(rankings)} ATP rankings from database (date: {latest_date})")
            return rankings
                    
        except Exception as e:
            self.logger.error(f"Error loading ATP rankings from database: {e}")
            return []
    
    async def get_atp_rankings_async(self, top_n: int = 5000) -> List[PlayerRanking]:
        """Async version - just calls sync since database access is fast."""
        return self.get_atp_rankings_sync(top_n)
    
    def find_player_ranking(self, player_name: str, rankings: List[PlayerRanking]) -> Optional[PlayerRanking]:
        """Find player ranking with multiple matching strategies."""
        player_name_lower = player_name.lower()
        
        # Strategy 1: Exact match
        for ranking in rankings:
            if ranking.name.lower() == player_name_lower:
                return ranking
        
        # Strategy 2: Contains match
        for ranking in rankings:
            if player_name_lower in ranking.name.lower():
                return ranking
        
        # Strategy 3: Partial name match
        name_parts = player_name_lower.split()
        for ranking in rankings:
            ranking_parts = ranking.name.lower().split()
            if any(part in ranking_parts for part in name_parts if len(part) > 2):
                return ranking
        
        # Strategy 4: Last name match
        if len(name_parts) > 1:
            last_name = name_parts[-1]
            for ranking in rankings:
                if last_name in ranking.name.lower():
                    return ranking
        
        return None
    
    def scrape_h2h_comprehensive_sync(self, player1_name: str, player2_name: str) -> Optional[H2HComparison]:
        """Comprehensive scraping: H2H data + rankings (synchronous)."""
        try:
            # Get H2H data
            h2h_data = self._scrape_h2h_data_sync(player1_name, player2_name)
            if not h2h_data:
                return None
            
            # Get rankings
            rankings = self.get_atp_rankings_sync()
            
            # Add ranking data to players
            p1_ranking = self.find_player_ranking(player1_name, rankings)
            p2_ranking = self.find_player_ranking(player2_name, rankings)
            
            h2h_data.player1.current_ranking = p1_ranking
            h2h_data.player2.current_ranking = p2_ranking
            
            # Calculate ranking gaps
            if p1_ranking and p2_ranking:
                h2h_data.ranking_gap = abs(p1_ranking.rank - p2_ranking.rank)
                h2h_data.points_gap = abs(p1_ranking.points - p2_ranking.points)
            
            return h2h_data
            
        except Exception as e:
            self.logger.error(f"Error in comprehensive scraping: {e}")
            return None
    
    async def scrape_h2h_comprehensive_async(self, player1_name: str, player2_name: str) -> Optional[H2HComparison]:
        """Comprehensive scraping: H2H data + rankings in parallel (async)."""
        # Run H2H scraping and rankings fetch in parallel
        h2h_task = self._scrape_h2h_data_async(player1_name, player2_name)
        rankings_task = self.get_atp_rankings_async()
        
        try:
            h2h_data, rankings = await asyncio.gather(h2h_task, rankings_task)
            
            if not h2h_data:
                return None
            
            # Add ranking data to players
            p1_ranking = self.find_player_ranking(player1_name, rankings)
            p2_ranking = self.find_player_ranking(player2_name, rankings)
            
            h2h_data.player1.current_ranking = p1_ranking
            h2h_data.player2.current_ranking = p2_ranking
            
            # Calculate ranking gaps
            if p1_ranking and p2_ranking:
                h2h_data.ranking_gap = abs(p1_ranking.rank - p2_ranking.rank)
                h2h_data.points_gap = abs(p1_ranking.points - p2_ranking.points)
            
            return h2h_data
            
        except Exception as e:
            self.logger.error(f"Error in comprehensive scraping: {e}")
            return None
    
    def scrape_h2h_comprehensive(self, player1_name: str, player2_name: str) -> Optional[H2HComparison]:
        """Main interface - uses async if available, sync otherwise."""
        if self.use_async:
            return asyncio.run(self.scrape_h2h_comprehensive_async(player1_name, player2_name))
        else:
            return self.scrape_h2h_comprehensive_sync(player1_name, player2_name)
    
    def _scrape_h2h_data_sync(self, player1_name: str, player2_name: str) -> Optional[H2HComparison]:
        """Internal method to scrape H2H data (synchronous)."""
        url = self.create_h2h_url(player1_name, player2_name)
        
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract main H2H data
            h2h_record = self._extract_h2h_record(soup)
            player1_stats = self._extract_player_stats(soup, 1)
            player2_stats = self._extract_player_stats(soup, 2)
            matches_combined = self._extract_matches_combined(soup)
            match_history = self._extract_match_history(soup)
            h2h_history = self._extract_h2h_history(soup, player1_name, player2_name)
            
            return H2HComparison(
                player1=player1_stats,
                player2=player2_stats,
                head_to_head=h2h_record,
                matches_combined=matches_combined,
                match_history=match_history,
                h2h_history=h2h_history
            )
                    
        except Exception as e:
            self.logger.error(f"Error scraping H2H data from {url}: {e}")
            return None
    
    async def _scrape_h2h_data_async(self, player1_name: str, player2_name: str) -> Optional[H2HComparison]:
        """Internal method to scrape H2H data (async)."""
        url = self.create_h2h_url(player1_name, player2_name)
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, 'html.parser')
                    
                    # Extract main H2H data
                    h2h_record = self._extract_h2h_record(soup)
                    player1_stats = self._extract_player_stats(soup, 1)
                    player2_stats = self._extract_player_stats(soup, 2)
                    matches_combined = self._extract_matches_combined(soup)
                    match_history = self._extract_match_history(soup)
                    h2h_history = self._extract_h2h_history(soup, player1_name, player2_name)
                    
                    return H2HComparison(
                        player1=player1_stats,
                        player2=player2_stats,
                        head_to_head=h2h_record,
                        matches_combined=matches_combined,
                        match_history=match_history,
                        h2h_history=h2h_history
                    )
                    
        except Exception as e:
            self.logger.error(f"Error scraping H2H data from {url}: {e}")
            return None
    
    def _extract_h2h_record(self, soup: BeautifulSoup) -> str:
        """Extract head-to-head record (e.g., '4:9')."""
        score_div = soup.find('div', class_='score')
        if score_div:
            score_p = score_div.find('p')
            if score_p:
                return score_p.get_text(strip=True)
        return "0:0"
    
    def _extract_player_stats(self, soup: BeautifulSoup, player_num: int) -> PlayerStats:
        """Extract individual player statistics."""
        # Player name - extract from specific player sections
        name = "Unknown"
        
        # Try to get names from the score section headers
        score_section = soup.find('div', class_='score_stats')
        if score_section:
            if player_num == 1:
                name_elem = score_section.select_one('.player1_name_cl')
            else:
                name_elem = score_section.select_one('.player2_name_cl')
            
            if name_elem:
                name = name_elem.get_text(strip=True)
        
        # Fallback: try player name divs
        if name == "Unknown":
            player_name_divs = soup.find_all('div', class_='player_name_div')
            if len(player_name_divs) >= player_num:
                name = player_name_divs[player_num-1].get_text(strip=True)
        
        # Another fallback: try h3 links
        if name == "Unknown":
            h3_links = soup.select('h3 a')
            if len(h3_links) >= player_num:
                name = h3_links[player_num-1].get_text(strip=True).replace('\n', ' ')
        
        # Win-Loss record from stats section
        win_loss = "0-0"
        score_spans = soup.select('.ply_staus_div span')
        if len(score_spans) >= player_num:
            win_loss = score_spans[player_num-1].get_text(strip=True)
        
        # Recent form (W/L sequence)
        form_class = 'play_loss_win_tab' if player_num == 1 else 'play_loss_win_tab_right'
        form_section = soup.find('div', class_=form_class)
        recent_form = []
        if form_section:
            for span in form_section.find_all('span', class_=['tab_color_w', 'tab_color_l']):
                recent_form.append('W' if 'tab_color_w' in span.get('class', []) else 'L')
        
        # Yearly stats from tables
        yearly_stats = self._extract_yearly_stats(soup, player_num)
        
        # Recent tournament matches
        recent_tournaments = self._extract_recent_tournaments(soup, player_num)
        
        return PlayerStats(
            name=name,
            win_loss_record=win_loss,
            recent_form=recent_form,
            yearly_stats=yearly_stats,
            recent_tournaments=recent_tournaments
        )
    
    def _extract_yearly_stats(self, soup: BeautifulSoup, player_num: int) -> Dict[str, Dict[str, str]]:
        """Extract yearly statistics by surface."""
        yearly_stats = {}
        
        # Find all tables (should be one for each player)
        tables = soup.find_all('table')
        if len(tables) >= player_num:
            table = tables[player_num - 1]
            
            # Get headers for surfaces
            headers = []
            header_row = table.find('tr')
            if header_row:
                for th in header_row.find_all('th'):
                    headers.append(th.get_text(strip=True))
            
            # Get data rows
            for row in table.find_all('tr')[1:]:  # Skip header
                cells = row.find_all('td')
                if cells and len(cells) >= len(headers):
                    year = cells[0].get_text(strip=True)
                    yearly_stats[year] = {}
                    
                    for i, header in enumerate(headers[1:], 1):
                        if i < len(cells):
                            yearly_stats[year][header] = cells[i].get_text(strip=True)
        
        return yearly_stats
    
    def _extract_matches_combined(self, soup: BeautifulSoup) -> Dict[str, Tuple[str, str]]:
        """Extract comprehensive matches combined statistics."""
        combined_stats = {}
        
        stats_section = soup.find('div', class_='score_stats')
        if stats_section:
            for stat_row in stats_section.find_all('div', class_='score_stas_sec'):
                # Get stat name
                title_elem = stat_row.find('div', class_='title_sec')
                if not title_elem:
                    continue
                    
                stat_name = title_elem.get_text(strip=True)
                
                # Skip empty stat names
                if not stat_name:
                    continue
                
                # Get player values - try multiple selectors
                p1_value = ""
                p2_value = ""
                
                # Method 1: spans inside player sections
                p1_elem = stat_row.select_one('.player1_sec_inner span')
                p2_elem = stat_row.select_one('.player2_sec_inner span')
                
                if p1_elem and p2_elem:
                    p1_value = p1_elem.get_text(strip=True)
                    p2_value = p2_elem.get_text(strip=True)
                else:
                    # Method 2: Look for any spans in player sections
                    p1_section = stat_row.find('div', class_='player1_sec')
                    p2_section = stat_row.find('div', class_='player2_sec')
                    
                    if p1_section and p2_section:
                        # Get text from inner divs or spans
                        p1_inner = p1_section.find('div', class_='player1_sec_inner')
                        p2_inner = p2_section.find('div', class_='player2_sec_inner')
                        
                        if p1_inner and p2_inner:
                            # Look for spans or get direct text
                            p1_span = p1_inner.find('span')
                            p2_span = p2_inner.find('span')
                            
                            if p1_span:
                                p1_value = p1_span.get_text(strip=True)
                            elif p1_inner:
                                p1_value = p1_inner.get_text(strip=True)
                                
                            if p2_span:
                                p2_value = p2_span.get_text(strip=True)
                            elif p2_inner:
                                p2_value = p2_inner.get_text(strip=True)
                
                # Only add if we have both values
                if p1_value and p2_value and stat_name:
                    combined_stats[stat_name] = (p1_value, p2_value)
        
        return combined_stats
    
    def _extract_match_history(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """Extract recent match history."""
        matches = []
        
        # Find match sections with tournament info
        for tournament_section in soup.find_all('div', class_='player_last_results_single'):
            match_data = {}
            
            # Tournament name
            tournament_elem = tournament_section.find('div', class_='tournament_name')
            if tournament_elem:
                match_data['tournament'] = tournament_elem.get_text(strip=True)
            
            # Date
            date_elem = tournament_section.find('div', class_='match_date')
            if date_elem:
                match_data['date'] = date_elem.get_text(strip=True)
            
            # Score
            score_elem = tournament_section.find('font', class_='font_size')
            if score_elem:
                match_data['score'] = score_elem.get_text(strip=True)
            
            # Round
            round_elem = tournament_section.find('a', title=True)
            if round_elem and round_elem.get('title'):
                match_data['round'] = round_elem.get('title')
            
            if match_data:
                matches.append(match_data)
        
        return matches[:10]  # Return last 10 matches
    
    def _extract_h2h_history(self, soup: BeautifulSoup, player1_name: str, player2_name: str) -> List[H2HMatch]:
        """Extract actual head-to-head match history between the two players."""
        h2h_matches = []
        
        # Look for the H2H history section
        for ul in soup.find_all('ul', class_='background_color'):
            try:
                # Each UL contains one H2H match with fst_li and second_li
                fst_li = ul.find('li', class_='fst_li')
                second_li = ul.find('li', class_='second_li')
                
                if not fst_li or not second_li:
                    continue
                
                # Extract players and score from fst_li
                p1_name = ""
                p2_name = ""
                winner = ""
                loser = ""
                score = ""
                round_info = ""
                
                # Get player names
                player_name_li = fst_li.find('li', class_='player_name_li')
                nxt_player_name_li = fst_li.find('li', class_='nxt_player_name_li')
                
                if player_name_li and nxt_player_name_li:
                    p1_link = player_name_li.find('a')
                    p2_link = nxt_player_name_li.find('a')
                    
                    if p1_link and p2_link:
                        p1_name = p1_link.get_text(strip=True)
                        p2_name = p2_link.get_text(strip=True)
                        
                        # Check for winner indication (bold text usually indicates winner)
                        p1_bold = p1_link.find('b')
                        p2_bold = p2_link.find('b')
                        
                        # Don't rely on bold text - analyze score instead
                        # Bold might just be formatting and not indicate winner
                        winner = ""
                        loser = ""
                
                # Extract score
                score_li = fst_li.find('li', class_='player_score')
                if not score_li:
                    score_li = fst_li.find('li', class_='text_center')
                if score_li:
                    score_a = score_li.find('a')
                    if score_a:
                        # Process tennis score with superscripts
                        score = self._process_tennis_score(score_a)
                
                # Extract round
                position_li = fst_li.find('li', class_='player_position_li')
                if not position_li:
                    position_li = fst_li.find('li', class_='text_center1')
                if position_li:
                    round_info = position_li.get_text(strip=True)
                
                # Extract date, tournament, and surface from second_li
                date = ""
                tournament = ""
                surface = ""
                
                # Date
                date_li = second_li.find('li', class_='player_date_li')
                if date_li:
                    date_p = date_li.find('p')
                    if date_p:
                        date = date_p.get_text(strip=True)
                
                # Tournament
                score_bg_li = second_li.find('li', class_='score-background')
                if score_bg_li:
                    country_name_spans = score_bg_li.find_all('span', class_='country_name')
                    if len(country_name_spans) >= 2:
                        tournament = country_name_spans[0].get_text(strip=True) + country_name_spans[1].get_text(strip=True)
                    elif len(country_name_spans) == 1:
                        tournament = country_name_spans[0].get_text(strip=True)
                
                # Surface
                surface_li = second_li.find('li', class_='surface_li')
                if surface_li:
                    surface_p = surface_li.find('p')
                    if surface_p:
                        surface = surface_p.get_text(strip=True)
                
                # Always analyze the score to determine winner (score is from p1's perspective)
                if score and p1_name and p2_name:
                    winner, loser = self._determine_winner_from_score(score, p1_name, p2_name)
                
                # Create H2H match record
                if date and tournament and winner and score:
                    h2h_match = H2HMatch(
                        date=date,
                        tournament=tournament,
                        surface=surface,
                        winner=winner,
                        loser=loser,
                        score=score,
                        round=round_info
                    )
                    h2h_matches.append(h2h_match)
                            
            except Exception as e:
                continue  # Skip problematic matches
        
        return h2h_matches
    
    def _determine_winner_from_score(self, score: str, player1_name: str, player2_name: str) -> Tuple[str, str]:
        """Determine the winner and loser from a tennis score."""
        import re
        
        # Clean up the score and split into sets
        # Handle cases like "4-6 6(4)-7 6-4 7-6(3) 7-6(2)" or "7-6(5) 6-1"
        clean_score = score.replace('​', '').strip()
        
        # Split by spaces to get individual sets
        sets = clean_score.split()
        
        player1_sets = 0
        player2_sets = 0
        
        for set_score in sets:
            # Handle different formats: "6-4", "7-6(3)", "6(4)-7", etc.
            # Remove tiebreak info for set counting
            set_clean = re.sub(r'\([^)]*\)', '', set_score)
            
            # Split by dash
            if '-' in set_clean:
                parts = set_clean.split('-')
                if len(parts) == 2:
                    try:
                        p1_games = int(parts[0])
                        p2_games = int(parts[1])
                        
                        # Determine set winner
                        if p1_games > p2_games:
                            player1_sets += 1
                        elif p2_games > p1_games:
                            player2_sets += 1
                    except ValueError:
                        continue
        
        # Match winner is whoever won more sets
        if player1_sets > player2_sets:
            return player1_name, player2_name
        elif player2_sets > player1_sets:
            return player2_name, player1_name
        else:
            # If tied or unclear, default to first player
            return player1_name, player2_name
    
    def _extract_recent_tournaments(self, soup: BeautifulSoup, player_num: int) -> List[TournamentMatch]:
        """Extract recent tournament matches for a specific player from ALL tournaments."""
        tournaments = []
        
        # Find player-specific sections - they are in left/right containers
        if player_num == 1:
            target_section = soup.find('div', class_='play_prev_tour_inner_left')
        else:
            target_section = soup.find('div', class_='play_prev_tour_inner_right')
        
        if not target_section:
            return tournaments
        
        # Look for all tournament sections within this player's section
        tournament_sections = target_section.find_all('div', class_='country_1')
        
        current_tournament_name = "Unknown Tournament"
        
        for tournament_section in tournament_sections:
            try:
                # Check if this section has a tournament header (with city name)
                page_heading = tournament_section.find('div', class_='page-heading')
                if page_heading:
                    h2_elem = page_heading.find('h2')
                    if h2_elem:
                        font_elem = h2_elem.find('font')
                        if font_elem:
                            current_tournament_name = font_elem.get_text(strip=True)
                
                # Also check for tournament links to extract tournament names
                tournament_link = tournament_section.find('a', href=True)
                if tournament_link and '/tournament/' in tournament_link.get('href', ''):
                    href = tournament_link.get('href')
                    if 'Wimbledon---London' in href:
                        current_tournament_name = "London"
                    elif 'Halle' in href:
                        current_tournament_name = "Halle"  
                    elif 'Paris' in href or 'French-Open' in href:
                        current_tournament_name = "Paris"
                    elif 'Roland-Garros' in href:
                        current_tournament_name = "Paris"
                
                # Extract match details from scoreRowss section
                score_rows = tournament_section.find('div', class_='scoreRowss')
                if score_rows:
                    # Find all UL elements (each contains one match)
                    ul_elements = score_rows.find_all('ul')
                    
                    for ul_elem in ul_elements:
                        # Each UL contains one match row with 4 LI elements (opponent, score, round, date)
                        li_elements = ul_elem.find_all('li')
                        
                        if len(li_elements) >= 4:
                            # Opponent name and result
                            opponent = ""
                            result = "Win"  # Default to Win unless Loss marker found
                            first_li = li_elements[0]
                            
                            # Get opponent name
                            name_div = first_li.find('div', class_='name_desktop_view')
                            if name_div:
                                opponent = name_div.get_text(strip=True)
                            
                            # Get result - check for Loss marker (red color)
                            result_font = first_li.find('font', color="#cc0000")
                            if result_font and result_font.get_text(strip=True).lower() == "loss":
                                result = "Loss"
                            else:
                                # If no loss marker, it's a win
                                result = "Win"
                            
                            # Score - handle tiebreaker superscripts properly
                            score = ""
                            if len(li_elements) > 1:
                                score_font = li_elements[1].find('font', class_='font_size')
                                if score_font:
                                    # Process the score to handle <sup> tags properly
                                    score = self._process_tennis_score(score_font)
                            
                            # Round
                            round_info = ""
                            if len(li_elements) > 2:
                                round_link = li_elements[2].find('a')
                                if round_link:
                                    round_info = round_link.get_text(strip=True)
                            
                            # Date - extract from <p> tag in 4th li element
                            date = ""
                            if len(li_elements) > 3:
                                date_p = li_elements[3].find('p')
                                if date_p:
                                    date = date_p.get_text(strip=True)
                            
                            # Use current tournament name (either from this section or carried from previous)
                            if opponent:
                                tournament_match = TournamentMatch(
                                    tournament_name=current_tournament_name,
                                    opponent=opponent,
                                    score=score,
                                    round=round_info,
                                    date=date,
                                    result=result
                                )
                                tournaments.append(tournament_match)
                                
            except Exception as e:
                continue  # Skip problematic tournament sections
        
        return tournaments  # Return all tournament matches
    
    def _process_tennis_score(self, score_element) -> str:
        """Process tennis score element to handle tiebreaker superscripts properly."""
        import re
        
        # Get the HTML content and process it
        html_content = str(score_element)
        
        # In tennis tiebreakers, the superscript shows points scored by the losing player
        # Examples:
        # "7-6<sup>3</sup>" -> "7-6(3)" (winner won 7-6, loser got 3 points in tiebreak)  
        # "6-7<sup>8</sup>" -> "6(8)-7" (loser got 6 games and 8 points in tiebreak)
        
        # Handle the two cases:
        # 1. X-Y<sup>Z</sup> where X > Y: becomes X-Y(Z) 
        # 2. X-Y<sup>Z</sup> where X < Y: becomes X(Z)-Y
        
        def fix_tiebreaker(match):
            score1 = int(match.group(1))
            score2 = int(match.group(2))
            tiebreak = match.group(3)
            
            if score1 > score2:
                # Winner-loser, tiebreak goes with loser (score2)
                return f"{score1}-{score2}({tiebreak})"
            else:
                # Loser-winner, tiebreak goes with loser (score1)
                return f"{score1}({tiebreak})-{score2}"
        
        # Pattern: digit-digit<sup>digit</sup>
        html_content = re.sub(r'(\d)-(\d)<sup>(\d+)</sup>', fix_tiebreaker, html_content)
        
        # Parse the modified HTML
        from bs4 import BeautifulSoup
        temp_soup = BeautifulSoup(html_content, 'html.parser')
        processed_score = temp_soup.get_text(strip=True)
        
        # Add proper spacing between sets if missing
        processed_score = re.sub(r'(\d(?:\(\d+\))?-\d(?:\(\d+\))?)([\d])', r'\1 \2', processed_score)
        
        return processed_score
    
    def print_comprehensive_comparison(self, comparison: H2HComparison):
        """Print enhanced comparison with rankings."""
        if not comparison:
            print("No comparison data available")
            return
            
        print(f"\n{'='*100}")
        print(f"🎾 COMPREHENSIVE TENNIS ANALYSIS: {comparison.player1.name} vs {comparison.player2.name}")
        print(f"{'='*100}")
        
        # Current rankings
        print(f"\n🏆 CURRENT ATP RANKINGS:")
        if comparison.player1.current_ranking:
            print(f"  {comparison.player1.name}: #{comparison.player1.current_ranking.rank} ({comparison.player1.current_ranking.points:,} points)")
        else:
            print(f"  {comparison.player1.name}: Not in top 5000")
            
        if comparison.player2.current_ranking:
            print(f"  {comparison.player2.name}: #{comparison.player2.current_ranking.rank} ({comparison.player2.current_ranking.points:,} points)")
        else:
            print(f"  {comparison.player2.name}: Not in top 5000")
        
        # Ranking gaps
        if comparison.ranking_gap is not None and comparison.points_gap is not None:
            higher_ranked = comparison.player1.name if (comparison.player1.current_ranking and 
                          comparison.player2.current_ranking and 
                          comparison.player1.current_ranking.rank < comparison.player2.current_ranking.rank) else comparison.player2.name
            print(f"  📊 Ranking Gap: {comparison.ranking_gap} positions | {comparison.points_gap:,} points")
            print(f"  🔝 Higher Ranked: {higher_ranked}")
        
        # H2H and season records
        print(f"\n📈 HEAD-TO-HEAD RECORD: {comparison.head_to_head}")
        print(f"📊 2025 Season Records: {comparison.player1.win_loss_record} vs {comparison.player2.win_loss_record}")
        
        # Recent form
        print(f"\n🎯 RECENT FORM:")
        print(f"  {comparison.player1.name}: {' '.join(comparison.player1.recent_form[-10:])}")
        print(f"  {comparison.player2.name}: {' '.join(comparison.player2.recent_form[-10:])}")
        
        # Current tournament statistics (what was previously called "H2H")
        if comparison.matches_combined:
            print(f"\n🎾 CURRENT TOURNAMENT STATISTICS:")
            key_stats = ['Time on court​', 'Last match​', 'Match played', 'Games Played​', 'Set played​​', 'Total points​', 'Avg point​', 'Winners', 'Avg Winners​', 'Ratio winners', 'Aces', 'Avg Aces', 'Double Faults', 'Avg Double Faults']
            for stat in key_stats:
                if stat in comparison.matches_combined:
                    p1_val, p2_val = comparison.matches_combined[stat]
                    print(f"  {stat}: {p1_val} vs {p2_val}")
            
            # Show serve statistics
            serve_stats = ['1st won total​', '1st won %', '2nd won total​', '2nd won %']
            if any(stat in comparison.matches_combined for stat in serve_stats):
                print(f"  📊 Serve Statistics:")
                for stat in serve_stats:
                    if stat in comparison.matches_combined:
                        p1_val, p2_val = comparison.matches_combined[stat]
                        print(f"    {stat}: {p1_val} vs {p2_val}")
            
            # Show break point statistics  
            bp_stats = ['BP won', 'Avg BP won', 'Won', 'Conceded', 'Avg Conceded', 'Broken', 'Avg Broken', 'Saved']
            if any(stat in comparison.matches_combined for stat in bp_stats):
                print(f"  🎯 Break Point Statistics:")
                for stat in bp_stats:
                    if stat in comparison.matches_combined:
                        p1_val, p2_val = comparison.matches_combined[stat]
                        print(f"    {stat}: {p1_val} vs {p2_val}")
        
        # Yearly stats tables
        if comparison.player1.yearly_stats:
            print(f"\n📊 {comparison.player1.name} Yearly Performance:")
            for year, stats in list(comparison.player1.yearly_stats.items())[:3]:  # Show last 3 years
                surfaces = [f"{surface}: {record}" for surface, record in stats.items() if surface != 'Sum.']
                print(f"  {year} | Overall: {stats.get('Sum.', 'N/A')} | {' | '.join(surfaces[:3])}")
        
        if comparison.player2.yearly_stats:
            print(f"\n📊 {comparison.player2.name} Yearly Performance:")
            for year, stats in list(comparison.player2.yearly_stats.items())[:3]:  # Show last 3 years
                surfaces = [f"{surface}: {record}" for surface, record in stats.items() if surface != 'Sum.']
                print(f"  {year} | Overall: {stats.get('Sum.', 'N/A')} | {' | '.join(surfaces[:3])}")
        
        # Recent tournaments
        if comparison.player1.recent_tournaments:
            print(f"\n🏟️ {comparison.player1.name} Recent Tournaments:")
            for t in comparison.player1.recent_tournaments[:10]:
                date_str = f" ({t.date})" if t.date else ""
                round_str = f" {t.round}" if t.round else ""
                print(f"  {t.tournament_name} vs {t.opponent} | {t.score} | {t.result}{round_str}{date_str}")
        
        if comparison.player2.recent_tournaments:
            print(f"\n🏟️ {comparison.player2.name} Recent Tournaments:")
            for t in comparison.player2.recent_tournaments[:10]:
                date_str = f" ({t.date})" if t.date else ""
                round_str = f" {t.round}" if t.round else ""
                print(f"  {t.tournament_name} vs {t.opponent} | {t.score} | {t.result}{round_str}{date_str}")
        
        # Actual Head-to-Head Match History
        if comparison.h2h_history:
            print(f"\n🏆 HEAD-TO-HEAD MATCH HISTORY:")
            print(f"{'Date':<12} {'Tournament':<25} {'Winner':<15} {'Score':<30} {'Surface':<8} {'Round':<8}")
            print(f"{'-'*105}")
            for match in comparison.h2h_history[:10]:  # Show last 10 H2H matches
                winner_short = match.winner[:14] if len(match.winner) > 14 else match.winner
                tournament_short = match.tournament[:24] if len(match.tournament) > 24 else match.tournament
                # Don't truncate the score - let it display fully
                score_display = match.score if match.score else ""
                print(f"{match.date:<12} {tournament_short:<25} {winner_short:<15} {score_display:<30} {match.surface:<8} {match.round:<8}")
        
        # All tournament combined stats (detailed breakdown)
        if comparison.matches_combined and len(comparison.matches_combined) > 14:
            print(f"\n📈 DETAILED H2H STATISTICS:")
            print(f"{'Statistic':<20} {'Player 1':<15} {'Player 2':<15}")
            print(f"{'-'*50}")
            for stat, (p1_val, p2_val) in list(comparison.matches_combined.items())[7:]:  # Show remaining stats
                print(f"{stat[:19]:<20} {p1_val:<15} {p2_val:<15}")

def main():
    """Test the comprehensive tennis scraper."""
    scraper = TennisScraper(use_async=False)  # Use sync for now since aiohttp isn't installed
    
    test_matchups = [
        ("Jannik Sinner", "Carlos Alcaraz"),
        ("Alexander Zverev", "Novak Djokovic")
    ]
    
    for player1, player2 in test_matchups:
        print(f"\n🔄 Scraping comprehensive data for {player1} vs {player2}...")
        comparison = scraper.scrape_h2h_comprehensive(player1, player2)
        
        if comparison:
            scraper.print_comprehensive_comparison(comparison)
        else:
            print(f"❌ Failed to scrape data for {player1} vs {player2}")
        
        print("\n" + "="*100)

if __name__ == "__main__":
    main()
