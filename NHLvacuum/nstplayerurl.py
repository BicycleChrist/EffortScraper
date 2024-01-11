from nst_player_ids import NST_playerids

baseurl = "https://www.naturalstattrick.com/playerreport.php?"

stype = {  # season-type
    "Pre-Season": "1",
    "Regular Season": "2",
    "RTP Exhibition": "3",  # unconfirmed
    "Playoffs": "4",  # unconfirmed
}

sit = {  # situation
    "All Strength": "placeholder",
    "Even Strength": "ev",
    "Power Play": "pp",
    "5 on 4 PP": "5v4",
    "5v5": "5v5",
    "Penalty Kill": "pk",
    "4 on 5 PK": "4v5",
    "3 on 3": "3v3",
    "Against empty net": "ena",
    "With empty net": "enf"
}

stdoi = {  # ?
    "On Ice": "oi",
    "Individual": "N/A"
}

rate = {
    "Counts": "c",  # unconfirmed
    "Rates": "y",
    "Relative": "r",
}

v_section = {
    "Player Summary": "p",
    "Scoring": "s",
    "Teammates": "t",
    "Opposition": "o",
    "Game Log": "g",
}


def Build_NST_URL(playerid, l_stype, l_sit, l_stdoi, l_rate, l_v, fromseason="20232024", thruseason="20232024"):
    #newurl = f"{baseurl}{stype[l_stype]}&{sit[l_sit]}&{stdoi[l_stdoi]}&{rate[l_rate]}&{v_section[l_v]}&{NST_playerids[playerid]}"
    newurl = f"{baseurl}stype={stype[l_stype]}&sit={sit[l_sit]}&stdoi={stdoi[l_stdoi]}&rate={rate[l_rate]}&v={v_section[l_v]}&playerid={NST_playerids[playerid]}"
    return newurl




import tkinter as tk
from tkinter import ttk
window = tk.Tk()
window.title("NST URL Builder")

currentrow = 0
def CreateDropdown(name, values):
    newvar = tk.StringVar(window)
    newdropdown = ttk.Combobox(window, textvariable=newvar, values=values)
    newdropdown.set(name)
    global currentrow
    currentrow = currentrow + 1
    newdropdown.grid(column=0, row=currentrow)
    return newvar


if __name__ == "__main__":
    # Create dropdown menus
    Dropdowns = [
        (CreateDropdown("playerid", values=list(NST_playerids.keys())),
            lambda x: NST_playerids[x.get()]),
        (CreateDropdown("stype", values=list(stype.keys())),
            lambda x: stype[x.get()]),
        (CreateDropdown("sit", values=list(sit.keys())),
            lambda x: sit[x.get()]),
        (CreateDropdown("stdoi", values=list(stdoi.keys())),
            lambda x: stdoi[x.get()]),
        (CreateDropdown("rate", values=list(rate.keys())),
            lambda x: rate[x.get()]),
        (CreateDropdown("v_section", values=list(v_section.keys())),
            lambda x: v_section[x.get()]),
    ]

    # Function to get the current selection from all dropdowns
    def get_selections():
        selected_values = [var.get() for var, _ in Dropdowns]
        url = Build_NST_URL(*selected_values)  # Pass the selected values
        print(url)


    btn_get_selections = tk.Button(window, text="Get URL", command=get_selections)
    btn_get_selections.grid(column=0, row=0)

    # Start the Tkinter event loop
    window.mainloop()
