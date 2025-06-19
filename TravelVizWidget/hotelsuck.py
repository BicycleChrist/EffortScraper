import requests
from bs4 import BeautifulSoup
import time
import csv

class ForbesHotelScraper:
    def __init__(self):
        self.base_url = "https://forbestravelguide.com"
        self.destinations_url = "https://forbestravelguide.com/destinations-list"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def get_destination_links(self):
        """Extract all destination links from the main destinations page"""
        print("Fetching destination links...")
        response = self.session.get(self.destinations_url)
        soup = BeautifulSoup(response.content, 'html.parser')

        destination_links = []
        destination_block = soup.find('div', id='destinationListBlock')

        if destination_block:
            links = destination_block.find_all('a', class_='destinationItem')
            print("FOUND DESTINATION BLOCK")
            for link in links:
                href = link.get('href')
                print(f'{link}')
                if href:
                    full_url = self.base_url + href
                    destination_name = link.text.strip()
                    destination_links.append((destination_name, full_url))


        print(f"Found {len(destination_links)} destinations")
        return destination_links

    def scrape_destination_hotels(self, destination_name, destination_url):
        """Scrape hotel data from a specific destination page"""
        print(f"Scraping hotels for: {destination_name}")

        try:
            response = self.session.get(destination_url)
            soup = BeautifulSoup(response.content, 'html.parser')

            hotels = []
            # Find all hotel divs with the specific class structure
            propertyList = soup.find('div', id='propertyList')
            hotel_divs = soup.find_all('div')
            print(f"found {len(hotel_divs)} divs under propertylist")

            for hotel_div in hotel_divs:
                data_rating = hotel_div.get('data-rating', '')
                data_name = hotel_div.get('data-name', '')

                if data_rating:
                    # Parse the rating data (e.g., "FOUR_STAR aman new york")
                    parts = data_rating.split(' ', 1)
                    star_rating = parts[0] if parts else ''
                    hotel_name = parts[1] if len(parts) > 1 else data_name

                    hotels.append({
                        'destination': destination_name,
                        'hotel_name': hotel_name,
                        'star_rating': star_rating,
                        'full_rating_data': data_rating
                    })

            print(f"Found {len(hotels)} hotels in {destination_name}")
            return hotels

        except Exception as e:
            print(f"Error scraping {destination_name}: {e}")
            return []

    def scrape_all_hotels(self):
        """Main method to scrape all hotels from all destinations"""
        all_hotels = []

        # Get all destination links
        destinations = self.get_destination_links()

        # Scrape each destination
        for i, (dest_name, dest_url) in enumerate(destinations):
            hotels = self.scrape_destination_hotels(dest_name, dest_url)
            all_hotels.extend(hotels)

            # Be polite - add delay between requests
            time.sleep(1)

            # Progress update
            if (i + 1) % 10 == 0:
                print(f"Processed {i + 1}/{len(destinations)} destinations")

        return all_hotels

    def save_to_csv(self, hotels, filename='forbes_hotels.csv'):
        """Save hotel data to CSV file"""
        if not hotels:
            print("No hotel data to save")
            return

        with open(filename, 'w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=['destination', 'hotel_name', 'star_rating', 'full_rating_data'])
            writer.writeheader()
            writer.writerows(hotels)

        print(f"Saved {len(hotels)} hotels to {filename}")

def main():
    scraper = ForbesHotelScraper()

    # Scrape all hotels
    hotels = scraper.scrape_all_hotels()

    # Save to CSV
    scraper.save_to_csv(hotels)

    # Print summary
    print(f"\nScraping complete!")
    print(f"Total hotels found: {len(hotels)}")

    # Show sample data
    if hotels:
        print("\nSample hotel data:")
        for hotel in hotels[:5]:
            print(f"- {hotel['hotel_name']} ({hotel['star_rating']}) in {hotel['destination']}")

if __name__ == "__main__":
    main()
