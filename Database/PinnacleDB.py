import update_importpaths
import Boddssuck.PinnaclePlzpoolski as PinnaclePlz
from SQLutil import *
from pprint import pprint


def AttemptToBuildTable(PinncaleData):
    dbname = 'Pinnacle.db'
    dbconnection, dbcursor = OpenDatabase(dbname, create_ifmissing=True)
    #for (gametitle, entries) in PinncaleData.items():
    #    TableFromDict(dbcursor, gametitle, entries)
    TableFromDict(dbconnection, "PinnacleTable", PinncaleData)
    dbconnection.commit()
    dbconnection.close()
    return

#from SampleData import *
test_dict = {
    'Arizona Diamondbacks vs Kansas City Royals': {'Adam Frazier (Total Bases)(must start)': {'Over 1.5 TotalBases': 142,
                                                                                           'Under 1.5 TotalBases': -196},
                                                'Alek Thomas (Total Bases)(must start)': {'Over 0.5 TotalBases': -175,
                                                                                          'Under 0.5 TotalBases': 129},
                                                'Bobby Witt Jr. (Home Runs)(must start)': {'Over 0.5 HomeRuns': 489,
                                                                                           'Under 0.5 HomeRuns': -855},
                                                'Bobby Witt Jr. (Total Bases)(must start)': {'Over 1.5 TotalBases': -146,
                                                                                             'Under 1.5 TotalBases': 110},
                                                'Christian Walker (Home Runs)(must start)': {'Over 0.5 HomeRuns': 577,
                                                                                             'Under 0.5 HomeRuns': -1098},
                                                'Christian Walker (Total Bases)(must start)': {'Over 0.5 TotalBases': -182,
                                                                                               'Under 0.5 TotalBases': 133},
                                                'Cole Ragans (Earned Runs)(must start)': {'Over 1.5 EarnedRuns': -154,
                                                                                          'Under 1.5 EarnedRuns': 115},
                                                'Cole Ragans (Hits Allowed)(must start)': {'Over 4.5 HitsAllowed': -184,
                                                                                           'Under 4.5 HitsAllowed': 131},
                                                'Cole Ragans (Pitching Outs)(must start)': {'Over 17.5 PitchingOuts': -190,
                                                                                            'Under 17.5 PitchingOuts': 131},
                                                'Cole Ragans (Total Strikeouts)(must start)': {'Over 6.5 Strikeouts': 103,
                                                                                               'Under 6.5 Strikeouts': -145},
                                                'Eugenio Suarez (Home Runs)(must start)': {'Over 0.5 HomeRuns': 741,
                                                                                           'Under 0.5 HomeRuns': -1648},
                                                'Eugenio Suarez (Total Bases)(must start)': {'Over 0.5 TotalBases': -115,
                                                                                             'Under 0.5 TotalBases': -115},
                                                'Freddy Fermin (Home Runs)(must start)': {'Over 0.5 HomeRuns': 882,
                                                                                          'Under 0.5 HomeRuns': -2229},
                                                'Freddy Fermin (Total Bases)(must start)': {'Over 0.5 TotalBases': -225,
                                                                                            'Under 0.5 TotalBases': 160},
                                                'Gabriel Moreno (Total Bases)(must start)': {'Over 1.5 TotalBases': 140,
                                                                                             'Under 1.5 TotalBases': -192},
                                                'Geraldo Perdomo (Total Bases)(must start)': {'Over 0.5 TotalBases': -155,
                                                                                              'Under 0.5 TotalBases': 116},
                                                'Hunter Renfroe (Home Runs)(must start)': {'Over 0.5 HomeRuns': 541,
                                                                                           'Under 0.5 HomeRuns': -996},
                                                'Hunter Renfroe (Total Bases)(must start)': {'Over 1.5 TotalBases': 141,
                                                                                             'Under 1.5 TotalBases': -194},
                                                'Jake McCarthy (Total Bases)(must start)': {'Over 0.5 TotalBases': -224,
                                                                                            'Under 0.5 TotalBases': 159},
                                                'Ketel Marte (Home Runs)(must start)': {'Over 0.5 HomeRuns': 531,
                                                                                        'Under 0.5 HomeRuns': -966},
                                                'Ketel Marte (Total Bases)(must start)': {'Over 1.5 TotalBases': 107,
                                                                                          'Under 1.5 TotalBases': -143},
                                                'Kyle Isbel (Total Bases)(must start)': {'Over 0.5 TotalBases': -163,
                                                                                         'Under 0.5 TotalBases': 121},
                                                'Lourdes Gurriel Jr. (Home Runs)(must start)': {'Over 0.5 HomeRuns': 718,
                                                                                                'Under 0.5 HomeRuns': -1564},
                                                'Lourdes Gurriel Jr. (Total Bases)(must start)': {'Over 1.5 TotalBases': 126,
                                                                                                  'Under 1.5 TotalBases': -170},
                                                'Maikel Garcia (Total Bases)(must start)': {'Over 1.5 TotalBases': 163,
                                                                                            'Under 1.5 TotalBases': -231},
                                                'Michael Massey (Home Runs)(must start)': {'Over 0.5 HomeRuns': 592,
                                                                                           'Under 0.5 HomeRuns': -1144},
                                                'Michael Massey (Total Bases)(must start)': {'Over 1.5 TotalBases': 124,
                                                                                             'Under 1.5 TotalBases': -168},
                                                'Randal Grichuk (Home Runs)(must start)': {'Over 0.5 HomeRuns': 720,
                                                                                           'Under 0.5 HomeRuns': -1571},
                                                'Randal Grichuk (Total Bases)(must start)': {'Over 0.5 TotalBases': -209,
                                                                                             'Under 0.5 TotalBases': 150},
                                                'Salvador Perez (Home Runs)(must start)': {'Over 0.5 HomeRuns': 406,
                                                                                           'Under 0.5 HomeRuns': -653},
                                                'Salvador Perez (Total Bases)(must start)': {'Over 1.5 TotalBases': 111,
                                                                                             'Under 1.5 TotalBases': -148},
                                                'Vinnie Pasquantino (Home Runs)(must start)': {'Over 0.5 HomeRuns': 516,
                                                                                               'Under 0.5 HomeRuns': -927},
                                                'Vinnie Pasquantino (Total Bases)(must start)': {'Over 1.5 TotalBases': 111,
                                                                                                 'Under 1.5 TotalBases': -148},
                                                'Yilber Diaz (Earned Runs)(must start)': {'Over 2.5 EarnedRuns': -119,
                                                                                          'Under 2.5 EarnedRuns': -112},
                                                'Yilber Diaz (Hits Allowed)(must start)': {'Over 5.5 HitsAllowed': 114,
                                                                                           'Under 5.5 HitsAllowed': -165},
                                                'Yilber Diaz (Pitching Outs)(must start)': {'Over 15.5 PitchingOuts': -115,
                                                                                            'Under 15.5 PitchingOuts': -115},
                                                'Yilber Diaz (Total Strikeouts)(must start)': {'Over 3.5 Strikeouts': -121,
                                                                                               'Under 3.5 Strikeouts': -114}},}


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
    #AttemptToBuildTable(SampleData)

