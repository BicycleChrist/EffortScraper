import tkinter
from tkinter import ttk
import ttkthemes
import os

class App(tkinter.Tk):
    def CreateTab(self, name):
        new_frame = ttk.Frame(self.tab_control)
        self.tab_control.add(new_frame, text=name)

        if name == "NHL":
            # Create two subframes within the NHL tab
            left_frame = ttk.Frame(new_frame)
            left_frame.pack(side="left", fill="both", expand=True)
            right_frame = ttk.Frame(new_frame)
            right_frame.pack(side="right", fill="both", expand=True)

            # Load and display rollingavg graphs for a given matchup
            #TODO Populate a dropdown menu in the NHL frame 
            # Maybe have it be a multifacted menu? might be better to just have two seperate dropdown's, time will tell
            # teams leading to all the graphs and static data, and matchups for the day being sourced via one of our scripts and the then displaying a default graph (10 game Xgf%) for each team
            team1_image_path = "/home/retupmoc/PycharmProjects/EffortScraper/NHLvacuum/nhlteamreports/WPG/generalTRdata/2024-04-15/rollingavggraphs/20232024-WPG-R10-xgpct-all.png"
            team2_image_path = "/home/retupmoc/PycharmProjects/EffortScraper/NHLvacuum/nhlteamreports/COL/generalTRdata/2024-04-15/rollingavggraphs/20232024-COL-R10-xgpct-all.png"

            if os.path.exists(team1_image_path):
                team1_image = tkinter.PhotoImage(file=team1_image_path)
                team1_label = ttk.Label(left_frame, image=team1_image)
                team1_label.image = team1_image  # Keep a reference to prevent garbage collection
                team1_label.pack()

            if os.path.exists(team2_image_path):
                team2_image = tkinter.PhotoImage(file=team2_image_path)
                team2_label = ttk.Label(right_frame, image=team2_image)
                team2_label.image = team2_image  # Keep a reference to prevent garbage collection
                team2_label.pack()
        else:
            self.CreateThemeButtons(new_frame)

        return

    def __init__(self):
        super().__init__()
        #self.title("Sports Betting App")
        self.geometry("800x600")
        #self.configure(bg="#FF0000")

        # Create the tab control
        self.tab_control = ttk.Notebook(self)

        # Configure tab styles
        self.style = ttkthemes.ThemedStyle()
        # ttk.Style().theme_names() lists available themes: ('clam', 'alt', 'default', 'classic')
        # got the additional theme pack to work with significantly more options, list of additional themes --> (https://wiki.tcl-lang.org/page/List+of+ttk+Themes)
        self.style.theme_use("alt")
        #self.style.configure("alt", background="#FF0000")
        #self.style.configure("TNotebook", background="#2c2c2c")
        #self.style.configure("TNotebook.Tab", background="#404040", foreground="white", font=("Helvetica", 12))
        #self.style.map("TNotebook.Tab", background=[("selected", "#666666")])
        self.title(self.style.theme_use())

        # Position the tab control
        self.tab_control.pack(expand=True, fill="both")

        # Create the tabs
        for sport in ("NBA", "NHL", "NFL", "MLB"):
            self.CreateTab(sport)

    def CreateThemeButtons(self, parent):
        themes = self.style.theme_names()

        # Create a frame to hold the buttons
        button_frame = ttk.Frame(parent, padding=20)
        button_frame.pack(fill="both", expand=True)

        for themename in themes:
            # setting default arguments is required for working around python jank
            def LambdaSetTheme(name=themename):
                self.title(name)
                self.style.theme_use(name)
                print(name)

            new_button = ttk.Button(button_frame, text=themename, command=LambdaSetTheme)
            new_button.pack(pady=2)

        return

if __name__ == "__main__":
    app = App()
    app.mainloop()
