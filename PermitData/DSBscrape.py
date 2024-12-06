import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime
import os
import csv
import time
import json

def scrape_permits_page(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/123.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
    }

    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, 'lxml')

    rows = soup.find_all('tr', class_='resp-table-body__row')

    data = []
    for row in rows:
        listing = {}

        for item in row.find_all(class_=['resp-table-body__item--main', 'resp-table-body__item--inline']):
            label = item.find(class_='resp-table-body__label')
            if label:
                key = label.text.strip().rstrip(':')
                value = item.get_text(strip=True).replace(label.get_text(strip=True), '').strip()
                listing[key] = value

        notes_div = row.find('div', id=lambda x: x and x.startswith('listing-notes-'))
        if notes_div:
            notes_label = notes_div.find(class_='resp-table-body__label')
            if notes_label:
                notes_text = notes_div.get_text(strip=True).replace(notes_label.get_text(strip=True), '').strip()
                listing['Updated'] = notes_text

        if listing:
            data.append(listing)

    return data

def scrape_vessels_page(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/123.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
    }

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, 'lxml')
    vessels = soup.find_all('div', class_='card-vessel--flex')

    if not vessels:
        return None

    data = []
    for vessel in vessels:
        vessel_data = {}

        title = vessel.find('h3', class_='card-vessel__title')
        if title:
            vessel_data['ID'] = title.text.strip()

        price = vessel.find('span', class_='card-vessel__subtitle')
        if price:
            vessel_data['Price'] = price.text.strip()

        specs = vessel.find('dl', class_='card-vessel__list')
        if specs:
            dts = specs.find_all('dt')
            dds = specs.find_all('dd')
            for dt, dd in zip(dts, dds):
                key = dt.text.strip().rstrip(':')
                value = dd.text.strip()
                vessel_data[key] = value

        description = vessel.find('p')
        if description:
            vessel_data['Description'] = description.text.strip()

        link = vessel.find('a', href=True)
        if link:
            vessel_data['Link'] = f"https://dockstreetbrokers.com{link['href']}"

        data.append(vessel_data)

    return data

def format_ifq_reference():
    # Horrible to be including this as I am. Sue me
    return """
IFQ Area Reference:
2C - Southeast Alaska
3A - Central Gulf of Alaska
3B - Western Gulf of Alaska
4A - Eastern Aleutian Islands
4B - Central/Western Aleutian Islands
4C - Pribilof Islands
4D - Northwestern Bering Sea
4E - Bering Sea Flats

Regulatory Areas for Halibut:
2C - Southeast Outside District
3A - Eastern Gulf of Alaska
3B - Central Gulf of Alaska
4A - Eastern Aleutians
4B - Western Aleutians
4C - Pribilof Islands
4D - Northwestern Bering Sea
4E - Bering Sea

Regulatory Areas for Sablefish:
AI - Aleutian Islands
BS - Bering Sea
CG - Central Gulf
SE - Southeast Outside
WG - Western Gulf
WY - West Yakutat

Vessel Categories:
A - Vessels of any length
B - Vessels less than or equal to 60 feet in length
C - Vessels less than or equal to 35 feet in length
D - Vessels less than or equal to 60 feet in length, not a catcher/processor

Block Status:
B - Blocked quota share
U - Unblocked quota share
"""

def save_data(data, name):
    if not data:
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    df = pd.DataFrame(data)

    output_dir = 'Archive'
    os.makedirs(output_dir, exist_ok=True)

    # add in reference information
    reference_info = format_ifq_reference()

    # save CSV with reference information as header comment, semi jenk
    csv_path = os.path.join(output_dir, f"{name}_{timestamp}.csv")
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        if 'ifq' in name.lower():
            f.write(f"# {reference_info.replace(chr(10), chr(10)+'# ')}\n")
        writer = csv.writer(f,
                          quoting=csv.QUOTE_ALL,
                          quotechar='"',
                          escapechar='\\')
        writer.writerow(df.columns)
        writer.writerows(df.values)

    # save excel file with area/region key
    excel_path = os.path.join(output_dir, f"{name}_{timestamp}.xlsx")
    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Data', index=False)
        if 'ifq' in name.lower():
            # Create reference sheet
            ref_df = pd.DataFrame({'Reference Information': [line for line in reference_info.split('\n') if line.strip()]})
            ref_df.to_excel(writer, sheet_name='IFQ Reference', index=False)

            # Adjust column width for reference sheet
            worksheet = writer.sheets['IFQ Reference']
            worksheet.column_dimensions['A'].width = 100

    print(f"Saved {len(data)} records for {name}")
    print(f"Files saved as {csv_path} and {excel_path}")

def main():
    # Scrape permits
    permit_urls = {
        'alaska_permits': "https://dockstreetbrokers.com/permits/alaska-permits",
        'Halibut_ifq': "https://dockstreetbrokers.com/longline-ifqs/halibut-ifqs",
        'Sablefish_ifq': "https://dockstreetbrokers.com/longline-ifqs/sablefish-ifqs"
    }

    print("Starting permit scraping...")
    for name, url in permit_urls.items():
        print(f"\nScraping {name}...")
        try:
            data = scrape_permits_page(url)
            if data:
                save_data(data, name)
            else:
                print(f"No data found for {name}")
        except Exception as e:
            print(f"Error scraping {name}: {e}")

    # Scrape vessels
    print("\nStarting vessel scraping...")
    base_url = "https://dockstreetbrokers.com/vessels?page="
    page = 1
    all_vessel_data = []

    while True:
        url = f"{base_url}{page}"
        print(f"Scraping vessel page {page}...")

        page_data = scrape_vessels_page(url)

        if page_data is None:
            print(f"No more vessel pages found after page {page-1}")
            break

        all_vessel_data.extend(page_data)
        print(f"Found {len(page_data)} vessels on page {page}")

        page += 1
        time.sleep(0.2)  # Be nice to the server

    if all_vessel_data:
        save_data(all_vessel_data, "vessels")
        print(f"\nTotal vessels scraped: {len(all_vessel_data)}")
    else:
        print("No vessel data found!")

if __name__ == "__main__":
    main()
