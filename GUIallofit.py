import tkinter
from tkinter import ttk
from sabersuckframe import SaberSuckPage  # Import the new class
import statsuckGUI
from CHN.CHNscrape import CHN_TeamIDs

class TabbedGUI(ttk.Notebook):
    WidgetStorage = {}
    def __init__(self, master=None, title="Statsuck"):
        super().__init__(master)
        self.master.title(title)
        self.pack(expand=1, fill="both")  # Use pack instead of grid

    def AddTab(self, name, new_widget_class):
        new_widget = new_widget_class(self)
        self.WidgetStorage[name] = new_widget
        self.add(new_widget, text=name)
        return new_widget

if __name__ == "__main__":
    root = tkinter.Tk()
    mainapp = TabbedGUI(root)

    mainapp.AddTab("NBAtab", statsuckGUI.TkApp)
    mainapp.AddTab("MLBtab", SaberSuckPage)
    chnframe = mainapp.AddTab("CHN", tkinter.Text)
    chnframe.insert(tkinter.END, "epic placeholder text!")
    mainapp.WidgetStorage["CHN"].insert(tkinter.END, "test2")

    root.mainloop()
