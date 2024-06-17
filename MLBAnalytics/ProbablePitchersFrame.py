import update_importpaths

import tkinter
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from DmFrame import *
from DmNotebook import *
from ProbablePitchers import *


LAYOUTMETHOD = tkinter.Widget.pack

class PPFrameT(DmFrameT):
    def __init__(self, master):
        super().__init__(master)
        self.probable_pitchers_data = []
        self.DownloadButton = ttk.Button(master=self, text="download", command=self.DownloadButtonLambda)
        self.DownloadButton.pack()
        self.DownloadButtonHook = None  # define this to override the default 'CreateTabLayout' at the end of DownloadButtonLambda
        #self.MatchupSelectorText = tkinter.StringVar()
        #self.MatchupSelectorDropdown = ttk.OptionMenu(master=self, variable=self.MatchupSelectorText)
        self.MatchupDisplay, self.MatchupNB = InsertFrame(
            self, "MatchupDisplay", 
            DmNotebookT, new_toplevel=self, new_layoutmethod=tkinter.Widget.pack  
        )
    
    def DownloadButtonLambda(self):
        soup = FetchProbablePitchers()
        if not soup: print("error: no soup"); return;
        self.probable_pitchers_data = ParseProbablePitchers(soup)
        for matchup_dict in self.probable_pitchers_data['matchups']:
            new_tab = self.MatchupNB.AddTab(matchup_dict['title'])
            matchup_frame = ttk.LabelFrame(new_tab, text=matchup_dict['title'])
            matchup_frame.pack(expand=True, fill="both", side="top", anchor="n")
            if not self.DownloadButtonHook:
                CreateTabLayout(matchup_frame, matchup_dict)
            else:
                self.DownloadButtonHook(matchup_frame, matchup_dict)
        self.DownloadButton.pack_forget() # delete the button


def CreateTabLayout(matchupframe, matchup_dict):
    for pitcher_name, pitcher_dict in list(matchup_dict['pitchers'].items()):
        pitcher_frame = ttk.LabelFrame(matchupframe, text=pitcher_name)
        pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")
        for key, value in pitcher_dict.items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")
    for teamside, teamdict in list(matchup_dict['teams'].items()):
        team_frame = ttk.LabelFrame(matchupframe, text=f"{teamdict['name']} - {teamside}")
        team_frame.pack(expand=True, fill="both", side="top", anchor="sw")
        for key, value in teamdict.items():
            textbox = ttk.Label(master=team_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")
    return


if __name__ == "__main__":
    toplevel = tkinter.Tk()
    toplevel.title("ProbablePitchers") 
    PPFrame = PPFrameT(master=toplevel)
    TOPLEVEL = PPFrame
    PPFrame.pack()
    toplevel.mainloop()
