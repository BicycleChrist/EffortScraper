from ProbablePitchersFrame import *
from stuffsuck import get_pitching_data
import pandas as pd
from BBSavant_statcast import scrape
from BBSplayer_ids import pitchers
import tkinter as tk
from tkinter import ttk

# adding stuff-plus data into the ProbablePitchersFrame

def CreateTabLayout(matchupframe, matchup_dict, dataframe):
    print("tablayout")
    # Collect all pitcher names
    pitcher_names = list(matchup_dict['pitchers'].keys())

    # Create a dictionary to store the reformatted pitcher names
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

    for pitcher_name, pitcher_dict in matchup_dict['pitchers'].items():
        pitcher_frame = ttk.LabelFrame(matchupframe, text=pitcher_name)
        pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")
        for key, value in pitcher_dict.items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")

        # Retrieve cached pitching data from the dictionary
        pitcher_data = pitcher_data_map.get(pitcher_name)
        if pitcher_data is not None:
            # Stuff+ frame for each pitcher
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
    PPFrame = PPFrameT(master=toplevel)
    TOPLEVEL = PPFrame
    PPFrame.pack()
    # defining callback hook
    dataframe = get_pitching_data() #DON'T CALL THIS INLINE IN THE LAMBDA!!!!!! It will re-download EVERY ITERATION!

    PPFrame.DownloadButtonHook = lambda a,b: CreateTabLayout(a,b, dataframe)

    print("mainloop")
    toplevel.mainloop()

if __name__ == "__main__":
    toplevel = tkinter.Tk()
    toplevel.title("ProbablePitchers")
    Main()








