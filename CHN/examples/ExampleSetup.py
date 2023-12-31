import pathlib
from bs4 import BeautifulSoup

exampledir = pathlib.Path.cwd() / "examples"
examplesources = [(exampledir / "schedule.html") , (exampledir / "boxscore.html")]
dumpdir = exampledir / "dumpedtables"

# simulates result of Downloading/extracting schedule-file
def DumpExample(examplefile=examplesources[0]):
    if not examplefile.exists():
        print(f"ERROR: file not found at: {examplefile.absolute()}")
        SystemExit()  # TODO: add error handling more proper than 'print & SystemExit' to this file
    destfile = dumpdir / "example_schedule.html"
    with open(examplefile, 'r', encoding='utf-8') as file:
        soup = BeautifulSoup(file, 'html.parser')
        table = soup.find('table', attrs={'class': 'data schedule'})
    with open(destfile, 'w', encoding='utf-8') as dfile:
        dfile.write(table.decode())
    return table


def LoadExample(usedump=True):
    if usedump:
        examplefile=pathlib.Path(dumpdir/"example_schedule.html")
    else:
        examplefile=examplesources[0]
    if not examplefile.exists():
        print(f"ERROR: file not found at: {examplefile.absolute()}")
        return None
    with open(examplefile, 'r', encoding='utf-8') as file:
        soup = BeautifulSoup(file, 'html.parser')
        #table = soup.find('table', attrs={'class': 'data schedule'})
    return soup
