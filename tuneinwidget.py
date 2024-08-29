import tkinter as tk
from tkinter import ttk
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import tunein 


#  parse URLs and update the listbox
def parse_urls(urls, listbox):
    max_length = 0
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = [executor.submit(tunein.get_href_links, url) for url in urls]
        for future in concurrent.futures.as_completed(futures):
            href_links = future.result()
            for link in href_links:
                listbox.insert(tk.END, link)
                if len(link) > max_length:
                    max_length = len(link)
    
    # change width based on the longest URL
    listbox.config(width=max_length)


root = tk.Tk()
root.title("Games list")


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






