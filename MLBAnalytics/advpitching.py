import requests
from bs4 import BeautifulSoup
import pandas as pd
from bs4 import SoupStrainer
from pathlib import Path
import lxml

url = "https://www.fangraphs.com/leaders/major-league?month=0&pos=all&stats=pit&type=1&qual=5&sortcol=20&sortdir=asc&pagenum=1&pageitems=2000000000"

response = requests.get(url)

def Main():
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'lxml')
        toplevel = soup.find('div', class_='fg-data-grid table-type').extract()
        bottomlevel = toplevel.find('div', class_='table-wrapper-inner').extract()

        if toplevel:
            df = pd.read_html(str(bottomlevel))[0]

            output_folder = Path("MLBstats")
            output_folder.mkdir(exist_ok=True)

            df.to_csv(output_folder / "fangraphs_advpitching.csv", index=False)
            print("Table saved successfully in MLBstats folder.")
        else:
            print("No table found on the page.")
    else:
        print(f"Failed to retrieve the page. Status code: {response.status_code}")


if __name__ == "__main__":
    Main()
