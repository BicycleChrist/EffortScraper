import update_importpaths
import Boddssuck.PinnaclePlzpoolski as PinnaclePlz
from SQLutil import *

from pprint import pprint


def AttemptToBuildTable(PinncaleData):
    pass


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
    
