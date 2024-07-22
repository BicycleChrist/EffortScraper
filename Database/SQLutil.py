import sqlite3
import pathlib
import logging
from pprint import pprint

logging.basicConfig(level=logging.INFO)

def CreateDBFolders():
    cwd = pathlib.Path.cwd()
    if pathlib.Path.cwd().name != "Database":
        print(f"""Your working directory is not correct;
            you should be running this from the 'Database' directory.
            \ncurrently: {pathlib.Path.cwd()}\n""")
        raise PermissionError
    (cwd / 'Stable' ).mkdir(exist_ok=True)
    (cwd / 'Testing').mkdir(exist_ok=True)
    return


def OpenDatabase(dbname: str, create_ifmissing=False) -> (sqlite3.Connection, sqlite3.Cursor):
    cwd = pathlib.Path.cwd()
    source = cwd / 'Stable' / dbname
    working_file = cwd / 'Testing' / dbname
    
    if not working_file.exists():
        print(f"Copying {source.relative_to(cwd)} to {working_file.relative_to(cwd)}")
        if not source.exists():
            if create_ifmissing:
                newdatabase = sqlite3.connect(source)
                newdatabase.commit()
                newdatabase.close()
            else:
                print(f"Error: {source} does not exist")
                return None
        
        with working_file.open("wb") as destination:
            with source.open(mode="rb") as sourcedata:
                destination.write(sourcedata.read())
    
    newconnection = sqlite3.connect(working_file)
    newcursor = newconnection.cursor()
    return newconnection, newcursor


def CreateTable(tablename, filetype, category="placeholder"):
    cwd = pathlib.Path.cwd()
    return

# iterdump seems to return a list of all transactions?
def DumpDatabase(connection: sqlite3.Connection):
    return [x for x in connection.iterdump()]


def ListTables(cursor: sqlite3.Cursor):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    # fetchall returns a list of tuples
    tables = [result[0] for result in cursor.fetchall()]
    return tables


def GetDatabaseStructure(connection: sqlite3.Connection):
    struct_types = ["table", "index", "view", "trigger"]  # this is what's shown in SQLite browser
    cursors = [(stype, connection.cursor()) for stype in struct_types]
    
    for stype, cursor in cursors:
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='{stype}';")
    
    # fetchall returns a list of tuples
    structure = {
        f"{stype.capitalize() + 's'}": [result[0] for result in cursor.fetchall()]
        for stype, cursor in cursors
    }
    return structure


# TODO: figure out how indecies work???
def CreateIndex():
    dbconnection.execute(
        '''CREATE UNIQUE INDEX "indextest2" ON "static" (
            "team" ASC,
            "date",
            "filepath"
        );'''
    )
    return

# convenience wrapper to get results in one call
# flatten_single: since the 'SELECT' command always returns a list of tuples, it's often useful to reduce the result
def CursorExec(cursor, dbcommand, flatten_single=True):
    cursor.execute(dbcommand)
    if flatten_single: 
        return [result[0] for result in cursor.fetchall()]
    return [result for result in cursor.fetchall()]


# builtin database names: "main" and "temp"


def TableFromDict(cursor, tablename: str, table_data: dict[any, dict]):
    # Get the column names from the keys of the first nested dictionary
    columns = [ f"{key}" for key in next(iter(table_data.values())).keys() ]
    # surrounding each one with quotes because SQL will fail if any of the inputs contain spaces
    
    # Create the table
    create_table_sql = f"CREATE TABLE IF NOT EXISTS {tablename} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    create_table_sql += ", ".join([f"{col} TEXT" for col in columns]) # should maybe be 'FLOAT' here instead?
    create_table_sql += ")"
    print("creating table SQL: \n\n")
    print(create_table_sql)
    
    cursor.execute(create_table_sql)
    insert_sql = f"INSERT INTO {tablename} ({', '.join(columns)}) VALUES ({', '.join(['?' for _ in columns])})"
    
    for key, value in table_data.items():
        cursor.execute(insert_sql, [value.get(col, None) for col in columns])
    
    print(f"Table '{tablename}' created successfully with {len(table_data)} rows.")
    return
# failing due to floating-point numbers in the data


def ClaudesExample():
    print("\nbuilding claude's DB...\n")
    dbconnection, dbcursor = OpenDatabase('claudes_database.db', create_ifmissing=True)
    table_name = 'people'
    input_data = {
       'person1': {'name': 'John', 'age': '30', 'city': 'New York'},
       'person2': {'name': 'Alice', 'age': '25', 'city': 'Los Angeles'},
       'person3': {'name': 'Bob', 'age': '35', 'city': 'Chicago'}
    }
    print("input_data:")
    pprint(input_data)
    TableFromDict(dbcursor, table_name, input_data)
    dbconnection.commit()
    dbconnection.close()


def Example():
    dbname = 'teamreports.db'
    dbconnection, dbcursor = OpenDatabase(dbname)

    db_structure = GetDatabaseStructure(dbconnection)
    pprint(db_structure)

    counts = CursorExec(dbcursor, 'SELECT COUNT(*) FROM "main"."graphs"')
    print(counts)
    return



if __name__ == "__main__":
    ClaudesExample()
