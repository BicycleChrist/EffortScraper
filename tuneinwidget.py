import tkinter as tk
from tkinter import ttk
import threading
import tunein 

#  parse URLs and update the listbox
def parse_urls(urls, listbox):
    def run():
        max_length = 0
        for url in urls:
            href_links = tunein.get_href_links(url)
            for link in href_links:
                listbox.insert(tk.END, link)
                if len(link) > max_length:
                    max_length = len(link)
        
        # change width based on the longest URL
        listbox.config(width=max_length)
    
    threading.Thread(target=run).start()


root = tk.Tk()
root.title("Listbox with Scrollbar Example")


frame = ttk.Frame(root)
frame.grid(row=0, column=0, sticky="nsew")

# vertical scrollbar 
v_scrollbar = ttk.Scrollbar(frame, orient="vertical")
v_scrollbar.grid(row=0, column=1, sticky="ns")


listbox = tk.Listbox(frame, yscrollcommand=v_scrollbar.set)
listbox.grid(row=0, column=0, sticky="nsew")

# configure the scrollbar to work with the listbox
v_scrollbar.config(command=listbox.yview)

# URLs to be parsed
urls = [
    "https://the.streameast.app/nhlstreams3",
    "https://the.streameast.app/mlbstreams17",
    "https://the.streameast.app/nbastreams64",
    "https://the.streameast.app/nflstreams3",
    "https://buffstreams.ai/mlb",
    "https://buffstreams.ai/nba",
    "https://buffstreams.ai/nhl"
    "https://methstreams.com/mlb-streams/live1/"
]

# populate the listbox
parse_urls(urls, listbox)


root.mainloop()






