import sqlite3
import pathlib
import logging


logging.basicConfig(level=logging.INFO)


def remove_duplicates(dbname, tablename, field):
    try:
        dbconnection = sqlite3.connect(dbname)
        cursor = dbconnection.cursor()

        cursor.execute(f"DELETE FROM {tablename} WHERE ROWID NOT IN (SELECT MIN(ROWID) FROM {tablename} GROUP BY {field})")

        dbconnection.commit()
        logging.info("Duplicates removed successfully.")
    except sqlite3.Error as e:
        logging.error(f"Error removing duplicates: {e}")
    finally:
        dbconnection.close()


from pathlib import Path

def import_logos_to_db(db_file, logos_dir):
    try:
        conn = sqlite3.connect(db_file)
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS TeamLogos
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     sport TEXT,
                     team_name TEXT,
                     logo_path TEXT)''')

        logos_path = Path(logos_dir)
        for sport_folder in logos_path.iterdir():
            if sport_folder.is_dir():
                sport = sport_folder.name
                for logo_file in sport_folder.iterdir():
                    if logo_file.suffix == '.png':
                        team_name = logo_file.stem
                        logo_path = str(logo_file)
                        c.execute('''INSERT INTO TeamLogos (sport, team_name, logo_path)
                                     VALUES (?, ?, ?)''', (sport, team_name, logo_path))

        conn.commit()
        logging.info("Logos imported successfully.")
    except sqlite3.Error as e:
        logging.error(f"Error importing logos: {e}")
    finally:
        conn.close()



def create_table(tablename, filetype, category="placeholder"):
    try:
        cwd = pathlib.Path.cwd()
        basedir = cwd / "NHLvacuum" / "nhlteamreports"

        dbconnection = sqlite3.connect('teamreports.db')
        cursor = dbconnection.cursor()

        cursor.execute(f"DROP TABLE IF EXISTS {tablename}")
        cursor.execute(f'''CREATE TABLE IF NOT EXISTS {tablename}
                           (id INTEGER PRIMARY KEY, team TEXT, date TEXT, category TEXT, filepath TEXT)''')

        team_subdirs = [subdir for subdir in basedir.glob("*")]
        for team_subdir in team_subdirs:
            team = team_subdir.name
            team_subdir = team_subdir / "generalTRdata"
            date_subdirs = [subdir for subdir in team_subdir.glob("*")]
            for date_subdir in date_subdirs:
                date = date_subdir.name
                filepaths = date_subdir.rglob(f"*.{filetype}")
                for filepath in filepaths:
                    cursor.execute(
                        "INSERT INTO {} (team, date, category, filepath) VALUES (?, ?, ?, ?)".format(tablename),
                        (team, date, category, str(filepath))
                    )

        dbconnection.commit()  # Changed from conn to dbconnection

        # Import logos to database after creating tables
        import_logos_to_db('teamreports.db', '/home/retupmoc/Desktop/EffortScraper/TeamLogos')

        logging.info("Tables created and logos imported successfully.")
    except sqlite3.Error as e:
        logging.error(f"Error creating table or importing logos: {e}")
    finally:
        dbconnection.close()



if __name__ == "__main__":
    create_table("graphs", "png", "rollingavggraphs")
    create_table("static", "csv", "static_data")
    remove_duplicates("teamreports.db", "graphs", "filepath")
