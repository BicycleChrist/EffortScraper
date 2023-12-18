import tkinter as tk
import time
import csv
from tkinter import ttk
import subprocess
import pandas as pd
from tkinter import scrolledtext

class NBAStatsApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NBA Stats App")

        # Dropdown menu
        self.stats_var = tk.StringVar()
        self.stats_var.set("Select Stats Type")
        self.stats_dropdown = ttk.Combobox(root, textvariable=self.stats_var,
                                           values=["Rebounding", "Passing"])
        self.stats_dropdown.grid(row=0, column=0, padx=10, pady=10)

        # Fetch button
        self.fetch_button = tk.Button(root, text="Fetch", command=self.fetch_stats)
        self.fetch_button.grid(row=0, column=1, padx=10, pady=10)

        # Textbox to display data
        self.data_textbox = scrolledtext.ScrolledText(root, width=200, height=52)
        self.data_textbox.grid(row=1, column=3, columnspan=10, padx=30, pady=10)

    def fetch_stats(self):
        stats_type = self.stats_var.get()

        if stats_type == "Rebounding":
            subprocess.run(["bash", "AutoBoundFetch.bash"], check=True)
            csv_path = "file/path"
        elif stats_type == "Passing":
            subprocess.run(["bash", "AutoAssistFetch.bash"], check=True)
            csv_path = "file/path"
        else:
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
    app = NBAStatsApp(root)
    root.mainloop()
