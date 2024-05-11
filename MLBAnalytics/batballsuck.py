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


def LoadHTMLfile(file_name):
    cwd = pathlib.Path().cwd()
    html_file_path = cwd / file_name
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
    cwd = pathlib.Path().cwd()
    save_dir = cwd / "MLBstats"
    current_time = time.localtime()
    timestr = str(current_time.tm_hour) + str(current_time.tm_min) + str(current_time.tm_sec)
    file_name = file_name + timestr + '.csv'
    dump_path = save_dir / file_name
    dataframe.to_csv(dump_path, encoding="utf-8")
    return dump_path


if __name__ == "__main__":
    # Minimum number of at bats can be changed via the "qual=" option at end of URL
    url = 'https://www.fangraphs.com/leaders/major-league?pageitems=2000000000&qual=10'
    #soup = LoadHTMLfile('FGhitter.html')
    newsoup = DownloadHTMLfile(url)
    dataframes = extract_table_data(newsoup)
    SaveDataframe(dataframes[0], "fangraph_hitting")

    print("pls dont exit")






