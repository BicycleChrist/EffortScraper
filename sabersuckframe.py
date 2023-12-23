#link to the scraper:https://github.com/spilchen/baseball_scraper
import tkinter as tk
from tkinter import ttk
from baseball_scraper import *
class SaberSuckPage(tk.Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.grid()

        # Dropdown menu
        self.stats_var = tk.StringVar()
        self.stats_var.set("Select Stats Type")
        self.stats_dropdown = ttk.Combobox(self, textvariable=self.stats_var,
                                           values=["Option 1", "Option 2"])
        self.stats_dropdown.grid(row=0, column=0, padx=10, pady=10)

        # Empty Textbox
        self.data_textbox = tk.Text(self, width=50, height=10)
        self.data_textbox.grid(row=1, column=0, padx=10, pady=10)

    def update_stats(self):
        selected_option = self.stats_var.get()
        # Add logic to update the Textbox based on the selected option
        # For now, let's clear the Textbox
        self.data_textbox.delete(1.0, tk.END)
        self.data_textbox.insert(tk.END, "Selected Option: {}".format(selected_option))

# If this file is run directly, create a standalone instance for testing
if __name__ == "__main__":
    root = tk.Tk()
    sabersuck_page = SaberSuckPage(root)
    sabersuck_page.mainloop()

