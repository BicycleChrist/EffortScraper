from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import *
import asyncio
import concurrent.futures
import threading
from time import sleep
import json
import pathlib
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

@dataclass
class MatchResult:
    date: str
    tournament: str
    surface: str
    round: str
    player_rank: str
    opponent_rank: str
    opponent: str
    score: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    break_points_saved: str
    match_time: str

@dataclass
class SeasonStats:
    year: str
    matches: str
    wins: str
    losses: str
    win_percentage: str
    set_record: str
    set_percentage: str
    game_record: str
    game_percentage: str
    tiebreak_record: str
    tiebreak_percentage: str
    matches_with_stats: str
    hold_percentage: str
    break_percentage: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    service_points_won: str
    return_points_won: str
    total_points_won: str
    dominance_ratio: str
    best_result: str

@dataclass
class FinalsResult:
    date: str
    tournament: str
    surface: str
    round: str
    player_rank: str
    opponent_rank: str
    opponent: str
    score: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    break_points_saved: str
    match_time: str

@dataclass
class YearEndRanking:
    year: str
    atp_rank: str
    points: str
    elo_rank: str
    elo_rating: str
    hard_elo_rank: str
    hard_elo: str
    clay_elo_rank: str
    clay_elo: str
    grass_elo_rank: str
    grass_elo: str

@dataclass
class EventResult:
    event: str
    years_entered: str
    surface: str
    matches: str
    wins: str
    losses: str
    win_percentage: str
    tiebreaks: str
    tb_wins: str
    tb_losses: str
    tb_percentage: str
    first_year: str
    last_year: str
    best_result: str
    matches_with_stats: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    service_points_won: str
    return_points_won: str
    break_points_saved_pct: str
    break_points_converted_pct: str

@dataclass
class SplitStats:
    split: str
    matches: str
    wins: str
    losses: str
    win_percentage: str
    set_record: str
    set_percentage: str
    game_record: str
    game_percentage: str
    tiebreak_record: str
    tiebreak_percentage: str
    matches_with_stats: str
    hold_percentage: str
    break_percentage: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    service_points_won: str
    return_points_won: str
    total_points_won: str
    dominance_ratio: str

@dataclass
class WinnersErrorsData:
    match: str
    result: str
    winners: str
    unforced_errors: str
    ratio: str
    winners_per_point: str
    ufe_per_point: str
    rally_winners: str
    rally_ufes: str
    rally_ratio: str
    rally_winners_per_point: str
    rally_ufe_per_point: str
    fh_winners_per_point: str
    bh_winners_per_point: str
    opponent_ratio: str

@dataclass
class ServeSpeedData:
    match: str
    first_serve_avg: str
    first_serve_max: str
    second_serve_avg: str
    second_serve_max: str

@dataclass
class TacticsData:
    match: str
    result: str
    snv_freq: str
    snv_w_pct: str
    net_freq: str
    net_w_pct: str
    fh_wnr_pct: str
    dtl_wnr_pct: str
    io_wnr_pct: str
    bh_wnr_pct: str
    dtl_wnr_pct_bh: str
    drop_freq: str
    drop_wnr_pct: str
    rally_agg: str
    return_agg: str

@dataclass
class HistoricalMatchData:
    date: str
    tournament: str
    surface: str
    round: str
    player_rank: str
    opponent_rank: str
    opponent: str
    result: str  # Win/Loss extracted from match description
    score: str
    charting_link: str
    dominance_ratio: str
    ace_rate: str
    double_fault_rate: str
    first_serve_in: str
    first_serve_won: str
    second_serve_won: str
    break_points_saved: str
    match_time: str

@dataclass
class PlayerBio:
    name: str
    country: str
    age: str
    birth_date: str
    plays: str
    current_rank: str
    peak_rank: str
    peak_rank_date: str
    elo_rank: str
    elo_rating: str
    photo_url: str

@dataclass
class PlayerData:
    player_name: str
    player_bio: PlayerBio
    recent_results: List[MatchResult]
    tour_seasons: List[SeasonStats]
    challenger_seasons: List[SeasonStats]
    recent_finals: List[FinalsResult]
    year_end_rankings: List[YearEndRanking]
    recent_events: List[EventResult]
    career_splits: List[SplitStats]
    last52_splits: List[SplitStats]
    career_splits_chall: List[SplitStats]
    last52_splits_chall: List[SplitStats]
    winners_errors: List[WinnersErrorsData]
    serve_speed: List[ServeSpeedData]
    tactics: List[TacticsData]
    historical_matches: List[HistoricalMatchData]
    scrape_timestamp: str
    source_url: str

class TennisAbstractScraper:
    def __init__(self, headless: bool = False, timeout: int = 10, max_workers: int = 4, reuse_driver: bool = True):
        self.headless = headless
        self.timeout = timeout
        self.reuse_driver = reuse_driver
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._driver_pool = []
        self._element_cache = {}
        
    def _create_driver(self) -> webdriver.Firefox:
        """Create a new Firefox driver instance"""
        options = FirefoxOptions()
        if self.headless:
            options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.set_preference('dom.webnotifications.enabled', False)
        options.set_preference('media.volume_scale', '0.0')
        driver = webdriver.Firefox(options=options)
        # No implicit wait - using explicit WebDriverWait instead
        return driver
    
    def _get_driver(self) -> webdriver.Firefox:
        """Get a driver from pool or create new one"""
        if self.reuse_driver and self._driver_pool:
            return self._driver_pool.pop()
        return self._create_driver()
    
    def _return_driver(self, driver: webdriver.Firefox):
        """Return driver to pool for reuse"""
        if self.reuse_driver and len(self._driver_pool) < 2:
            self._driver_pool.append(driver)
        else:
            driver.quit()
    
    def _get_cached_data(self, driver: webdriver.Firefox, url: str) -> Dict:
        """Get all page HTML at once and parse offline like historical matches"""
        if url in self._element_cache:
            return self._element_cache[url]
        
        # Get entire page HTML in one call (like historical matches approach)
        page_html = driver.page_source
        
        # Parse with BeautifulSoup offline
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page_html, 'html.parser')
        
        data = {}
        
        # Extract bio from HTML
        bio_element = soup.find(id="bio")
        if bio_element:
            data["bio"] = self._extract_player_bio_from_soup(bio_element)
        else:
            data["bio"] = PlayerBio("", "", "", "", "", "", "", "", "", "", "")
        
        # Extract all table data from parsed HTML (no more DOM queries)
        data["recent-results"] = self._scrape_table_from_soup(soup.find(id="recent-results"), "recent-results")
        data["tour-years"] = self._scrape_table_from_soup(soup.find(id="tour-years"), "tour-years")
        data["chall-years"] = self._scrape_table_from_soup(soup.find(id="chall-years"), "chall-years")
        data["recent-finals"] = self._scrape_table_from_soup(soup.find(id="recent-finals"), "recent-finals")
        data["year-end-rankings"] = self._scrape_table_from_soup(soup.find(id="year-end-rankings"), "year-end-rankings")
        data["recent-events"] = self._scrape_table_from_soup(soup.find(id="recent-events"), "recent-events")
        data["career-splits"] = self._scrape_table_from_soup(soup.find(id="career-splits"), "career-splits")
        data["last52-splits"] = self._scrape_table_from_soup(soup.find(id="last52-splits"), "last52-splits")
        data["career-splits-chall"] = self._scrape_table_from_soup(soup.find(id="career-splits-chall"), "career-splits-chall")
        data["last52-splits-chall"] = self._scrape_table_from_soup(soup.find(id="last52-splits-chall"), "last52-splits-chall")
        data["winners-errors"] = self._scrape_table_from_soup(soup.find(id="winners-errors"), "winners-errors")
        data["serve-speed"] = self._scrape_table_from_soup(soup.find(id="serve-speed"), "serve-speed")
        data["mcp-tactics"] = self._scrape_table_from_soup(soup.find(id="mcp-tactics"), "mcp-tactics")
            
        self._element_cache[url] = data
        return data

    def _extract_player_bio_from_soup(self, bio_element):
        """Extract player bio from BeautifulSoup element"""
        if not bio_element:
            return PlayerBio("", "", "", "", "", "", "", "", "", "", "")
        
        try:
            bio_table = bio_element.find('table')
            if not bio_table:
                return PlayerBio("", "", "", "", "", "", "", "", "", "", "")
            
            # Get photo URL
            photo_url = ""
            img_element = bio_table.find('img')
            if img_element:
                photo_url = img_element.get('src', '')
            
            # Get bio text
            bio_rows = bio_table.find_all('tr')
            bio_text = [row.get_text(strip=True) for row in bio_rows if row.get_text(strip=True)]
            
            # Parse bio info (same logic as before)
            name = country = age = birth_date = plays = current_rank = peak_rank = peak_rank_date = elo_rank = elo_rating = ""
            
            for line in bio_text:
                if "[" in line and "]" in line:
                    name = line.split("[")[0].strip()
                    country = line.split("[")[1].split("]")[0].strip()
                elif line.startswith("Age:"):
                    age_parts = line.replace("Age:", "").strip()
                    if "(" in age_parts:
                        age = age_parts.split("(")[0].strip()
                        birth_date = age_parts.split("(")[1].replace(")", "").strip()
                elif line.startswith("Plays:"):
                    plays = line.replace("Plays:", "").strip()
                elif line.startswith("Current rank:"):
                    current_rank = line.replace("Current rank:", "").strip()
                elif line.startswith("Peak rank:"):
                    peak_parts = line.replace("Peak rank:", "").strip()
                    if "(" in peak_parts:
                        peak_rank = peak_parts.split("(")[0].strip()
                        peak_rank_date = peak_parts.split("(")[1].replace(")", "").strip()
                elif line.startswith("Elo rank:"):
                    elo_parts = line.replace("Elo rank:", "").strip()
                    if "(" in elo_parts:
                        elo_rank = elo_parts.split("(")[0].strip()
                        rating_part = elo_parts.split("rating:")[1].replace(")", "").strip() if "rating:" in elo_parts else ""
                        elo_rating = rating_part
            
            return PlayerBio(name, country, age, birth_date, plays, current_rank, peak_rank, peak_rank_date, elo_rank, elo_rating, photo_url)
            
        except Exception as e:
            return PlayerBio("", "", "", "", "", "", "", "", "", "", "")

    def _scrape_table_from_soup(self, table_element, table_type):
        """Generic table scraper that works with BeautifulSoup elements"""
        if not table_element:
            return []
        
        try:
            tbody = table_element.find('tbody')
            if not tbody:
                return []
            
            rows = tbody.find_all('tr')
            if not rows:
                return []
            
            # Convert BeautifulSoup rows to text data and use existing scrapers
            row_data = []
            for row in rows:
                cells = row.find_all(['td', 'th'])
                cell_texts = [cell.get_text(strip=True) for cell in cells]
                row_data.append(cell_texts)
            
            # Use existing scraping logic based on table type
            if table_type in ["recent-results"]:
                return self._process_match_results(row_data)
            elif table_type in ["tour-years", "chall-years"]:
                return self._process_season_stats(row_data)
            elif table_type in ["recent-finals"]:
                return self._process_finals_results(row_data)
            elif table_type in ["year-end-rankings"]:
                return self._process_year_end_rankings(row_data)
            elif table_type in ["recent-events"]:
                return self._process_recent_events(row_data)
            elif table_type in ["career-splits", "last52-splits", "career-splits-chall", "last52-splits-chall"]:
                return self._process_split_stats(row_data)
            elif table_type in ["winners-errors"]:
                return self._process_winners_errors(row_data)
            elif table_type in ["serve-speed"]:
                return self._process_serve_speed(row_data)
            elif table_type in ["mcp-tactics"]:
                return self._process_tactics(row_data)
            else:
                return []
                
        except Exception as e:
            return []
    
    def _process_match_results(self, row_data):
        """Process match results from text data"""
        results = []
        for row_texts in row_data:
            if len(row_texts) >= 16:
                try:
                    result = MatchResult(*row_texts[:16])
                    results.append(result)
                except:
                    continue
        return results
    
    def _process_season_stats(self, row_data):
        """Process season stats from text data"""
        stats = []
        for row_texts in row_data:
            if len(row_texts) >= 23:
                try:
                    season = SeasonStats(
                        year=row_texts[0],
                        matches=row_texts[1],
                        wins=row_texts[2],
                        losses=row_texts[3],
                        win_percentage=row_texts[4],
                        set_record=row_texts[5],
                        set_percentage=row_texts[6],
                        game_record=row_texts[7],
                        game_percentage=row_texts[8],
                        tiebreak_record=row_texts[9],
                        tiebreak_percentage=row_texts[10],
                        matches_with_stats=row_texts[11],
                        hold_percentage=row_texts[12],
                        break_percentage=row_texts[13],
                        ace_rate=row_texts[14],
                        double_fault_rate=row_texts[15],
                        first_serve_in=row_texts[16],
                        first_serve_won=row_texts[17],
                        second_serve_won=row_texts[18],
                        service_points_won=row_texts[19],
                        return_points_won=row_texts[20],
                        total_points_won=row_texts[21],
                        dominance_ratio=row_texts[22],
                        best_result=row_texts[23] if len(row_texts) > 23 else ""
                    )
                    stats.append(season)
                except:
                    continue
        return stats
    
    def _process_finals_results(self, row_data):
        """Process finals results from text data"""
        results = []
        for row_texts in row_data:
            if len(row_texts) >= 16:
                try:
                    result = FinalsResult(*row_texts[:16])
                    results.append(result)
                except:
                    continue
        return results
    
    def _process_year_end_rankings(self, row_data):
        """Process year end rankings from text data"""
        rankings = []
        for row_texts in row_data:
            if len(row_texts) >= 11:
                try:
                    ranking = YearEndRanking(*row_texts[:11])
                    rankings.append(ranking)
                except:
                    continue
        return rankings
    
    def _process_recent_events(self, row_data):
        """Process recent events from text data"""
        events = []
        for row_texts in row_data:
            if len(row_texts) >= 25:
                try:
                    event = EventResult(*row_texts[:25])
                    events.append(event)
                except:
                    continue
        return events
    
    def _process_split_stats(self, row_data):
        """Process split stats from text data"""
        splits = []
        for row_texts in row_data:
            if len(row_texts) >= 22:
                try:
                    split = SplitStats(*row_texts[:22], row_texts[22] if len(row_texts) > 22 else "")
                    splits.append(split)
                except:
                    continue
        return splits
    
    def _process_winners_errors(self, row_data):
        """Process winners/errors from text data"""
        data = []
        for row_texts in row_data:
            if len(row_texts) >= 15:
                try:
                    winners_errors = WinnersErrorsData(*row_texts[:15])
                    data.append(winners_errors)
                except:
                    continue
        return data
    
    def _process_serve_speed(self, row_data):
        """Process serve speed from text data"""
        data = []
        for row_texts in row_data:
            if len(row_texts) >= 5:
                try:
                    serve_speed = ServeSpeedData(*row_texts[:5])
                    data.append(serve_speed)
                except:
                    continue
        return data
    
    def _process_tactics(self, row_data):
        """Process tactics from text data"""
        data = []
        for row_texts in row_data:
            if len(row_texts) >= 15:
                try:
                    tactics = TacticsData(*row_texts[:15])
                    data.append(tactics)
                except:
                    continue
        return data
    
    
    def _get_table_rows_safe(self, driver: webdriver.Firefox, table_id: str, quick_check: bool = False) -> List:
        """Safely get table rows with minimal waiting"""
        try:
            # Always use find_elements (no wait) instead of find_element
            tables = driver.find_elements(By.ID, table_id)
            if not tables:
                return []
                
            table = tables[0]
            tbody_elements = table.find_elements(By.TAG_NAME, "tbody")
            if not tbody_elements:
                return []
                
            return tbody_elements[0].find_elements(By.TAG_NAME, "tr")
        except:
            return []
    
    def _extract_player_name(self, driver: webdriver.Firefox) -> str:
        """Extract player name from page title or header"""
        try:
            title = driver.title
            # Title format is usually "Player Name - Tennis Abstract"
            if " - Tennis Abstract" in title:
                return title.replace(" - Tennis Abstract", "").strip()
            return "Unknown Player"
        except Exception as e:
            print(f"Error extracting player name: {e}")
            return "Unknown Player"
    
    def _extract_player_bio(self, driver: webdriver.Firefox) -> PlayerBio:
        """Extract player bio information from the bio section"""
        try:
            bio_span = driver.find_element(By.ID, "bio")
            bio_table = bio_span.find_element(By.TAG_NAME, "table")
            
            # Extract photo URL
            photo_url = ""
            try:
                img_element = bio_table.find_element(By.TAG_NAME, "img")
                photo_url = img_element.get_attribute("src")
            except:
                pass
            
            # Extract text data from the bio table
            bio_rows = bio_table.find_elements(By.TAG_NAME, "tr")
            bio_text = []
            for row in bio_rows:
                text = row.text.strip()
                if text:
                    bio_text.append(text)
            
            # Parse the bio information
            name = ""
            country = ""
            age = ""
            birth_date = ""
            plays = ""
            current_rank = ""
            peak_rank = ""
            peak_rank_date = ""
            elo_rank = ""
            elo_rating = ""
            
            for line in bio_text:
                if "[" in line and "]" in line:
                    # Extract name and country
                    name = line.split("[")[0].strip()
                    country = line.split("[")[1].split("]")[0].strip()
                elif line.startswith("Age:"):
                    age_parts = line.replace("Age:", "").strip()
                    if "(" in age_parts:
                        age = age_parts.split("(")[0].strip()
                        birth_date = age_parts.split("(")[1].replace(")", "").strip()
                elif line.startswith("Plays:"):
                    plays = line.replace("Plays:", "").strip()
                elif line.startswith("Current rank:"):
                    current_rank = line.replace("Current rank:", "").strip()
                elif line.startswith("Peak rank:"):
                    peak_parts = line.replace("Peak rank:", "").strip()
                    if "(" in peak_parts:
                        peak_rank = peak_parts.split("(")[0].strip()
                        peak_rank_date = peak_parts.split("(")[1].replace(")", "").strip()
                elif line.startswith("Elo rank:"):
                    elo_parts = line.replace("Elo rank:", "").strip()
                    if "(" in elo_parts:
                        elo_rank = elo_parts.split("(")[0].strip()
                        # Extract rating from "rating: 1663)"
                        rating_part = elo_parts.split("rating:")[1].replace(")", "").strip() if "rating:" in elo_parts else ""
                        elo_rating = rating_part
            
            return PlayerBio(
                name=name,
                country=country,
                age=age,
                birth_date=birth_date,
                plays=plays,
                current_rank=current_rank,
                peak_rank=peak_rank,
                peak_rank_date=peak_rank_date,
                elo_rank=elo_rank,
                elo_rating=elo_rating,
                photo_url=photo_url
            )
            
        except Exception as e:
            print(f"Error extracting player bio: {e}")
            return PlayerBio("", "", "", "", "", "", "", "", "", "", "")
    
    def _scrape_recent_results(self, rows: List) -> List[MatchResult]:
        """Scrape recent results table from cached rows"""
        results = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 16:
                    cell_texts = [cell.text.strip() for cell in cells[:16]]
                    
                    result = MatchResult(
                        date=cell_texts[0],
                        tournament=cell_texts[1],
                        surface=cell_texts[2],
                        round=cell_texts[3],
                        player_rank=cell_texts[4],
                        opponent_rank=cell_texts[5],
                        opponent=cell_texts[6],
                        score=cell_texts[7],
                        dominance_ratio=cell_texts[8],
                        ace_rate=cell_texts[9],
                        double_fault_rate=cell_texts[10],
                        first_serve_in=cell_texts[11],
                        first_serve_won=cell_texts[12],
                        second_serve_won=cell_texts[13],
                        break_points_saved=cell_texts[14],
                        match_time=cell_texts[15]
                    )
                    results.append(result)
                    
        except Exception as e:
            print(f"Error scraping recent results: {e}")
            
        return results
    
    def _scrape_season_stats(self, rows: List) -> List[SeasonStats]:
        """Scrape season statistics table from cached rows"""
        stats = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 23:
                    cell_texts = [cell.text.strip() for cell in cells]
                    
                    season = SeasonStats(
                        year=cell_texts[0],
                        matches=cell_texts[1],
                        wins=cell_texts[2],
                        losses=cell_texts[3],
                        win_percentage=cell_texts[4],
                        set_record=cell_texts[5],
                        set_percentage=cell_texts[6],
                        game_record=cell_texts[7],
                        game_percentage=cell_texts[8],
                        tiebreak_record=cell_texts[9],
                        tiebreak_percentage=cell_texts[10],
                        matches_with_stats=cell_texts[11],
                        hold_percentage=cell_texts[12],
                        break_percentage=cell_texts[13],
                        ace_rate=cell_texts[14],
                        double_fault_rate=cell_texts[15],
                        first_serve_in=cell_texts[16],
                        first_serve_won=cell_texts[17],
                        second_serve_won=cell_texts[18],
                        service_points_won=cell_texts[19],
                        return_points_won=cell_texts[20],
                        total_points_won=cell_texts[21],
                        dominance_ratio=cell_texts[22],
                        best_result=cell_texts[23] if len(cell_texts) > 23 else ""
                    )
                    stats.append(season)
                    
        except Exception as e:
            print(f"Error scraping season stats: {e}")
            
        return stats
    
    def _scrape_finals_results(self, driver: webdriver.Firefox) -> List[FinalsResult]:
        """Scrape recent finals table"""
        results = []
        try:
            table = driver.find_element(By.ID, "recent-finals")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 16:
                    opponent_cell = cells[6]
                    opponent_text = opponent_cell.text
                    
                    result = FinalsResult(
                        date=cells[0].text.strip(),
                        tournament=cells[1].text.strip(),
                        surface=cells[2].text.strip(),
                        round=cells[3].text.strip(),
                        player_rank=cells[4].text.strip(),
                        opponent_rank=cells[5].text.strip(),
                        opponent=opponent_text.strip(),
                        score=cells[7].text.strip(),
                        dominance_ratio=cells[8].text.strip(),
                        ace_rate=cells[9].text.strip(),
                        double_fault_rate=cells[10].text.strip(),
                        first_serve_in=cells[11].text.strip(),
                        first_serve_won=cells[12].text.strip(),
                        second_serve_won=cells[13].text.strip(),
                        break_points_saved=cells[14].text.strip(),
                        match_time=cells[15].text.strip()
                    )
                    results.append(result)
                    
        except Exception as e:
            print(f"Error scraping recent finals: {e}")
            
        return results
    
    def _scrape_year_end_rankings(self, driver: webdriver.Firefox) -> List[YearEndRanking]:
        """Scrape year-end rankings table"""
        rankings = []
        try:
            table = driver.find_element(By.ID, "year-end-rankings")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 11:
                    ranking = YearEndRanking(
                        year=cells[0].text.strip(),
                        atp_rank=cells[1].text.strip(),
                        points=cells[2].text.strip(),
                        elo_rank=cells[3].text.strip(),
                        elo_rating=cells[4].text.strip(),
                        hard_elo_rank=cells[5].text.strip(),
                        hard_elo=cells[6].text.strip(),
                        clay_elo_rank=cells[7].text.strip(),
                        clay_elo=cells[8].text.strip(),
                        grass_elo_rank=cells[9].text.strip(),
                        grass_elo=cells[10].text.strip()
                    )
                    rankings.append(ranking)
                    
        except Exception as e:
            print(f"Error scraping year-end rankings: {e}")
            
        return rankings
    
    def _scrape_recent_events(self, driver: webdriver.Firefox) -> List[EventResult]:
        """Scrape recent events table"""
        events = []
        try:
            table = driver.find_element(By.ID, "recent-events")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 25:
                    event = EventResult(
                        event=cells[0].text.strip(),
                        years_entered=cells[1].text.strip(),
                        surface=cells[2].text.strip(),
                        matches=cells[3].text.strip(),
                        wins=cells[4].text.strip(),
                        losses=cells[5].text.strip(),
                        win_percentage=cells[6].text.strip(),
                        tiebreaks=cells[7].text.strip(),
                        tb_wins=cells[8].text.strip(),
                        tb_losses=cells[9].text.strip(),
                        tb_percentage=cells[10].text.strip(),
                        first_year=cells[11].text.strip(),
                        last_year=cells[12].text.strip(),
                        best_result=cells[13].text.strip(),
                        matches_with_stats=cells[14].text.strip(),
                        dominance_ratio=cells[15].text.strip(),
                        ace_rate=cells[16].text.strip(),
                        double_fault_rate=cells[17].text.strip(),
                        first_serve_in=cells[18].text.strip(),
                        first_serve_won=cells[19].text.strip(),
                        second_serve_won=cells[20].text.strip(),
                        service_points_won=cells[21].text.strip(),
                        return_points_won=cells[22].text.strip(),
                        break_points_saved_pct=cells[23].text.strip(),
                        break_points_converted_pct=cells[24].text.strip()
                    )
                    events.append(event)
                    
        except Exception as e:
            print(f"Error scraping recent events: {e}")
            
        return events
    
    def _scrape_split_stats(self, driver: webdriver.Firefox, table_id: str) -> List[SplitStats]:
        """Scrape split statistics table (career or last52)"""
        splits = []
        try:
            table = driver.find_element(By.ID, table_id)
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 22:
                    split = SplitStats(
                        split=cells[0].text.strip(),
                        matches=cells[1].text.strip(),
                        wins=cells[2].text.strip(),
                        losses=cells[3].text.strip(),
                        win_percentage=cells[4].text.strip(),
                        set_record=cells[5].text.strip(),
                        set_percentage=cells[6].text.strip(),
                        game_record=cells[7].text.strip(),
                        game_percentage=cells[8].text.strip(),
                        tiebreak_record=cells[9].text.strip(),
                        tiebreak_percentage=cells[10].text.strip(),
                        matches_with_stats=cells[11].text.strip(),
                        hold_percentage=cells[12].text.strip(),
                        break_percentage=cells[13].text.strip(),
                        ace_rate=cells[14].text.strip(),
                        double_fault_rate=cells[15].text.strip(),
                        first_serve_in=cells[16].text.strip(),
                        first_serve_won=cells[17].text.strip(),
                        second_serve_won=cells[18].text.strip(),
                        service_points_won=cells[19].text.strip(),
                        return_points_won=cells[20].text.strip(),
                        total_points_won=cells[21].text.strip(),
                        dominance_ratio=cells[22].text.strip() if len(cells) > 22 else ""
                    )
                    splits.append(split)
                    
        except Exception as e:
            print(f"Error scraping {table_id}: {e}")
            
        return splits
    
    def _scrape_winners_errors(self, driver: webdriver.Firefox) -> List[WinnersErrorsData]:
        """Scrape winners and errors table"""
        data = []
        try:
            table = driver.find_element(By.ID, "winners-errors")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 15:
                    winners_errors = WinnersErrorsData(
                        match=cells[0].text.strip(),
                        result=cells[1].text.strip(),
                        winners=cells[2].text.strip(),
                        unforced_errors=cells[3].text.strip(),
                        ratio=cells[4].text.strip(),
                        winners_per_point=cells[5].text.strip(),
                        ufe_per_point=cells[6].text.strip(),
                        rally_winners=cells[7].text.strip(),
                        rally_ufes=cells[8].text.strip(),
                        rally_ratio=cells[9].text.strip(),
                        rally_winners_per_point=cells[10].text.strip(),
                        rally_ufe_per_point=cells[11].text.strip(),
                        fh_winners_per_point=cells[12].text.strip(),
                        bh_winners_per_point=cells[13].text.strip(),
                        opponent_ratio=cells[14].text.strip()
                    )
                    data.append(winners_errors)
                    
        except Exception as e:
            print(f"Error scraping winners and errors: {e}")
            
        return data
    
    def _scrape_serve_speed(self, driver: webdriver.Firefox) -> List[ServeSpeedData]:
        """Scrape serve speed table"""
        data = []
        try:
            table = driver.find_element(By.ID, "serve-speed")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 5:
                    serve_speed = ServeSpeedData(
                        match=cells[0].text.strip(),
                        first_serve_avg=cells[1].text.strip(),
                        first_serve_max=cells[2].text.strip(),
                        second_serve_avg=cells[3].text.strip(),
                        second_serve_max=cells[4].text.strip()
                    )
                    data.append(serve_speed)
                    
        except Exception as e:
            print(f"Error scraping serve speed: {e}")
            
        return data
    
    def _scrape_tactics(self, driver: webdriver.Firefox) -> List[TacticsData]:
        """Scrape tactics table (mcp-tactics)"""
        data = []
        try:
            table = driver.find_element(By.ID, "mcp-tactics")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 15:
                    tactics = TacticsData(
                        match=cells[0].text.strip(),
                        result=cells[1].text.strip(),
                        snv_freq=cells[2].text.strip(),
                        snv_w_pct=cells[3].text.strip(),
                        net_freq=cells[4].text.strip(),
                        net_w_pct=cells[5].text.strip(),
                        fh_wnr_pct=cells[6].text.strip(),
                        dtl_wnr_pct=cells[7].text.strip(),
                        io_wnr_pct=cells[8].text.strip(),
                        bh_wnr_pct=cells[9].text.strip(),
                        dtl_wnr_pct_bh=cells[10].text.strip(),
                        drop_freq=cells[11].text.strip(),
                        drop_wnr_pct=cells[12].text.strip(),
                        rally_agg=cells[13].text.strip(),
                        return_agg=cells[14].text.strip()
                    )
                    data.append(tactics)
                    
        except Exception as e:
            print(f"Error scraping tactics: {e}")
            
        return data
    
    def _scrape_player_page(self, url: str) -> Optional[PlayerData]:
        """Scrape a single player page"""
        driver = self._get_driver()
        try:
            print(f"Scraping: {url}")
            driver.get(url)
            
            # Wait for bio section to load (indicates page is ready)
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "bio"))
            )
            
            # Extract player name first
            player_name = self._extract_player_name(driver)
            
            # Extract ALL main page data immediately (no element caching)
            main_page_data = self._get_cached_data(driver, url)
            
            # Now scrape historical data in separate tab
            historical_matches = self._scrape_historical_matches(driver, url)
            
            # Use the already-extracted data (no more DOM access needed)
            player_bio = main_page_data["bio"]
            recent_results = main_page_data["recent-results"]
            tour_seasons = main_page_data["tour-years"]
            challenger_seasons = main_page_data["chall-years"]
            recent_finals = main_page_data["recent-finals"]
            year_end_rankings = main_page_data["year-end-rankings"]
            recent_events = main_page_data["recent-events"]
            career_splits = main_page_data["career-splits"]
            last52_splits = main_page_data["last52-splits"]
            career_splits_chall = main_page_data["career-splits-chall"]
            last52_splits_chall = main_page_data["last52-splits-chall"]
            winners_errors = main_page_data["winners-errors"]
            serve_speed = main_page_data["serve-speed"]
            tactics = main_page_data["mcp-tactics"]
            
            player_data = PlayerData(
                player_name=player_name,
                player_bio=player_bio,
                recent_results=recent_results,
                tour_seasons=tour_seasons,
                challenger_seasons=challenger_seasons,
                recent_finals=recent_finals,
                year_end_rankings=year_end_rankings,
                recent_events=recent_events,
                career_splits=career_splits,
                last52_splits=last52_splits,
                career_splits_chall=career_splits_chall,
                last52_splits_chall=last52_splits_chall,
                winners_errors=winners_errors,
                serve_speed=serve_speed,
                tactics=tactics,
                historical_matches=historical_matches,
                scrape_timestamp=datetime.now().isoformat(),
                source_url=url
            )
            
            print(f"Successfully scraped {player_name}: {len(recent_results)} recent results, "
                  f"{len(tour_seasons)} tour seasons, {len(challenger_seasons)} challenger seasons, "
                  f"{len(recent_finals)} finals, {len(year_end_rankings)} year rankings, "
                  f"{len(recent_events)} events, {len(career_splits)} career splits, "
                  f"{len(last52_splits)} recent splits, {len(winners_errors)} winner/error matches, "
                  f"{len(serve_speed)} serve speed matches, {len(tactics)} tactics matches, "
                  f"{len(historical_matches)} historical matches")
            
            return player_data
            
        except Exception as e:
            print(f"Error scraping {url}: {e}")
            return None
        finally:
            self._return_driver(driver)
    
    async def scrape_players_async(self, urls: List[str]) -> Dict[str, PlayerData]:
        """Asynchronously scrape multiple player pages"""
        loop = asyncio.get_event_loop()
        
        # Submit all scraping tasks to thread pool
        futures = [
            loop.run_in_executor(self.executor, self._scrape_player_page, url)
            for url in urls
        ]
        
        # Wait for all tasks to complete
        results = await asyncio.gather(*futures, return_exceptions=True)
        
        # Process results
        scraped_data = {}
        for url, result in zip(urls, results):
            if isinstance(result, PlayerData):
                scraped_data[url] = result
            elif isinstance(result, Exception):
                print(f"Exception for {url}: {result}")
            else:
                print(f"No data returned for {url}")
        
        return scraped_data
    
    def scrape_players(self, urls: List[str]) -> Dict[str, PlayerData]:
        """Synchronous wrapper for scraping multiple players"""
        return asyncio.run(self.scrape_players_async(urls))
    
    def save_data(self, data: Dict[str, PlayerData], filename: str = None):
        """Save scraped data to JSON file"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"tennis_abstract_data_{timestamp}.json"
        
        # Convert dataclasses to dictionaries for JSON serialization
        json_data = {}
        for url, player_data in data.items():
            json_data[url] = asdict(player_data)
        
        save_path = pathlib.Path(__file__).parent / filename
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        
        print(f"Data saved to {save_path}")
    
    def _extract_player_bio_cached(self, bio_element) -> PlayerBio:
        """Extract player bio from cached element"""
        try:
            bio_table = bio_element.find_element(By.TAG_NAME, "table")
            
            # Extract photo URL
            photo_url = ""
            try:
                img_element = bio_table.find_element(By.TAG_NAME, "img")
                photo_url = img_element.get_attribute("src")
            except:
                pass
            
            # Extract text data
            bio_rows = bio_table.find_elements(By.TAG_NAME, "tr")
            bio_text = [row.text.strip() for row in bio_rows if row.text.strip()]
            
            # Parse bio information
            name = country = age = birth_date = plays = current_rank = peak_rank = peak_rank_date = elo_rank = elo_rating = ""
            
            for line in bio_text:
                if "[" in line and "]" in line:
                    name = line.split("[")[0].strip()
                    country = line.split("[")[1].split("]")[0].strip()
                elif line.startswith("Age:"):
                    age_parts = line.replace("Age:", "").strip()
                    if "(" in age_parts:
                        age = age_parts.split("(")[0].strip()
                        birth_date = age_parts.split("(")[1].replace(")", "").strip()
                elif line.startswith("Plays:"):
                    plays = line.replace("Plays:", "").strip()
                elif line.startswith("Current rank:"):
                    current_rank = line.replace("Current rank:", "").strip()
                elif line.startswith("Peak rank:"):
                    peak_parts = line.replace("Peak rank:", "").strip()
                    if "(" in peak_parts:
                        peak_rank = peak_parts.split("(")[0].strip()
                        peak_rank_date = peak_parts.split("(")[1].replace(")", "").strip()
                elif line.startswith("Elo rank:"):
                    elo_parts = line.replace("Elo rank:", "").strip()
                    if "(" in elo_parts:
                        elo_rank = elo_parts.split("(")[0].strip()
                        rating_part = elo_parts.split("rating:")[1].replace(")", "").strip() if "rating:" in elo_parts else ""
                        elo_rating = rating_part
            
            return PlayerBio(name, country, age, birth_date, plays, current_rank, peak_rank, peak_rank_date, elo_rank, elo_rating, photo_url)
            
        except Exception as e:
            print(f"Error extracting player bio: {e}")
            return PlayerBio("", "", "", "", "", "", "", "", "", "", "")
    
    def _scrape_finals_results_cached(self, rows: List) -> List[FinalsResult]:
        """Scrape finals results from cached rows"""
        results = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 16:
                    cell_texts = [cell.text.strip() for cell in cells[:16]]
                    result = FinalsResult(*cell_texts)
                    results.append(result)
        except Exception as e:
            print(f"Error scraping finals results: {e}")
        return results
    
    def _scrape_year_end_rankings_cached(self, rows: List) -> List[YearEndRanking]:
        """Scrape year-end rankings from cached rows"""
        rankings = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 11:
                    cell_texts = [cell.text.strip() for cell in cells[:11]]
                    ranking = YearEndRanking(*cell_texts)
                    rankings.append(ranking)
        except Exception as e:
            print(f"Error scraping year-end rankings: {e}")
        return rankings
    
    def _scrape_recent_events_cached(self, rows: List) -> List[EventResult]:
        """Scrape recent events from cached rows"""
        events = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 25:
                    cell_texts = [cell.text.strip() for cell in cells[:25]]
                    event = EventResult(*cell_texts)
                    events.append(event)
        except Exception as e:
            print(f"Error scraping recent events: {e}")
        return events
    
    def _scrape_split_stats_cached(self, rows: List) -> List[SplitStats]:
        """Scrape split stats from cached rows"""
        splits = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 22:
                    cell_texts = [cell.text.strip() for cell in cells]
                    dominance_ratio = cell_texts[22] if len(cell_texts) > 22 else ""
                    split = SplitStats(*cell_texts[:22], dominance_ratio)
                    splits.append(split)
        except Exception as e:
            print(f"Error scraping split stats: {e}")
        return splits
    
    def _scrape_winners_errors_cached(self, rows: List) -> List[WinnersErrorsData]:
        """Scrape winners/errors from cached rows"""
        data = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 15:
                    cell_texts = [cell.text.strip() for cell in cells[:15]]
                    winners_errors = WinnersErrorsData(*cell_texts)
                    data.append(winners_errors)
        except Exception as e:
            print(f"Error scraping winners/errors: {e}")
        return data
    
    def _scrape_serve_speed_cached(self, rows: List) -> List[ServeSpeedData]:
        """Scrape serve speed from cached rows"""
        data = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 5:
                    cell_texts = [cell.text.strip() for cell in cells[:5]]
                    serve_speed = ServeSpeedData(*cell_texts)
                    data.append(serve_speed)
        except Exception as e:
            print(f"Error scraping serve speed: {e}")
        return data
    
    def _scrape_tactics_cached(self, rows: List) -> List[TacticsData]:
        """Scrape tactics from cached rows"""
        data = []
        try:
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 15:
                    cell_texts = [cell.text.strip() for cell in cells[:15]]
                    tactics = TacticsData(*cell_texts)
                    data.append(tactics)
        except Exception as e:
            print(f"Error scraping tactics: {e}")
        return data

    def _extract_player_name_from_url(self, url: str) -> str:
        """Extract player name from URL for historical data URL construction"""
        if "?p=" in url:
            return url.split("?p=")[1].split("&")[0]
        return ""

    def _scrape_historical_matches(self, driver: webdriver.Firefox, base_url: str) -> List[HistoricalMatchData]:
        """Scrape historical match data from player-classic page in separate tab"""
        historical_matches = []
        
        try:
            player_name = self._extract_player_name_from_url(base_url)
            if not player_name:
                print("Could not extract player name from URL")
                return historical_matches
            
            historical_url = f"https://www.tennisabstract.com/cgi-bin/player-classic.cgi?p={player_name}&f=ACareerqq"
            
            # Check driver connection before proceeding
            try:
                driver.current_url  # Test connection
            except Exception as e:
                print(f"Driver connection lost before historical scraping: {e}")
                return historical_matches
            
            # Open historical page in new tab
            driver.execute_script("window.open('');")
            driver.switch_to.window(driver.window_handles[1])
            driver.get(historical_url)
            
            # Wait for the matches table to load
            try:
                matches_table = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.ID, "matches"))
                )
                table_html = matches_table.get_attribute('outerHTML')
                
                # Parse the HTML with BeautifulSoup
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(table_html, 'html.parser')
                
                # Find all rows with tdate class
                rows = soup.find_all('tr')
                match_rows = [row for row in rows if row.find('td', class_='tdate')]
                
                print(f"Found {len(match_rows)} historical match rows")
                
                # Extract all data from the parsed HTML (no more Selenium DOM access)
                all_row_data = []
                for row in match_rows:
                    cells = row.find_all('td')
                    if len(cells) >= 17:
                        # Get basic cell text
                        row_texts = [cell.get_text(strip=True) for cell in cells[:17]]
                        
                        # Extract opponent and result from cell 6 structure
                        opponent = ""
                        result = ""
                        match_cell = cells[6]
                        
                        # Find opponent name from link
                        opponent_link = match_cell.find('a')
                        if opponent_link:
                            opponent = opponent_link.get_text(strip=True)
                        
                        # Determine result by checking span order
                        spans = match_cell.find_all('span')
                        sinner_pos = -1
                        d_pos = -1
                        
                        for i, span in enumerate(spans):
                            if span.get('style') == 'font-weight: bold;' and 'Sinner' in span.get_text():
                                sinner_pos = i
                            elif 'd.' in span.get_text():
                                d_pos = i
                        
                        if sinner_pos != -1 and d_pos != -1:
                            if sinner_pos < d_pos:
                                result = "Win"
                            else:
                                result = "Loss"
                        
                        # Get charting link if present
                        charting_link = ""
                        if len(cells) > 8:
                            link = cells[8].find('a')
                            if link:
                                charting_link = link.get('href', '')
                        
                        all_row_data.append((row_texts, opponent, result, charting_link))
                
                # Close tab immediately after data extraction
                driver.close()
                driver.switch_to.window(driver.window_handles[0])
                
                # Process the extracted data (no more DOM access)
                for row_texts, opponent, result, charting_link in all_row_data:
                    try:
                        # Parse match info from text data
                        date = row_texts[0]
                        tournament = row_texts[1]
                        surface = row_texts[2]
                        round_info = row_texts[3]
                        player_rank = row_texts[4]
                        opponent_rank = row_texts[5]
                        score = row_texts[7]
                        
                        if date and opponent:
                            historical_match = HistoricalMatchData(
                                date=date,
                                tournament=tournament,
                                surface=surface,
                                round=round_info,
                                player_rank=player_rank,
                                opponent_rank=opponent_rank,
                                opponent=opponent,
                                result=result,
                                score=score,
                                charting_link=charting_link,
                                dominance_ratio=row_texts[9] if len(row_texts) > 9 else "",
                                ace_rate=row_texts[10] if len(row_texts) > 10 else "",
                                double_fault_rate=row_texts[11] if len(row_texts) > 11 else "",
                                first_serve_in=row_texts[12] if len(row_texts) > 12 else "",
                                first_serve_won=row_texts[13] if len(row_texts) > 13 else "",
                                second_serve_won=row_texts[14] if len(row_texts) > 14 else "",
                                break_points_saved=row_texts[15] if len(row_texts) > 15 else "",
                                match_time=row_texts[16] if len(row_texts) > 16 else ""
                            )
                            historical_matches.append(historical_match)
                            
                    except Exception as e:
                        print(f"Error processing historical match data: {e}")
                        continue
                        
            except Exception as e:
                print(f"Error accessing historical page elements: {e}")
                # Ensure tab cleanup
                try:
                    if len(driver.window_handles) > 1:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                except:
                    pass
            
            print(f"Scraped {len(historical_matches)} historical matches")
            
        except Exception as e:
            print(f"Error scraping historical matches: {e}")
            # Ensure proper cleanup
            try:
                if len(driver.window_handles) > 1:
                    driver.close()
                    driver.switch_to.window(driver.window_handles[0])
            except:
                pass
        
        return historical_matches

    def close(self):
        """Clean up resources"""
        for driver in self._driver_pool:
            driver.quit()
        self._driver_pool.clear()
        self._element_cache.clear()
        self.executor.shutdown(wait=True)

# Example usage and testing
def main():
    # Example player URLs - these would typically be discovered through search
    test_urls = [
        "https://www.tennisabstract.com/cgi-bin/player.cgi?p=JannikSinner"
    ]
    
    scraper = TennisAbstractScraper(headless=False)
    
    try:
        # Scrape players asynchronously
        data = scraper.scrape_players(test_urls)
        
        
        # Save results
        scraper.save_data(data)
        
        # Print summary
        print(f"\nScraping complete! Scraped {len(data)} players:")
        for url, player_data in data.items():
            print(f"- {player_data.player_name}: {len(player_data.recent_results)} recent matches, "
                  f"{len(player_data.tour_seasons) + len(player_data.challenger_seasons)} total seasons, "
                  f"{len(player_data.recent_events)} events tracked")
            
    finally:
        scraper.close()

if __name__ == "__main__":
    main()
