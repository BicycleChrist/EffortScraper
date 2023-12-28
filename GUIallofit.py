import tkinter as tk
import tkinter.filedialog
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from tkinter.filedialog import FileDialog
from sabersuckframe import SaberSuckPage  # Import the new class
import subprocess
import pandas as pd
import csv
import pathlib

import pprint
import json

import statsuckGUI
import sabersuckframe

class TkApp(ttk.Notebook):
    def __init__(self, master=None, title="Statsuck"):
        super().__init__(master)
        self.master.title(title)
        self.pack()

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from sabersuckframe import SaberSuckPage
import subprocess
import pandas as pd
import csv
import pathlib

import pprint
import json

class TkApp(tk.Tk):  # Change to subclass of tk.Tk
    def __init__(self, master=None, title="Statsuck"):
        super().__init__(master)
        self.title(title)

if __name__ == "__main__":
    root = TkApp()
    mainapp = ttk.Notebook(root)

    tab0 = statsuckGUI.TkApp(mainapp)
    mainapp.add(tab0, text="NBAtab")

    tab1 = sabersuckframe.SaberSuckPage(mainapp)
    mainapp.add(tab1, text="MLBtab")

    mainapp.pack(expand=1, fill="both")  # Use pack instead of grid

    root.mainloop()
