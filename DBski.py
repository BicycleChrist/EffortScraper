import sqlite3
import pathlib


# TODO: actually prevent duplicates here
def RemoveDuplicates(dbname, tablename, field):
    dbconnection = sqlite3.connect(dbname)
    cursor = dbconnection.cursor()

    cursor.execute("DROP TABLE IF EXISTS temptable")
    cursor.execute(
        '''CREATE TABLE temptable AS
            SELECT DISTINCT * FROM graphs
            WHERE file_path IN (
                SELECT DISTINCT file_path FROM graphs
           );'''
    )

    #unduplicated = cursor.execute("SELECT * FROM temptable").fetchall()
    #print(unduplicated)
    dbconnection.commit()
    dbconnection.close()
    return


# TODO: actually do SQL syntax correctly instead of fstring
def CreateTable(tablename, filetype, category="placeholder"):
    cwd = pathlib.Path.cwd()
    basedir = cwd / "NHLvacuum" / "nhlteamreports"

    # Connect to the SQLite database
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
                # Insert the graph file path into the database
                print(f"INSERT INTO {tablename} (team, date, category, filepath) VALUES ('{team}', '{date}', '{category}', '{filepath})'")
                cursor.execute(
                    f"INSERT INTO {tablename} (team, date, category, filepath) VALUES ('{team}', '{date}', '{category}', '{filepath}')"
                )

    dbconnection.commit()
    dbconnection.close()
    return


if __name__ == "__main__":
    CreateTable("graphs", "png", "rollingavggraphs")
    CreateTable("static", "csv", "static_data")
