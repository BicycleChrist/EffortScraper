import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime
import os
import csv  # Added missing import for csv.QUOTE_ALL

def scrape_page(url):
    # Use a more modern browser User-Agent
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/123.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
    }

    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, 'lxml')  # lxml is faster than html.parser

    # Find all listing rows
    rows = soup.find_all('tr', class_='resp-table-body__row')

    data = []
    for row in rows:
        listing = {}

        # Extract all labeled data at once
        for item in row.find_all(class_=['resp-table-body__item--main', 'resp-table-body__item--inline']):
            label = item.find(class_='resp-table-body__label')
            if label:
                key = label.text.strip().rstrip(':')
                # Get text after the label
                value = item.get_text(strip=True).replace(label.get_text(strip=True), '').strip()
                listing[key] = value

        # Extract notes/updated info
        notes_div = row.find('div', id=lambda x: x and x.startswith('listing-notes-'))
        if notes_div:
            notes_label = notes_div.find(class_='resp-table-body__label')
            if notes_label:
                notes_text = notes_div.get_text(strip=True).replace(notes_label.get_text(strip=True), '').strip()
                listing['Updated'] = notes_text

        if listing:
            data.append(listing)

    return data

def save_data(data, name):
    if not data:
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    df = pd.DataFrame(data)

    output_dir = 'PermitData'
    os.makedirs(output_dir, exist_ok=True)

    # Fix for CSV: properly escape special characters and handle encoding
    csv_path = os.path.join(output_dir, f"{name}_{timestamp}.csv")
    df.to_csv(csv_path,
              index=False,
              encoding='utf-8-sig',  # Use UTF-8 with BOM for Excel compatibility
              quoting=csv.QUOTE_ALL,  # Quote all fields
              quotechar='"',         # Use double quotes
              escapechar='\\')       # Use backslash as escape character

    # Excel saving remains the same
    excel_path = os.path.join(output_dir, f"{name}_{timestamp}.xlsx")
    df.to_excel(excel_path, index=False)

    print(f"Saved {len(data)} records for {name}")
    print(f"Files saved as {csv_path} and {excel_path}")

def main():
    urls = {
        'alaska_permits': "https://dockstreetbrokers.com/permits/alaska-permits",
        'Halibut_ifq': "https://dockstreetbrokers.com/longline-ifqs/halibut-ifqs",
        'Sablefish_ifq': "https://dockstreetbrokers.com/longline-ifqs/sablefish-ifqs"
    }

    for name, url in urls.items():
        print(f"\nScraping {name}...")
        try:
            data = scrape_page(url)
            if data:
                save_data(data, name)
            else:
                print(f"No data found for {name}")
        except Exception as e:
            print(f"Error scraping {name}: {e}")

if __name__ == "__main__":
    main()
