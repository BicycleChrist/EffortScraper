import requests
from bs4 import BeautifulSoup
import tkinter as tk
from tkinter import ttk
from nst_player_ids import NST_playerids
import os
import csv
from datetime import date

baseurl = "https://www.naturalstattrick.com/playerreport.php?"

stype = {
    "Pre-Season": "1",
    "Regular Season": "2",
    "RTP Exhibition": "3",
    "Playoffs": "4",
}

sit = {
    "All Strength": "placeholder",
    "Even Strength": "ev",
    "Power Play": "pp",
    "5 on 4 PP": "5v4",
    "5v5": "5v5",
    "Penalty Kill": "pk",
    "4 on 5 PK": "4v5",
    "3 on 3": "3v3",
    "Against empty net": "ena",
    "With empty net": "enf"
}

stdoi = {
    "On Ice": "oi",
    "Individual": "N/A"
}

rate = {
    "Counts": "c",
    "Rates": "y",
    "Relative": "r",
}

v_section = {
    "Player Summary": "p",
    "Scoring": "s",
    "Teammates": "t",
    "Opposition": "o",
    "Game Log": "g",
}

def Build_NST_URL(playerid, l_stype, l_sit, l_stdoi, l_rate, l_v, fromseason="20232024", thruseason="20232024"):
    newurl = f"{baseurl}stype={stype[l_stype]}&sit={sit[l_sit]}&stdoi={stdoi[l_stdoi]}&rate={rate[l_rate]}&v={v_section[l_v]}&playerid={NST_playerids[playerid]}"
    print(newurl)
    return newurl

# New function to extract table data from the web page
def extract_table_data(url):
    response = requests.get(url)
    table_data = []

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        tables = soup.find_all('table')

        for table in tables:
            rows = table.find_all('tr')
            header_row = rows[0]  # Get the first row as the header row
            header_columns = header_row.find_all('th')
            header_data = [col.text.strip() for col in header_columns]
            table_data.append(header_data)

            for row in rows[1:]:
                columns = row.find_all('td')
                row_data = [col.text.strip() for col in columns]
                table_data.append(row_data)

        return table_data
    else:
        print(f"Failed to retrieve the page. Status code: {response.status_code}")
        return None


# Create the Tkinter window and dropdowns as before
window = tk.Tk()
window.title("NST URL Builder")
currentrow = 0

def CreateDropdown(name, values):
    newvar = tk.StringVar(window)
    newdropdown = ttk.Combobox(window, textvariable=newvar, values=values)
    newdropdown.set(name)
    global currentrow
    currentrow = currentrow + 1
    newdropdown.grid(column=0, row=currentrow)
    return newvar

# Create the button function
def get_selections():
    selected_values = [var.get() for var, _ in Dropdowns]
    player_name = selected_values[0]
    current_date = date.today().strftime("%Y-%m-%d")
    file_name = f"{player_name}{selected_values[2]}--{selected_values[4]}-{current_date}.csv"  # Customize the filename as needed
    csv_filename = os.path.join("playerdata", file_name)

    # Extract table data from the URL
    url = Build_NST_URL(*selected_values)
    table_data = extract_table_data(url)

    if not table_data:
        print("No table data found.")
        return

    # Save the data to a CSV file
    with open(csv_filename, 'w', newline='') as csvfile:
        csv_writer = csv.writer(csvfile)
        # Add a header row to label the columns (replace with actual column names)
        header = []
        csv_writer.writerow(header)
        for row in table_data:
            csv_writer.writerow(row)

    print(f"Data saved to {csv_filename}")


# Create a button to trigger URL extraction
btn_get_selections = tk.Button(window, text="Get URL", command=get_selections)
btn_get_selections.grid(column=2, row=currentrow + 1)

if __name__ == "__main__":
    Dropdowns = [
        (CreateDropdown("playerid", values=list(NST_playerids.keys())),
            lambda x: NST_playerids[x.get()]),
        (CreateDropdown("stype", values=list(stype.keys())),
            lambda x: stype[x.get()]),
        (CreateDropdown("sit", values=list(sit.keys())),
            lambda x: sit[x.get()]),
        (CreateDropdown("stdoi", values=list(stdoi.keys())),
            lambda x: stdoi[x.get()]),
        (CreateDropdown("rate", values=list(rate.keys())),
            lambda x: rate[x.get()]),
        (CreateDropdown("v_section", values=list(v_section.keys())),
            lambda x: v_section[x.get()]),
    ]

    window.mainloop()
