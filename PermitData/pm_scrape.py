import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime
import os
import time
from typing import Dict, List
from pathlib import Path

class PermitScraper:
    def __init__(self):
        self.urls = {
            'halibut_ifq': "https://www.permitmaster.com/ifqs/?type=halibut&sort=area",
            'sablefish_ifq': "https://www.permitmaster.com/ifqs/?type=sablefish&sort=area",
            'permits': "https://www.permitmaster.com/permits/?location=alaska"
        }
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        # Get the directory where the script is located
        self.script_dir = Path(__file__).parent.absolute()
        self.archive_dir = self.script_dir / "Archive"

        # Create archive directory if it doesn't exist
        self.archive_dir.mkdir(exist_ok=True)

    def get_soup(self, url: str) -> BeautifulSoup:
        """Fetch and parse page content"""
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as e:
            print(f"Error fetching {url}: {e}")
            return None

    def parse_halibut_ifq(self, row) -> Dict:
        """Parse halibut IFQ table row"""
        cols = row.find_all('td')
        return {
            'ID': cols[1].text.strip(),
            'Fishery': cols[2].text.strip(),
            'Location': cols[3].text.strip(),
            'Type': cols[4].text.strip(),
            'Ask': cols[5].text.strip(),
            'Offer': cols[6].text.strip(),
            'Updated': cols[7].text.strip(),
            'Notes': cols[8].text.strip(),
            'Data_Type': 'Halibut_IFQ'
        }

    def parse_sablefish_ifq(self, row) -> Dict:
        """Parse sablefish IFQ table row"""
        cols = row.find_all('td')
        return {
            'ID': cols[1].text.strip(),
            'Type': cols[2].text.strip(),
            'Area': cols[3].text.strip(),
            'Class': cols[4].text.strip(),
            'BU': cols[5].text.strip(),
            'Pounds': cols[6].text.strip(),
            'Fished': cols[7].text.strip(),
            'Ask': cols[8].text.strip(),
            'Offer': cols[9].text.strip(),
            'Updated': cols[10].text.strip(),
            'Notes': cols[11].text.strip(),
            'Data_Type': 'Sablefish_IFQ'
        }

    def parse_permit(self, row) -> Dict:
        """Parse permit table row"""
        cols = row.find_all('td')
        return {
            'ID': cols[1].text.strip(),
            'Fishery': cols[2].text.strip(),
            'Location': cols[3].text.strip(),
            'Type': cols[4].text.strip(),
            'Ask': cols[5].text.strip(),
            'Offer': cols[6].text.strip(),
            'Updated': cols[7].text.strip(),
            'Notes': cols[8].text.strip(),
            'Data_Type': 'Permit'
        }

    def scrape_data(self, url: str, data_type: str) -> pd.DataFrame:
        """Scrape data based on type"""
        soup = self.get_soup(url)
        if not soup:
            return pd.DataFrame()

        rows = soup.find_all('tr', {'class': 'desktop-display'})[1:]  # Skip header
        parser = {
            'halibut_ifq': self.parse_halibut_ifq,
            'sablefish_ifq': self.parse_sablefish_ifq,
            'permits': self.parse_permit
        }[data_type]

        data = [parser(row) for row in rows]
        return pd.DataFrame(data)

    def save_data(self, df: pd.DataFrame, data_type: str):
        """Save data to CSV and Excel"""
        if df.empty:
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_path = self.archive_dir / f"permit_master_{data_type}_{timestamp}"

        df.to_csv(f"{base_path}.csv", index=False)
        print(f"Saved CSV: {base_path}.csv")

        df.to_excel(f"{base_path}.xlsx", index=False)
        print(f"Saved Excel: {base_path}.xlsx")

    def run(self):
        """Run the scraper for all data types"""
        print(f"Starting data collection... Archives will be saved to: {self.archive_dir}")

        total_entries = 0
        for data_type, url in self.urls.items():
            print(f"\nScraping {data_type}...")
            df = self.scrape_data(url, data_type)
            if not df.empty:
                self.save_data(df, data_type)
                total_entries += len(df)
                print(f"Collected {len(df)} entries for {data_type}")
            time.sleep(0.5)  # Maybe faster?

        print(f"\nScraping completed! Total entries collected: {total_entries}")

if __name__ == "__main__":
    scraper = PermitScraper()
    scraper.run()
