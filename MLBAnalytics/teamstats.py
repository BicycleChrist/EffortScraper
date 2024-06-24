import requests
from bs4 import BeautifulSoup
import pandas as pd

# Function to extract table data from a given URL
def extract_table(url, table_index):
    response = requests.get(url)
    response.raise_for_status()  # Check if the request was successful

    soup = BeautifulSoup(response.content, 'html.parser')
    tables = soup.find_all('table')

    if table_index < len(tables):
        table = tables[table_index]
        return table
    else:
        raise IndexError("Table index out of range.")

# URLs and table index details
urls = [
    "https://sports.yahoo.com/mlb/stats/team/?selectedTable=0",
    "https://sports.yahoo.com/mlb/stats/team/?selectedTable=1"
]
table_indices = [0, 0]  # Assuming both URLs have the tables we want at index 0

# Extract tables from both URLs
tables = [extract_table(url, table_index) for url, table_index in zip(urls, table_indices)]

# Names for the DataFrames
df_names = ["hitting", "pitching"]

# Convert the HTML tables to pandas DataFrames
dataframes = {}
for i, table in enumerate(tables):
    # Extract headers
    headers = [th.text.strip() for th in table.find_all('th')]

    # Extract rows
    rows = []
    for tr in table.find_all('tr')[1:]:  # Skip header row
        cells = [td.text.strip() for td in tr.find_all('td')]
        if cells:
            rows.append(cells)

    # Create DataFrame
    df = pd.DataFrame(rows, columns=headers)
    dataframes[df_names[i]] = df


pitching_df = dataframes["pitching"]
hitting_df = dataframes["hitting"]

pitching_df['G'] = pd.to_numeric(pitching_df['G'], errors='coerce')
pitching_df['ERA'] = pd.to_numeric(pitching_df['ERA'], errors='coerce')
hitting_df['H'] = pd.to_numeric(hitting_df['H'], errors='coerce')
hitting_df['R'] = pd.to_numeric(hitting_df['R'], errors='coerce')

#  total team earned runs
pitching_df['Earned Runs'] = pitching_df['G'] * pitching_df['ERA']
hitting_df['Hits per R'] = round(hitting_df['H'] / hitting_df['R'], 2)
average_hits_per_r = round(hitting_df['Hits per R'].mean(), 2)
hitting_df['TeamAVG Hits/R'] = [average_hits_per_r] + [None] * (len(hitting_df) - 1)

dataframes["hitting"].to_csv('hitting_table.csv', index=False)
pitching_df.to_csv('pitching_table.csv', index=False)


print("Hitting Table:")
print(dataframes["hitting"].head())
print("\n")

print("Updated Pitching Table with Earned Runs:")
print(pitching_df.head())
print("\n")
