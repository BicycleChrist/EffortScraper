import update_importpaths

import tkinter
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from DmFrame import *
from DmNotebook import *
from ProbablePitchers import *
from stuffsuck import *

# The amount of time it takes to load the GUI with the stuff+ metrics versus without is dogshit
#TODO: Make faster I Suck

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
        soup = FetchProbablePitchers()
        if not soup: 
            print("error: no soup")
            return
        self.probable_pitchers_data = ParseProbablePitchers(soup)
        for matchup_dict in self.probable_pitchers_data['matchups']:
            new_tab = self.MatchupNB.AddTab(matchup_dict['title'])
            matchup_frame = ttk.LabelFrame(new_tab, text=matchup_dict['title'])
            matchup_frame.pack(expand=True, fill="both", side="top", anchor="n")
            CreateTabLayout(matchup_frame, matchup_dict, get_pitching_data())
        self.DownloadButton.pack_forget()  # delete the button

def CreateTabLayout(matchupframe, matchup_dict, df):
    for pitcher_name, pitcher_dict in matchup_dict['pitchers'].items():
        pitcher_frame = ttk.LabelFrame(matchupframe, text=pitcher_name)
        pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")
        for key, value in pitcher_dict.items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")

    # Add labels for column headers
    column_headers_to_display = [
        'Stf+ FA', 'Stf+ SI', 'Stf+ FC', 'Stf+ FS',
        'Stf+ SL', 'Stf+ CU', 'Stf+ CH', 'Stf+ KC', 'Stf+ FO', 'Stuff+', 'Location+', 'Pitching+'

    ]

    for pitcher_name in matchup_dict['pitchers'].keys():
        pitcher_data = df[df['Name'] == pitcher_name]

        if not pitcher_data.empty:
            # stuff+ frame for each pitcher
            pitcher_stats_frame = ttk.LabelFrame(matchupframe, text=f"{pitcher_name} Stuff+ Stats")
            pitcher_stats_frame.pack(expand=True, fill="both", side="top", anchor="sw")

            # Add labels and stat value if it exists
            for header in column_headers_to_display:
                if header in pitcher_data.columns and not pitcher_data[header].isnull().values[0]:
                    stat_value = pitcher_data[header].values[0]
                    header_label = ttk.Label(pitcher_stats_frame, text=header)
                    header_label.pack(side="left", padx=5, pady=5)
                    stat_label = ttk.Label(pitcher_stats_frame, text=str(stat_value))
                    stat_label.pack(side="left", padx=5, pady=5)

if __name__ == "__main__":
    toplevel = tkinter.Tk()
    toplevel.title("ProbablePitchers")
    PPFrame = PPFrameT(master=toplevel)
    TOPLEVEL = PPFrame
    PPFrame.pack()
    toplevel.mainloop()




