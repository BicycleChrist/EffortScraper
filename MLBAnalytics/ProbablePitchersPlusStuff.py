import os
import pandas as pd
import tkinter as tk
from tkinter import ttk
from ProbablePitchersFrame import PPFrameT
from stuffsuck import get_pitching_data
from BBSavant_statcast import scrape
from BBSplayer_ids import pitchers

# Code is very jenk
# The .csv file names of the team bullpen stats are not named correctly in order to be loaded by this GUI for some reason
#TODO: Alter the file naming functionality for penski, or some other fix

def load_bullpen_data(team_name):
   
    file_path = f'/home/retupmoc/Desktop/EffortScraper/MLBAnalytics/MLBstats/BPdata/{team_name}_Bullpen_Usage_bullpen_stats_30052024.csv'
    
    if os.path.exists(file_path):
        # Read the CSV file into a DataFrame
        return pd.read_csv(file_path)
    else:
        print(f"File not found: {file_path}")
        return None

def CreateTabLayout(matchupframe, matchup_dict, dataframe):
    print("tablayout")
    # Collect all pitcher names
    pitcher_names = list(matchup_dict['pitchers'].keys())

    # Create a dictionary to store the reformatted pitcher names
    # This is being done in order to perform a lookup in the BBS dict to pass into the scrape function
    formatted_pitcher_names = {}
    pitcher_id_map = {}

    for name in pitcher_names:
        last_first_name = ", ".join(name.split()[::-1])
        formatted_pitcher_names[name] = last_first_name

        # Retrieve the player ID from the pitchers dictionary
        player_id = pitchers.get(last_first_name)
        pitcher_id_map[last_first_name] = player_id

    # Filter the dataframe once for all pitcher names
    filtered_df = dataframe[dataframe['Name'].isin(pitcher_names)]

    # Create a dictionary to map pitcher names to their data
    pitcher_data_map = {row['Name']: row for index, row in filtered_df.iterrows()}

    column_headers_to_display = [
        'Stf+ FA', 'Stf+ SI', 'Stf+ FC', 'Stf+ FS',
        'Stf+ SL', 'Stf+ CU', 'Stf+ CH', 'Stf+ KC', 'Stf+ FO',
        'Stuff+', 'Location+', 'Pitching+'
    ]

    
    for side in ['home', 'away']:
        team_name = matchup_dict['teams'][side]['name']
        bullpen_data = load_bullpen_data(team_name)
        if bullpen_data is not None:
            bullpen_frame = ttk.LabelFrame(matchupframe, text=f"{team_name} Bullpen Usage")
            bullpen_frame.pack(expand=True, fill="both", side="bottom", anchor="sw")

            # Extract column names 
            column_names = list(bullpen_data.columns)

            # Create a Treeview 
            bullpen_treeview = ttk.Treeview(bullpen_frame, columns=column_names, show='headings')
            for col in column_names:
                # Set column headings
                bullpen_treeview.heading(col, text=col)
                # Set column widths
                bullpen_treeview.column(col, width=100, anchor='center')
            bullpen_treeview.pack(expand=True, fill="both", padx=2, pady=2)

            # Iterate over DataFrame rows and insert  into the Treeview
            for index, row in bullpen_data.iterrows():
                # Extracting values for each row
                values = [row[col] for col in column_names]
                # Insert row into the Treeview
                bullpen_treeview.insert("", "end", values=values)

   
    for pitcher_name, pitcher_dict in matchup_dict['pitchers'].items():
        pitcher_frame = ttk.LabelFrame(matchupframe, text=pitcher_name)
        pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")
        for key, value in pitcher_dict.items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")

        
        pitcher_data = pitcher_data_map.get(pitcher_name)
        if pitcher_data is not None:
            pitcher_stats_frame = ttk.LabelFrame(matchupframe, text=f"{pitcher_name} Stuff+ Stats")
            pitcher_stats_frame.pack(expand=True, fill="both", side="top", anchor="sw")

            for header in column_headers_to_display:
                if header in pitcher_data and not pd.isnull(pitcher_data[header]):
                    stat_value = pitcher_data[header]
                    stat_label = ttk.Label(pitcher_stats_frame, text=f"{header}: {stat_value}")
                    stat_label.pack(side="left", padx=5, pady=5)

    print("Formatted Pitcher Names:", formatted_pitcher_names)
    print("Pitcher ID Map:", pitcher_id_map)

def Main():
    toplevel = tk.Tk()
    toplevel.title("ProbablePitchers")
    PPFrame = PPFrameT(master=toplevel)
    PPFrame.pack()

    # Define callback hook
    dataframe = get_pitching_data()  # DON'T CALL THIS INLINE IN THE LAMBDA!!!!!! It will re-download EVERY ITERATION!

    PPFrame.DownloadButtonHook = lambda a, b: CreateTabLayout(a, b, dataframe)

    print("mainloop")
    toplevel.mainloop()

if __name__ == "__main__":
    Main()








