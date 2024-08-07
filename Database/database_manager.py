import sqlite3
import os
from datetime import datetime

DB_NAME = 'pinnacle_odds.db'

def ensure_database_exists():
    if not os.path.exists(DB_NAME):
        create_database()

def create_database():
    conn = sqlite3.connect(DB_NAME, uri=True)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS player_props
                 (prop_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  game_url TEXT NOT NULL,
                  player_name TEXT NOT NULL,
                  market TEXT NOT NULL,
                  line REAL NOT NULL,
                  over_odds INTEGER,
                  under_odds INTEGER,
                  scrape_date TEXT)''')

    conn.commit()
    conn.close()


def insert_player_prop(game_url, player_name, market, line, over_odds, under_odds):
    ensure_database_exists()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    scrape_date = datetime.now().isoformat()
    c.execute("""
        INSERT INTO player_props 
        (game_url, player_name, market, line, over_odds, under_odds, scrape_date) 
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (game_url, player_name, market, line, over_odds, under_odds, scrape_date))
    
    conn.commit()
    conn.close()
    return

# This is the function that was lost, now refound to bring it all back together
def process_pinnacle_data(game_url, data):
    for game, markets in data.items():
        for player_market, odds in markets.items():
            player_name, market = player_market.rsplit(' (', 1)
            market = market.rstrip(')')
            
            line = None
            over_odds = None
            under_odds = None
            
            for line_type, line_odds in odds.items():
                if line_type.startswith('Over'):
                    line = float(line_type.split()[1])
                    over_odds = line_odds
                elif line_type.startswith('Under'):
                    under_odds = line_odds
            insert_player_prop(game_url, player_name, market, line, over_odds, under_odds)
    print("Data successfully inserted into the database.")
    return

if __name__ == "__main__":
    create_database()
    print(f"Database '{DB_NAME}' created successfully.")
