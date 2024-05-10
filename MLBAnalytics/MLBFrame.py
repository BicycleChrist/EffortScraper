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
        self.daily_lineups_data = {}  # keys are the matchup titles
        self.DownloadButton = ttk.Button(master=self, text="download", command=self.DownloadButtonLambda)
        self.DownloadButton.pack()
        #self.MatchupSelectorText = tkinter.StringVar()
        #self.MatchupSelectorDropdown = ttk.OptionMenu(master=self, variable=self.MatchupSelectorText)
        self.MatchupDisplay, self.MatchupNB = InsertFrame(
            self, "MatchupDisplay", 
            DmNotebookT, self, tkinter.Widget.pack  
        )
    
    def DownloadButtonLambda(self):
        data = daily_lineups.Main()
        for matchupdict in data:
            matchup_title = matchupdict["Matchup"]
            new_tab = self.MatchupNB.AddTab(matchup_title)
            self.daily_lineups_data[matchup_title] = matchupdict
        
        # we add the callback here so it doesn't trigger when adding the tabs
        self.MatchupNB.AddCallback(self.Tabswitch_Callback)
        # triggers anyway, lol
        self.DownloadButton.pack_forget() # delete the button
    
    def Tabswitch_Callback(self, event):
        current_tab = self.MatchupNB.current_tab
        current_tab_name = current_tab["name"]
        if current_tab_name not in self.daily_lineups_data.keys(): return
        daily_lineup_data = self.daily_lineups_data[current_tab_name]
        if daily_lineup_data is None: return
        current_frame = self.MatchupNB.WidgetStorage[current_tab_name] 
        CreateTabLayout(current_frame, daily_lineup_data)


def CreateTabLayout(frame, data):
    title_text = tkinter.Text(frame)
    title_text.insert(tkinter.END, data["Matchup"])
    title_text.pack(expand=1, fill="x")
    team_text = tkinter.ScrolledText()



if __name__ == "__main__":
    toplevel = tkinter.Tk()
    TOPLEVEL = toplevel
    
    MLB_frame = MLBFrameT(master=toplevel)
    MLB_frame.pack()
    
    toplevel.mainloop()
