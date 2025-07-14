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
    winners_errors: List[WinnersErrorsData]
    serve_speed: List[ServeSpeedData]
    scrape_timestamp: str
    source_url: str

class TennisAbstractScraper:
    def __init__(self, headless: bool = False, timeout: int = 10):
        self.headless = headless
        self.timeout = timeout
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        
    def _create_driver(self) -> webdriver.Firefox:
        """Create a new Firefox driver instance"""
        options = FirefoxOptions()
        if self.headless:
            options.add_argument('--headless')
        driver = webdriver.Firefox(options=options)
        driver.implicitly_wait(self.timeout)
        return driver
    
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
    
    def _scrape_recent_results(self, driver: webdriver.Firefox) -> List[MatchResult]:
        """Scrape recent results table"""
        results = []
        try:
            table = driver.find_element(By.ID, "recent-results")
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 16:  # Ensure we have all expected columns
                    # Extract opponent name from the complex cell (index 6)
                    opponent_cell = cells[6]
                    opponent_text = opponent_cell.text
                    
                    result = MatchResult(
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
            print(f"Error scraping recent results: {e}")
            
        return results
    
    def _scrape_season_stats(self, driver: webdriver.Firefox, table_id: str) -> List[SeasonStats]:
        """Scrape season statistics table (tour or challenger)"""
        stats = []
        try:
            table = driver.find_element(By.ID, table_id)
            tbody = table.find_element(By.TAG_NAME, "tbody")
            rows = tbody.find_elements(By.TAG_NAME, "tr")
            
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 23:  # Ensure we have all expected columns
                    season = SeasonStats(
                        year=cells[0].text.strip(),
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
                        dominance_ratio=cells[22].text.strip(),
                        best_result=cells[23].text.strip() if len(cells) > 23 else ""
                    )
                    stats.append(season)
                    
        except Exception as e:
            print(f"Error scraping {table_id}: {e}")
            
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
    
    def _scrape_player_page(self, url: str) -> Optional[PlayerData]:
        """Scrape a single player page"""
        driver = self._create_driver()
        try:
            print(f"Scraping: {url}")
            driver.get(url)
            
            # Wait for main content to load
            WebDriverWait(driver, self.timeout).until(
                EC.presence_of_element_located((By.ID, "main"))
            )
            
            # Extract player data
            player_name = self._extract_player_name(driver)
            player_bio = self._extract_player_bio(driver)
            recent_results = self._scrape_recent_results(driver)
            tour_seasons = self._scrape_season_stats(driver, "tour-years")
            challenger_seasons = self._scrape_season_stats(driver, "chall-years")
            recent_finals = self._scrape_finals_results(driver)
            year_end_rankings = self._scrape_year_end_rankings(driver)
            recent_events = self._scrape_recent_events(driver)
            career_splits = self._scrape_split_stats(driver, "career-splits")
            last52_splits = self._scrape_split_stats(driver, "last52-splits")
            winners_errors = self._scrape_winners_errors(driver)
            serve_speed = self._scrape_serve_speed(driver)
            
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
                winners_errors=winners_errors,
                serve_speed=serve_speed,
                scrape_timestamp=datetime.now().isoformat(),
                source_url=url
            )
            
            print(f"Successfully scraped {player_name}: {len(recent_results)} recent results, "
                  f"{len(tour_seasons)} tour seasons, {len(challenger_seasons)} challenger seasons, "
                  f"{len(recent_finals)} finals, {len(year_end_rankings)} year rankings, "
                  f"{len(recent_events)} events, {len(career_splits)} career splits, "
                  f"{len(last52_splits)} recent splits, {len(winners_errors)} winner/error matches, "
                  f"{len(serve_speed)} serve speed matches")
            
            return player_data
            
        except Exception as e:
            print(f"Error scraping {url}: {e}")
            return None
        finally:
            driver.quit()
    
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
    
    def close(self):
        """Clean up thread pool"""
        self.executor.shutdown(wait=True)

# Example usage and testing
def main():
    # Example player URLs - these would typically be discovered through search
    test_urls = [
        "https://www.tennisabstract.com/cgi-bin/player.cgi?p=BrandonHolt",
        "https://www.tennisabstract.com/cgi-bin/player.cgi?p=JannikSinner",
        "https://www.tennisabstract.com/cgi-bin/player.cgi?p=RafaelNadal"
    ]
    
    scraper = TennisAbstractScraper(headless=True)
    
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
