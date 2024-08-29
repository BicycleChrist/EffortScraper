import sqlite3
import pathlib
import logging
from pprint import pprint
import datetime

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


def BackupDB(dbpath: pathlib.Path):
    if not dbpath.exists(): return
    newlocation = dbpath
    numbering = 0
    while newlocation.exists():
        numbering += 1
        newname = dbpath.name + str(numbering)
        newlocation = dbpath.parent / newname
    # loop ended; found an untaken filename
    newlocation.write_bytes(dbpath.read_bytes())
    return


def OpenDatabase(dbname: str, create_ifmissing=False, backup_existing=False) -> (sqlite3.Connection, sqlite3.Cursor):
    cwd = pathlib.Path.cwd()
    source = cwd / 'Stable' / dbname
    working_file = cwd / 'Testing' / dbname
    
    if backup_existing:
        BackupDB(source)
        BackupDB(working_file)
    
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
def CreateIndex(dbconnection):
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


def RecurseDictionaryTypes(table_data, recursion_lvl=0):
    leading_indent = ''
    for lvl in range(recursion_lvl):
        leading_indent += '\t'
    print(f"{leading_indent}key_types: {[type(key) for key in table_data.keys()]}")
    print(f"{leading_indent}value_types: {[type(value) for value in table_data.values()]}")
    for (value, value_type) in [(value, type(value)) for value in table_data.values()]:
        if value_type == type(dict()):
            RecurseDictionaryTypes(value, recursion_lvl+1)
    return


# whitespace and parentheses are fine inside of quotes, and apostrophes need to be escaped
def EscapeString(inputstring: str):
    return f"\"{inputstring}\"".replace("'", "`")


def TableFromDict(dbconnection, tablename: str, table_data: dict[any, dict]):
    # whitespace in tablenames / columns is illegal (SQL syntax splits on spaces)
    #tablename_segments = tablename.split() # on whitespace
    #tablename = '_'.join(tablename_segments)
    # whitespace and parentheses are fine inside of quotes
    current_time = str(datetime.datetime.now().isoformat())
    #tz=datetime.timezone.utc
    
    tablename = EscapeString(tablename)
    columns = [ 'game', 'market', 'line', 'value', 'time' ]
    sql_column_defs = [f'{column_name} INTEGER' if column_name == 'value' else f'{column_name} TEXT' for column_name in columns]
    sql_full_column_def = '(' + ', '.join(sql_column_defs) + ')'
    sql_column_bullshit = '(' + ', '.join(columns) + ')'  # when inserting, you don't specify the types
    
    dbconnection.execute(f"CREATE TABLE IF NOT EXISTS {tablename} {sql_full_column_def}")
    
    current_state = {
        'depth'  : 0,
        'game'   : "placeholder",
        'market' : "placeholder",
        'line'   : "placeholder",
        'value'  : "placeholder",
    }
    
    def IterateLambda(data: dict):
        current_depth = current_state['depth']
        state_key = columns[current_depth]
        for key, value in data.items():
            current_state[state_key] = EscapeString(key) 
            if type(value) == type(dict()):
                current_state['depth'] += 1
                IterateLambda(value)
                current_state['depth'] -= 1
            else: # reached the bottom dictionary (line and value)
                current_state['line'] = EscapeString(key) # if you try insert a string without quotes, it'll error: 'no such column'
                current_state['value'] = value  # assuming these are always integers for Pinnacle
                dbconnection.execute(f'INSERT INTO {tablename} {sql_column_bullshit} VALUES {current_state["game"], current_state["market"], current_state["line"], current_state["value"], current_time}')
        return
    IterateLambda(table_data)
    return


def ClaudesExample():
    print("\nbuilding claude's DB...\n")
    dbconnection, dbcursor = OpenDatabase('claudes_database.db', create_ifmissing=True)
    table_name = 'people'
    input_data = {
       'somestring' : {
       1: {'name': 'John' , 'age': 99, 'city': 'New York'   },
       2: {'name': 'Alice', 'age': 25, 'city': 'Los Angeles'},
       3: {'name': 'Bob'  , 'age': 567, 'city': 'Chicago'    },
       },
        44 : {
        'person1': {'name': 'John' , 'age': 343, 'city': 'New York'   },
        'person2': {'name': 'Alice', 'age': 25, 'city': 'Los Angeles'},
        'person3': {'name': 'Bob'  , 'age': 55, 'city': 'Chicago'    },
       },
       12 : {
       'person13': {'name': 'John' , 'age': 30, 'city': 'New York'   },
       'person23': {'name': 'Alice', 'age': 25, 'city': 'Los Angeles'},
       'person33': {'name': 'Bob'  , 'age': 35, 'city': 'Chicago'    },
       },
    }
    print("input_data:")
    pprint(input_data)
    print('\n\n')
    RecurseDictionaryTypes(input_data)
    print('\n\n')
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
