import update_importpaths

import tkinter
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from DmFrame import *
from DmNotebook import *
import daily_lineups


LAYOUTMETHOD = tkinter.Widget.pack

class MLBFrameT(DmFrameT):
    def __init__(self, master):
        super().__init__(master)
        self.daily_lineups_data = []
        self.DownloadButton = ttk.Button(master=self, text="download", command=self.DownloadButtonLambda)
        self.DownloadButton.pack()
        #self.MatchupSelectorText = tkinter.StringVar()
        #self.MatchupSelectorDropdown = ttk.OptionMenu(master=self, variable=self.MatchupSelectorText)
        self.MatchupDisplay, self.MatchupNB = InsertFrame(
            self, "MatchupDisplay", 
            DmNotebookT, self, tkinter.Widget.pack  
        )
    
    def DownloadButtonLambda(self):
        self.daily_lineups_data = daily_lineups.Main()
        for matchupdict in self.daily_lineups_data:
            matchup_title = matchupdict["Matchup"]
            new_tab = self.MatchupNB.AddTab(matchup_title)
            CreateTabLayout(new_tab, matchupdict)
        self.DownloadButton.pack_forget() # delete the button


def CreateTabLayout(frame, data):
    #title_text = tkinter.Text(frame)
    #title_text.insert(tkinter.END, data["Matchup"])
    #title_text.pack(expand=False)
    teamlist_frame = InsertFrame(frame, "Teamlists")
    home_team = False  # away_team always listed first
    for team_name, playerdatalist in data["Team_Lineups"].items():
        label_team = "Away Team: "
        if home_team: label_team = "Home Team: "
        else: home_team = True
        labelframe = tkinter.LabelFrame(teamlist_frame, text=label_team + team_name)
        labelframe.pack(expand=True, fill="y", side="left")
        textbox = ScrolledText(labelframe)
        textbox.pack(expand=True, fill="both")
        for playertext in playerdatalist:
            textbox.insert(tkinter.END, playertext)
            textbox.insert(tkinter.END, '\n')
    for segment in ("Umpire", "Weather"):
        new_frame, new_widget = InsertFrame(frame, segment, tkinter.Text)
        new_widget.insert(tkinter.END, segment + ': ' + data[segment])
        new_frame.pack(expand=False, fill="x", side="bottom")        
        new_widget.pack(expand=False, fill="x", side="bottom")
    return


if __name__ == "__main__":
    toplevel = tkinter.Tk(sync=True)
    MLB_frame = MLBFrameT(master=toplevel)
    TOPLEVEL = MLB_frame
    MLB_frame.pack()
    toplevel.mainloop()
