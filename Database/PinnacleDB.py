import update_importpaths
import Boddssuck.PinnaclePlzpoolski as PinnaclePlz
from SQLutil import *
from pprint import pprint

#from SampleData import *

# TODO: SQL won't let you call 'CREATE TABLE' an already-existing table (sqlite3.OperationalError: table "PinnacleTable" already exists)
def AttemptToBuildTable(PinncaleData):
    dbname = 'Pinnacle.db'
    dbconnection, dbcursor = OpenDatabase(dbname, create_ifmissing=True, backup_existing=True)
    #for (gametitle, entries) in PinncaleData.items():
    #    TableFromDict(dbcursor, gametitle, entries)
    TableFromDict(dbconnection, "PinnacleTable", PinncaleData)
    dbconnection.commit()
    dbconnection.close()
    return


if __name__ == "__main__":
    if pathlib.Path.cwd().name != "Database":
        print(f"""Your working directory is not correct;
            you should be running this from the 'Database' directory.
            \ncurrently: {pathlib.Path.cwd()}\n""")
        exit(1)
    
    CreateDBFolders()
    print("Asking Pinnacle Nicely...\n\n")
    PinnacleData = PinnaclePlz.Main()
    pprint(PinnacleData)
    AttemptToBuildTable(PinnacleData)
    # AttemptToBuildTable(SampleData)
    print("Success!")

