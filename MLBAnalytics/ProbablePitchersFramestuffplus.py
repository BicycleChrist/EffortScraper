import update_importpaths
import pandas as pd
import os
import tkinter
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
import threading
from DmFrame import *
from DmNotebook import *
from ProbablePitchers import *
from stuffsuck import get_pitching_data
from datetime import datetime
# The amount of time it takes to load the GUI with the stuff+ metrics versus without is dogshit
#TODO: Make faster I Suck
import pickle
LAYOUTMETHOD = tkinter.Widget.pack




class PPFrameT(DmFrameT):
    def __init__(self, master):
        super().__init__(master)
        self.probable_pitchers_data = []
        self.DownloadButton = ttk.Button(master=self, text="download", command=self.DownloadButtonLambda)
        self.DownloadButton.pack()
        self.MatchupDisplay, self.MatchupNB = InsertFrame(
            self, "MatchupDisplay",
            DmNotebookT, self, tkinter.Widget.pack
        )

    def DownloadButtonLambda(self):
        threading.Thread(target=self.DownloadData).start()

    def DownloadData(self):
        soup = FetchProbablePitchers()
        if not soup: 
            print("error: no soup")
            return
        self.probable_pitchers_data = ParseProbablePitchers(soup)
        self.master.after(0, self.UpdateGUI)

        # Cache the pitching data
        pitching_data = get_pitching_data()
        cache_filename = self.get_cache_filename()
        with open(cache_filename, 'wb') as f:
            pickle.dump(pitching_data, f)

    def UpdateGUI(self):
        for matchup_dict in self.probable_pitchers_data['matchups']:
            new_tab = self.MatchupNB.AddTab(matchup_dict['title'])
            matchup_frame = ttk.LabelFrame(new_tab, text=matchup_dict['title'])
            matchup_frame.pack(expand=True, fill="both", side="top", anchor="n")
            CreateTabLayout(matchup_frame, matchup_dict, self.load_cached_pitching_data())
        self.DownloadButton.pack_forget()  # delete the button

    def load_cached_pitching_data(self):
        cache_filename = self.get_cache_filename()
        # Check if the cache file exists
        if os.path.exists(cache_filename):
            with open(cache_filename, 'rb') as f:
                pitching_data = pickle.load(f)
            return pitching_data
        else:
            print("Pitching data cache not found.")
            return None

    def get_cache_filename(self):
        today = datetime.now()
        cache_filename = f"stuffplustable_data_{today.strftime('%d-%m-%Y')}.pkl"
        return cache_filename


def CreateTabLayout(matchupframe, matchup_dict, df):
    # Collect all pitcher names
    pitcher_names = list(matchup_dict['pitchers'].keys())

    # Filter the dataframe once for all pitcher names
    filtered_df = df[df['Name'].isin(pitcher_names)]

    # Create a dictionary to map pitcher names to their data
    pitcher_data_map = {row['Name']: row for index, row in filtered_df.iterrows()}
    
    column_headers_to_display = [
        'Stf+ FA', 'Stf+ SI', 'Stf+ FC', 'Stf+ FS',
        'Stf+ SL', 'Stf+ CU', 'Stf+ CH', 'Stf+ KC', 'Stf+ FO', 'Stuff+', 'Location+', 'Pitching+'

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


if __name__ == "__main__":
    toplevel = tkinter.Tk()
    toplevel.title("ProbablePitchers")
    PPFrame = PPFrameT(master=toplevel)
    TOPLEVEL = PPFrame
    PPFrame.pack()
    toplevel.mainloop()




