from bs4 import BeautifulSoup
from bs4 import SoupStrainer
import requests
import pathlib
import pandas
import time


# Extract tableski data from HTML
def extract_table_data(soup):
    toplevel = soup.find('div', class_="fg-data-grid table-type").extract()
    bottomlevel = toplevel.find('div', class_='table-wrapper-inner').extract()
    dataframes = pandas.read_html(bottomlevel.encode(), encoding="utf-8")  # list of two tables; 'table-scroll' and 'table-fixed'(hidden)
    # they should be the same table, probably
    return dataframes

    #tables = bottomlevel.contents
    #print([table.attrs['class'] for table in tables])
    ##tablestwo = {'table-scroll': bottomlevel.find('div', class_='table'), 'table-fixed': bottomlevel.find('div', class_='table-fixed')}
    #print("plzbreak")
    #table_scroll = tables[0]
    ## pandas.read_html always returns a list of dataframes (even if only one is returned)
    #dataframe = pandas.read_html(table_scroll.encode())[0]
    ##dataframes = [pandas.read_html(table.encode()) for table in tables]
    #return


def GetSaveDir():
    cwd = pathlib.Path().cwd()
    save_dir = cwd / "MLBstats"
    if not save_dir.exists():
        save_dir.mkdir()
    return save_dir


def LoadHTMLfile(file_name):
    save_dir = GetSaveDir()
    html_file_path = save_dir / file_name
    if not html_file_path.exists():
        print(f"error: {file_name} not found!")
        return None
    with open(html_file_path, encoding="utf-8", mode='r') as html_file:
        newsoup = BeautifulSoup(html_file.read(), 'lxml')
    return newsoup


def DownloadHTMLfile(url):
    response = requests.get(url)
    if response.status_code != 200:
        print("did not return 200")
        return None
    newsoup = BeautifulSoup(response.content, 'lxml').extract()
    return newsoup


def SaveDataframe(dataframe, file_name):
    current_time = time.localtime()  # Get the current time
    timestr = '_' + time.strftime('%d%m%Y', current_time)
    save_dir = GetSaveDir()
    file_name = file_name + timestr + '.csv'
    dump_path = save_dir / file_name
    dataframe.to_csv(dump_path, encoding="utf-8")
    return dump_path

def get_pitching_data():
    # Minimum number of at bats can be changed via the "qual=" option at end of URL
    qual = 1
    url = 'https://www.fangraphs.com/leaders/major-league?type=36&pos=all&stats=pit&sortcol=3&sortdir=default&qual=1&pagenum=1&pageitems=2000000000'
    newsoup = DownloadHTMLfile(url)
    dataframes = extract_table_data(newsoup)
    return dataframes[0]

if __name__ == "__main__":
    # Minimum number of at bats can be changed via the "qual=" option at end of URL
    qual = 1
    url = 'https://www.fangraphs.com/leaders/major-league?type=36&pos=all&stats=pit&sortcol=3&sortdir=default&qual=1&pagenum=1&pageitems=2000000000'
    #soup = LoadHTMLfile('FGhitter.html')
    newsoup = DownloadHTMLfile(url)
    dataframes = extract_table_data(newsoup)
    SaveDataframe(dataframes[0], "stuffplustable_data")

    print("pls dont exit")

# full url
#url = 'https://www.fangraphs.com/leaders/major-league?type=36&pos=all&stats=pit&sortcol=3&sortdir=default&qual=1&pagenum=1&pageitems=2000000000'
