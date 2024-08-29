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


def Main(max_retries=3):
    PinnacleData = None
    scrapeFinished = False
    attempt_num = 1
    
    while not scrapeFinished:
        if ++attempt_num > max_retries: return -1
        print("Asking Pinnacle Nicely...\n\n")
        PinnacleData = PinnaclePlz.Main()
        if len(PinnacleData) == 0:
            print(f"Recieved no data from PinnaclePlz. Retrying (attempt #{attempt_num})...\n")
            continue
        scrapeFinished = True
    
    pprint(PinnacleData)
    AttemptToBuildTable(PinnacleData)
    # AttemptToBuildTable(SampleData)
    return 0


if __name__ == "__main__":
    if pathlib.Path.cwd().name != "Database":
        print(f"""Your working directory is not correct;
            you should be running this from the 'Database' directory.
            \ncurrently: {pathlib.Path.cwd()}\n""")
        exit(1)
    
    CreateDBFolders()
    exit_status = Main()
    if exit_status == 0: print("Success!") 
    else: print(f"\n\nPinnaclePlz failed with nonzero exit_status: {exit_status}\n\n")
    exit(exit_status)

