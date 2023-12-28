import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from pybaseball import statcast, batting_stats, pitching_stats, playerid_lookup, statcast_pitcher

from pybaseball import cache
cache.enable()

class SaberSuckPage(tk.Frame):
    def __init__(self, master=None):
        super().__init__(master)
        self.grid()

        # Dropdown menu for stat types
        self.stat_types = ["Statcast", "Pitching Stats", "Batting Stats"]
        self.stats_var = tk.StringVar()
        self.stats_var.set("Select Stats Type")
        self.stats_dropdown = ttk.Combobox(self, textvariable=self.stats_var,
                                           values=self.stat_types, state="readonly")
        self.stats_dropdown.grid(row=0, column=0, padx=2, pady=2)

        # Entry fields for parameters
        self.start_date_label = tk.Label(self, text="Start Date (YYYY-MM-DD):")
        self.start_date_label.grid(row=0, column=1, padx=2, pady=2)
        self.start_date_var = tk.StringVar()
        self.start_date_var.set("2023-08-15")  # Default start date
        self.start_date_entry = tk.Entry(self, textvariable=self.start_date_var)
        self.start_date_entry.grid(row=0, column=2, padx=2, pady=2)

        self.end_date_label = tk.Label(self, text="End Date (YYYY-MM-DD):")
        self.end_date_label.grid(row=0, column=3, padx=2, pady=2)
        self.end_date_var = tk.StringVar()
        self.end_date_var.set("2023-08-17")  # Default start date
        self.end_date_entry = tk.Entry(self, textvariable=self.end_date_var)
        self.end_date_entry.grid(row=0, column=4, padx=10, pady=10)


        # Entry field for player search
        self.player_search_label = tk.Label(self, text="Player Search:")
        self.player_search_label.grid(row=0, column=5, padx=2, pady=2)
        self.player_search_entry = tk.Entry(self)
        self.player_search_entry.grid(row=0, column=6, padx=2, pady=2)

        # Empty Textbox
        self.data_textbox = tk.Text(self, width=210, height=42)
        self.data_textbox.grid(row=1, column=0, columnspan=7, padx=10, pady=10)

        # Button to trigger data retrieval
        self.retrieve_button = tk.Button(self, text="Retrieve Stats", command=self.update_stats)
        self.retrieve_button.grid(row=0, column=11, columnspan=2, pady=1)

        # Button to trigger player lookup
        self.player_lookup_button = tk.Button(self, text="Lookup Player", command=self.lookup_player)
        self.player_lookup_button.grid(row=0, column=10, pady=10)

    def update_stats(self):
        selected_option = self.stats_var.get()

        # Validate date input
        start_date = self.start_date_var.get()
        end_date = self.end_date_var.get()

        if selected_option not in self.stat_types or not start_date or not end_date:
            messagebox.showerror("Error", "Please select a valid stats type and enter start and end dates.")
            return

        try:
            if selected_option == "Statcast":
                data = statcast(start_dt=start_date, end_dt=end_date)
            elif selected_option == "Pitching Stats":
                player_name = self.player_name_entry.get()
                data = pitching_stats(player_name, start_date, end_date)
            elif selected_option == "Batting Stats":
                player_name = self.player_name_entry.get()
                data = batting_stats(player_name, start_date, end_date)

            # Display the result in the Textbox
            self.data_textbox.delete(1.0, tk.END)
            self.display_data_in_textbox(data)
        except Exception as e:
            messagebox.showerror("Error", f"Error retrieving stats: {str(e)}")

    def display_data_in_textbox(self, data):
        # Iterate through rows and columns and insert each element into the Textbox
        for index, row in data.iterrows():
            self.data_textbox.insert(tk.END, f"Index: {index}\n")
            for column, value in row.items():
                self.data_textbox.insert(tk.END, f"{column}: {value}\n")
            self.data_textbox.insert(tk.END, "\n")

    def lookup_player(self):
        player_name = self.player_search_entry.get()

        if not player_name:
            player_name = "clayton kershaw"
            #messagebox.showerror("Error", "Please enter a player name for lookup.")
            #return

        try:
            player_info = playerid_lookup(player_name.split(' ')[1], player_name.split(' ')[0])
            start_date = self.start_date_var.get()
            end_date = self.end_date_var.get()
            newdata = statcast_pitcher(start_date, end_date, player_info['key_mlbam'].array[0])
            self.display_data_in_textbox(newdata)
            #messagebox.showinfo("Player Lookup", f"Player ID: {player_info}")
        except Exception as e:
            messagebox.showerror("Error", f"Error looking up player: {str(e)}")

# If this file is run directly, create a standalone instance for testing
if __name__ == "__main__":
    root = tk.Tk()
    root.title("sabersuck")
    sabersuck_page = SaberSuckPage(root)
    sabersuck_page.mainloop()
