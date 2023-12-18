import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import subprocess
import pandas as pd
import csv

 
class TkApp(tk.Frame):
    def __init__(self, master=None, title="NoTitle"):
        super().__init__(master)
        self.master.title(title)
        self.grid()

        # Dropdown menu
        self.stats_var = tk.StringVar()
        self.stats_var.set("Select Stats Type")
        self.stats_dropdown = ttk.Combobox(self.master, textvariable=self.stats_var,
                                           values=["Rebounding", "Passing"])
        self.stats_dropdown.grid(row=0, column=0, padx=10, pady=10)
        
        # Fetch button
        self.fetch_button = tk.Button(self.master, text="Fetch", command=self.fetch_stats)
        self.fetch_button.grid(row=0, column=1, padx=10, pady=10)

        # Textbox to display data
        self.data_textbox = ScrolledText(self.master)
        self.data_textbox.grid(row=1, column=1, padx=10, pady=10)

    # placeholder
    def fetch_stats(self):
        print("pretending to fetch stats")
        self.data_textbox.insert(tk.END, "legit stats:\n 1 2 3 4 5\n 69")
    
    # why is this even a member function?
    def fetch_stats_real(self):
        stats_type = self.stats_var.get()

        if stats_type == "Rebounding":
            subprocess.run(["bash", "AutoBoundFetch.bash"], check=True)
            csv_path = "file/path"
        elif stats_type == "Passing":
            subprocess.run(["bash", "AutoAssistFetch.bash"], check=True)
            csv_path = "file/path"
        else:  # invalid selection, somehow
            print("stats_var has an invalid state")
            return

        # Read CSV file and display in the textbox
        try:
            df = pd.read_csv(csv_path)
            self.data_textbox.delete(1.0, tk.END)
            self.data_textbox.insert(tk.END, df.to_string(index=False))
        except pd.errors.EmptyDataError:
            self.data_textbox.delete(1.0, tk.END)
            self.data_textbox.insert(tk.END, "No data available.")


if __name__ == "__main__":
    root = tk.Tk()
    NBAStatsApp = TkApp(root)
    NBAStatsApp.mainloop()
