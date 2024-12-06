import sqlite3
import pandas as pd
from datetime import datetime
import os

def create_database():
    conn = sqlite3.connect('fisheries.db')
    c = conn.cursor()

    # Drop existing tables if they exist
    c.execute('DROP TABLE IF EXISTS vessels')
    c.execute('DROP TABLE IF EXISTS ifq_permits')
    c.execute('DROP TABLE IF EXISTS alaska_permits')



    #TODO: More columns need to be created
    # for now the base data is being data entered properly



    # create primary tables with schemas
    c.execute('''
        CREATE TABLE IF NOT EXISTS vessels (
            id TEXT,
            price DECIMAL(10,2),
            year INTEGER,
            length DECIMAL(5,2),
            hull TEXT,
            builder TEXT,
            location TEXT,
            description TEXT,
            link TEXT,
            scrape_date DATE,
            PRIMARY KEY (id, scrape_date)
        )
    ''')

    #  halibut_ifq and sablefish_ifq CSVs schema
    c.execute('''
        CREATE TABLE IF NOT EXISTS ifq_permits (
            id TEXT,
            type TEXT,
            area TEXT,
            region TEXT,
            lbs DECIMAL(10,2),
            asking TEXT,
            offer TEXT,
            updated TEXT,
            scrape_date DATE,
            PRIMARY KEY (id, scrape_date)
        )
    ''')

    # alaska_permits CSV schema
    c.execute('''
        CREATE TABLE IF NOT EXISTS alaska_permits (
            id TEXT,
            type TEXT,
            asking TEXT,
            offer TEXT,
            updated TEXT,
            scrape_date DATE,
            PRIMARY KEY (id, scrape_date)
        )
    ''')

    conn.commit()
    conn.close()

def parse_timestamp_from_filename(filename):
    """Extract full timestamp from filename format: name_YYYYMMDD_HHMMSS.csv"""
    try:
        date_part = filename.split('_')[-2]
        time_part = filename.split('_')[-1].split('.')[0]
        return datetime.strptime(f"{date_part}_{time_part}", '%Y%m%d_%H%M%S')
    except:
        return None

def create_database():
    conn = sqlite3.connect('fisheries.db')
    c = conn.cursor()

    # Drop existing tables if they exist
    c.execute('DROP TABLE IF EXISTS vessels')
    c.execute('DROP TABLE IF EXISTS ifq_permits')
    c.execute('DROP TABLE IF EXISTS alaska_permits')

    # Create main tables with schemas matching CSV files exactly
    c.execute('''
        CREATE TABLE IF NOT EXISTS vessels (
            id TEXT,
            price DECIMAL(10,2),
            year INTEGER,
            length DECIMAL(5,2),
            hull TEXT,
            builder TEXT,
            location TEXT,
            description TEXT,
            link TEXT,
            scrape_date TIMESTAMP,
            PRIMARY KEY (id, scrape_date)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS ifq_permits (
            id TEXT,
            type TEXT,
            area TEXT,
            region TEXT,
            lbs DECIMAL(10,2),
            asking TEXT,
            offer TEXT,
            updated TEXT,
            scrape_date TIMESTAMP,
            PRIMARY KEY (id, scrape_date)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS alaska_permits (
            id TEXT,
            type TEXT,
            asking TEXT,
            offer TEXT,
            updated TEXT,
            scrape_date TIMESTAMP,
            PRIMARY KEY (id, scrape_date)
        )
    ''')

    conn.commit()
    conn.close()

def import_vessels_data(file_path):
    conn = sqlite3.connect('fisheries.db')
    try:
        df = pd.read_csv(file_path)
        scrape_date = parse_timestamp_from_filename(os.path.basename(file_path))

        # Add scrape_date column
        df['scrape_date'] = scrape_date

        # Convert NaN to None for SQLite
        df = df.where(pd.notnull(df), None)

        # Insert data with REPLACE strategy
        df.to_sql('vessels', conn, if_exists='append', index=False)
        print(f"Successfully imported {len(df)} vessel records")

    except Exception as e:
        print(f"Error processing {file_path}: {str(e)}")
    finally:
        conn.close()

def import_ifq_data(file_path):
    conn = sqlite3.connect('fisheries.db')
    try:
        df = pd.read_csv(file_path, comment='#')
        scrape_date = parse_timestamp_from_filename(os.path.basename(file_path))

        # Add scrape_date column
        df['scrape_date'] = scrape_date

        # Convert NaN to None for SQLite
        df = df.where(pd.notnull(df), None)

        # Insert data with REPLACE strategy
        df.to_sql('ifq_permits', conn, if_exists='append', index=False)
        print(f"Successfully imported {len(df)} IFQ records")

    except Exception as e:
        print(f"Error processing {file_path}: {str(e)}")
    finally:
        conn.close()

def import_alaska_permits_data(file_path):
    conn = sqlite3.connect('fisheries.db')
    try:
        df = pd.read_csv(file_path)
        scrape_date = parse_timestamp_from_filename(os.path.basename(file_path))

        # Add scrape_date column
        df['scrape_date'] = scrape_date

        # Convert NaN to None for SQLite
        df = df.where(pd.notnull(df), None)

        # Insert data with REPLACE strategy
        df.to_sql('alaska_permits', conn, if_exists='append', index=False)
        print(f"Successfully imported {len(df)} Alaska permit records")

    except Exception as e:
        print(f"Error processing {file_path}: {str(e)}")
    finally:
        conn.close()

def import_all_historical_data(archive_dir):
    """Import all historical data from the archive directory"""
    for filename in sorted(os.listdir(archive_dir)):
        if not filename.endswith('.csv'):
            continue

        file_path = os.path.join(archive_dir, filename)
        print(f"\nProcessing {filename}...")

        try:
            if filename.startswith('vessels_'):
                import_vessels_data(file_path)
            elif filename.startswith('Sablefish_ifq_') or filename.startswith('Halibut_ifq_'):
                import_ifq_data(file_path)
            elif filename.startswith('alaska_permits_'):
                import_alaska_permits_data(file_path)
        except Exception as e:
            print(f"Error processing {filename}: {str(e)}")

def update_database():
    """Import newly scraped data into the database"""
    try:
        # Import only the most recent files (from current scrape)
        current_date = datetime.now().strftime("%Y%m%d")
        archive_dir = '/home/retupmoc/Desktop/EffortScraper/PermitData/Archive'

        for filename in os.listdir(archive_dir):
            if current_date in filename and filename.endswith('.csv'):
                file_path = os.path.join(archive_dir, filename)
                print(f"Importing {filename}...")

                if filename.startswith('vessels_'):
                    import_vessels_data(file_path)
                elif filename.startswith('Sablefish_ifq_') or filename.startswith('Halibut_ifq_'):
                    import_ifq_data(file_path)
                elif filename.startswith('alaska_permits_'):
                    import_alaska_permits_data(file_path)

        print("Database update complete.")

    except Exception as e:
        print(f"Error updating database: {str(e)}")

if __name__ == "__main__":
    # create database
    create_database()
    print("Database schema created.")

    # Import historical data (may delete later)
    archive_dir = '/home/retupmoc/Desktop/EffortScraper/PermitData/Archive'
    import_all_historical_data(archive_dir)
    print("\nHistorical data import complete.")
