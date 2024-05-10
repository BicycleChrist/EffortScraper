import update_importpaths

import tkinter
from tkinter import ttk

from DmFrame import *
from DmNotebook import *
import daily_lineups


LAYOUTMETHOD = tkinter.Widget.pack

class MLBFrameT(DmFrameT):
    def __init__(self, master):
        super().__init__(master)
        self.daily_lineups_data = None
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
            new_pane:ttk.PanedWindow = self.MatchupNB.AddTab(matchup_title, ttk.PanedWindow)
        
        # we add the callback here so it doesn't trigger when adding the tabs
        self.MatchupNB.AddCallback(self.Tabswitch_Callback)
        # delete the button
        self.DownloadButton.pack_forget()
    
    def Tabswitch_Callback(self, event):
        print("hi")


if __name__ == "__main__":
    toplevel = tkinter.Tk()
    TOPLEVEL = toplevel
    
    MLB_frame = MLBFrameT(master=toplevel)
    MLB_frame.pack()
    
    toplevel.mainloop()
