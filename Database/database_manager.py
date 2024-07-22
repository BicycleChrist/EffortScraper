import sqlite3
import os
from datetime import datetime

DB_NAME = 'pinnacle_odds.db'

def create_database():
    conn = sqlite3.connect(DB_NAME)
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

def ensure_database_exists():
    if not os.path.exists(DB_NAME):
        create_database()

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

if __name__ == "__main__":
    create_database()
    print(f"Database '{DB_NAME}' created successfully.")
