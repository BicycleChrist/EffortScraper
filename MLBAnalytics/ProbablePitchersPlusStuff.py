# This file is a version of 'ProbablePitchersPlusStuff' with more stuff jammed in
import update_importpaths

import pathlib
import pandas
import tkinter
from tkinter import ttk
from concurrent.futures import ThreadPoolExecutor, as_completed
import more_itertools  # flatten

from DmFrame import InsertFrame
from ProbablePitchersFrame import PPFrameT
from ProbablePitchers import *
from DmNotebook import DmNotebookT
from stuffsuck import get_pitching_data
import BBSplayer_ids
import BBSavant_statcast
import penski


#TODO: Allow mouse wheel scrolling when not hovering over scrollbar


# used to color-code values in the GUI
def GetTextColor(value: int) -> str:
    if value == '': return "#000000"
    try: value = int(value)
    except ValueError: print(f"[GetTextColor] ERROR: Int conversion failed: {value}"); return "#000000"
    if value >= 80:       return "#FF0000"
    elif value <= 50:     return "#0000FF"
    elif 50 < value < 80: return "#027C5E"
    return "#000000"

# need to store images here to keep them from unloading
GLOBAL_IMAGE_STORAGE = []

# TODO: create a subdirectory for these images
def LoadPitcherImages(pitchername, parentframe, quiet:bool = False):
    base_path = pathlib.Path.cwd() / "MLBstats"
    if not base_path.exists(): base_path.mkdir()

    new_frame, new_label = InsertFrame(
        parentframe, f"{pitchername}_images",
        new_widget_class=ttk.Labelframe, text=f"{pitchername}_images", border=2
    )
    loaded_images = []

    # get rid of non-ascii before constructing filenames
    unformatted_pitchername = pitchername.replace('_', ', ')
    if unformatted_pitchername in BBSplayer_ids.Nonascii_Remap.keys():
        new_unformatted_pitchername = BBSplayer_ids.Nonascii_Remap.get(unformatted_pitchername, pitchername)
        pitchername = new_unformatted_pitchername.replace(', ', '_')
    for thing in ("trending_div", "pitch_distribution"):
        image_path = base_path / f"{pitchername}_{thing}.png"

        if not image_path.exists():
            if not quiet: print(f"[LoadPitcherImages] Image not found: {image_path}")
            continue

        loadedimage = tkinter.PhotoImage(master=new_label, name=f"{image_path.stem}", file=image_path)
        loaded_images.append(loadedimage)
        # 'compound="top"' here causes the label to be displayed with the image (otherwise no text)
        image_label = ttk.Label(master=new_label, text=f"{pitchername}_{thing}", image=f"{image_path.stem}", compound="bottom")
        image_label.pack(expand=True, side="left")

    GLOBAL_IMAGE_STORAGE.extend(loaded_images)
    return loaded_images


def FilloutStartingPitchers(matchupframe, matchup_dict, dataframe, statcast_data):
    starting_pitcher_names = list(matchup_dict['pitchers'].keys())
    print(starting_pitcher_names)

    pitcher_id_map = {}
    for name in starting_pitcher_names:
        if name == 'TBD':
            pitcher_id_map[name] = None
            continue

        try:
            newname, pitcherid = BBSplayer_ids.LookupPlayerID(name, reverseOrder=True)
            pitcher_id_map[newname] = pitcherid
        except KeyError as e:
            print(f"KeyError: {e} - Pitcher name {name} not found in the dictionary. Skipping this pitcher.")
            continue

    filtered_df = dataframe[dataframe['Name'].isin(starting_pitcher_names)]
    pitcher_data_map = {row['Name']: row for index, row in filtered_df.iterrows()}

    column_headers_to_display = [
        'Stf+ FA', 'Stf+ SI', 'Stf+ FC', 'Stf+ FS',
        'Stf+ SL', 'Stf+ CU', 'Stf+ CH', 'Stf+ KC', 'Stf+ FO',
        'Stuff+', 'Location+', 'Pitching+'
    ]

    for reversed_pitcher_name, pitcher_data in matchup_dict["pitchers"].items():
        pitcher_frame = ttk.LabelFrame(matchupframe, text=reversed_pitcher_name)
        pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")

        # unreversed
        spaced_pitcher_name = " ".join(reversed_pitcher_name.split(", ")[::-1])
        for key, value in matchup_dict['pitchers'].get(spaced_pitcher_name, {}).items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")

        pitcher_stats_frame = ttk.LabelFrame(matchupframe, text=f"{spaced_pitcher_name} Stuff+ Stats")
        pitcher_stats_frame.pack(expand=True, fill="both", side="top", anchor="sw")

        stats_frame = tkinter.Frame(pitcher_stats_frame)
        stats_frame.pack(side="top", fill="x")
        for header in column_headers_to_display:
            if header in pitcher_data_map.get(spaced_pitcher_name, {}) and not pandas.isnull(pitcher_data_map[spaced_pitcher_name][header]):
                stat_value = pitcher_data_map[spaced_pitcher_name][header]
                stat_label = ttk.Label(stats_frame, text=f"{header}: {stat_value}")
                stat_label.pack(side="left", anchor="w")

        images_frame = tkinter.Frame(pitcher_stats_frame)
        images_frame.pack(side="top", fill="x")

        pitchername_reformatted = "_".join(reversed(spaced_pitcher_name.split(" ")))
        LoadPitcherImages(pitchername_reformatted, images_frame)

        scraped_data_frame = ttk.LabelFrame(matchupframe, text=f"{spaced_pitcher_name} Statcast Stats")
        scraped_data_frame.pack(expand=True, fill="both", side="top")

        unreversed_pitcher_name_with_comma = ", ".join(reversed(reversed_pitcher_name.split(" ")))
        if not unreversed_pitcher_name_with_comma in statcast_data.keys():
            print(f"no statcast data for {unreversed_pitcher_name_with_comma}")
            continue

        statcast_dict = statcast_data[unreversed_pitcher_name_with_comma]

        # statcast stuff
        for key, value in statcast_dict.items():
            ttk.Label(scraped_data_frame, text=f"{key}:", font=('Helvetica', 10, 'bold')).pack(anchor="w", padx=5, pady=2)
            ttk.Label(scraped_data_frame, text=f"  % Ranking: {value['value']}", foreground=GetTextColor(value['value']), font=('Helvetica', 10)).pack(anchor="w", padx=5, pady=2)
            ttk.Label(scraped_data_frame, text=f"  Stat: {value['stat']}", font=('Helvetica', 10)).pack(anchor="w", padx=5, pady=2)

    # Handle 'TBD' pitchers
    for name in starting_pitcher_names:
        if name == 'TBD':
            pitcher_frame = ttk.LabelFrame(matchupframe, text='TBD')
            pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")

            textbox = ttk.Label(master=pitcher_frame, text="The starter has not been decided yet.", font=('Helvetica', 25))
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")

    return


def Fillout_BP_Frame(parent_frame, possible_files: dict):
    BP_dicts = []  # list of dicts; maps BPstat_type to a dataframe (loaded from csv)
    for BPstat_type, all_matching_files in possible_files.items():
        BPdata_dict = {BPstat_type: pandas.read_csv(file) for file in all_matching_files}
        BP_dicts.append(BPdata_dict)

    # Create a parent frame for the adv_traits and splits_stats frames
    adv_splits_parent_frame = tkinter.Frame(parent_frame)
    adv_splits_parent_frame.pack(expand=True, fill="both", anchor="w", side="top")

    for BP_dict in BP_dicts:
        for BPstat_type, dataframe in BP_dict.items():
            dataframe.columns = dataframe.columns.str.strip()
            BPstat_frame = ttk.LabelFrame(adv_splits_parent_frame, text=BPstat_type)
            BPstat_frame.pack(expand=True, fill="both", side="left")  # Pack side by side

            treeview = ttk.Treeview(BPstat_frame, columns=list(dataframe.columns), show='headings')
            treeview.pack(expand=True, fill="both", side="left")

            for col in dataframe.columns:
                treeview.heading(col, text=col)
                treeview.column(col, anchor='center', width=50, stretch=tkinter.NO)

            for column_name in ("Trait", "Season"):
                if column_name in dataframe.columns:
                    if column_name == "Trait":
                        treeview.column(column_name, anchor='center', width=175, stretch=tkinter.NO)
                    elif column_name == "Season":
                        treeview.column(column_name, anchor='center', width=75, stretch=tkinter.NO)

            for index, row in dataframe.iterrows():
                values = [row[col] for col in dataframe.columns]
                treeview.insert("", "end", values=values)

    return


def CreateTabLayoutCustom(matchupframe, matchup_dict):
    bullpen_dir = penski.GetFilepath('bullpen_stats', '').parent
    bullpen_files = bullpen_dir.glob("*bullpen_stats*.csv")

    def GetTeamname(filepath: pathlib.Path):
        return filepath.name.split("_Bullpen", maxsplit=1)[0]

    bullpen_dict = {GetTeamname(bullpen_file):bullpen_file for bullpen_file in bullpen_files}

    for side in ['home', 'away']:
        team_name = matchup_dict['teams'][side]['name']
        team_name = team_name.strip().replace(' ', '_')
        if team_name == "D-backs":
            team_name = "Diamondbacks"

        bullpen_data = None
        # the teamnames in the matchup_dict aren't the real names, so we have to search
        keys = list(bullpen_dict.keys())
        for real_name in keys:
            if team_name in real_name:
                filepath = bullpen_dict[real_name]
                bullpen_data = pandas.read_csv(filepath)
                break

        if bullpen_data is not None:
            bullpen_frame = InsertFrame(master=matchupframe, new_widget_class=None)
            bullpen_frame.pack(expand=True, fill="x", side="top", anchor="nw")
            bullpen_subframe = InsertFrame(bullpen_frame, new_toplevel=bullpen_frame, new_widget_class=None)
            bullpen_subframe.pack(expand=True, fill="y", side="top", anchor="nw")

            column_names = list(bullpen_data.columns)
            pitcher_names = list(bullpen_data.Player)
            default_text = tkinter.StringVar()
            default_text.set("Default Text")

            def DropdownCallback(selection, stringvar=default_text, target_frame=bullpen_subframe):
                stringvar.set(selection)
                formatted_name = selection.strip().replace(' ', '_')
                possible_files = {
                    BPstat_type: penski.GetFilepath(BPstat_type, formatted_name, append_date=False)
                    for BPstat_type in ('adv_traits', 'splits_stats')
                }
                for widget in target_frame.winfo_children():
                    widget.pack_forget()
                Fillout_BP_Frame(target_frame, possible_files)

                # Load images
                firstname, lastname = selection.split()
                reformatted_name = f"{lastname}_{firstname}"
                try:
                    LoadPitcherImages(reformatted_name, target_frame)
                except FileNotFoundError:
                    # Scrape images if not found
                    print(f"Images for {reformatted_name} not found. Attempting to scrape...")
                    try:
                        # Lookup pitcher ID
                        pitcher_key, pitcher_id = BBSplayer_ids.LookupPlayerID(selection, reverseOrder=False)
                        # Perform the scrape
                        BBSavant_statcast.Scrape(selection, pitcher_id, True)
                    except Exception as e:
                        print(f"Error scraping images for {reformatted_name}: {e}")
                return

            dropdown = ttk.OptionMenu(bullpen_frame, default_text, f"{team_name} Adv BP stats", *pitcher_names, command=DropdownCallback)
            dropdown.pack(expand=False, fill="none", anchor="center")

            bullpen_treeview = ttk.Treeview(matchupframe, columns=column_names, show='headings')
            for col in column_names:
                bullpen_treeview.heading(col, text=col)
                bullpen_treeview.column(col, anchor='center', width=75, stretch=tkinter.NO) # Stretch needs to used with width !!!

            bullpen_treeview.pack(expand=False, fill="both")
            bullpen_treeview.column("Player", width=130)

            for index, row in bullpen_data.iterrows():
                values = [row[col] for col in column_names]
                bullpen_treeview.insert("", "end", values=values)

    return


# expects pitcherData from ProbablePitchers.ScrapePitcherData
def GetStatcastPitcherData(pitcherData:dict) -> dict:
    print("GetStatcastPitcherData")
    all_pitcher_names = [matchup["pitchers"].keys() for matchup in pitcherData["matchups"]]
    all_pitcher_names = [*more_itertools.flatten(all_pitcher_names)]
    # matchup-keys are pairs of names, so we need to flatten the list

    formatted_pitcher_id_map = {
        newpitchername : playerID
        for newpitchername, playerID in
        map(lambda pn: BBSplayer_ids.LookupPlayerID(pn, reverseOrder=True), all_pitcher_names)
        if playerID is not None
    }

    #single-threaded version
    # pitcher_data_results = {
    #     pitcher_name: BBSavant_statcast.Scrape(pitcher_name, player_id, take_screenshots=False)
    #     for pitcher_name, player_id in formatted_pitcher_id_map.items() if player_id is not None
    # }

    # look into using 'add_done_callback()' for ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = {
          pitcher_name: executor.submit(BBSavant_statcast.Scrape, pitcher_name, player_id, take_screenshots=False)
          for pitcher_name, player_id in formatted_pitcher_id_map.items() if player_id is not None
        }
        pitcher_data_results = {
          pitcher_name: future.result() for pitcher_name, future in futures.items()
          #for future, pitcher_name in as_completed(futures.values())  # as_completed not necessary?
        }

    return pitcher_data_results


def Main():
    pitcher_data = ScrapeAllPitcherData()
    statcast_data = GetStatcastPitcherData(pitcher_data)

    toplevel = tkinter.Tk()
    toplevel.title("ProbablePitchers")

    frame, canvas = InsertFrame(toplevel, new_widget_class=tkinter.Canvas, new_layoutmethod=tkinter.Widget.pack)
    scrollbar = ttk.Scrollbar(toplevel, orient=tkinter.VERTICAL, command=canvas.yview)
    frame.pack(fill=tkinter.BOTH, expand=True, side=tkinter.LEFT)
    canvas.pack(fill=tkinter.BOTH, expand=True)
    scrollbar.pack(side=tkinter.RIGHT, fill=tkinter.Y)

    PPFrame = PPFrameT(master=frame)
    PPFrame.pack(expand=False, side="left")

    # first arg is a coordinate. 'window' must NOT be a toplevel window!
    # https://tkinter-docs.readthedocs.io/en/latest/widgets/canvas.html

    def scroll_event(event):
        canvas.configure(scrollregion=canvas.bbox("all"))

    canvas.configure(yscrollcommand=scrollbar.set)
    toplevel.bind("<Configure>", scroll_event, add='+')

    #PPFrame.bind("<Configure>", scroll_event, add='+')
    canvas.create_window((0, 0), window=PPFrame, anchor="nw")

    def mousewheel_callback(event):
        scroll_delta = 5  # the (linux-specific) button-events always have a delta of 0, so we hardcode it
        if event.delta != 0: scroll_delta = event.delta  # handling actual mousewheel event
        if event.num == 4: scroll_delta *= -1  # button-4 == scroll-down
        canvas.yview_scroll(scroll_delta, "units")

    toplevel.bind("<MouseWheel>", mousewheel_callback)
    # Linux maps mouse-wheel scrollevents to these buttons, for some reason
    toplevel.bind("<Button-4>",   mousewheel_callback)
    toplevel.bind("<Button-5>",   mousewheel_callback)

    dataframe = get_pitching_data()  # DON'T CALL THIS INLINE IN THE LAMBDA!!!!!! It will re-download EVERY ITERATION!

    def CreateTabLayoutLambda(matchupframe, matchupdict, dataframe, statcast_data):
        CreateTabLayoutCustom(matchupframe, matchupdict)
        FilloutStartingPitchers(matchupframe, matchupdict, dataframe, statcast_data)

    PPFrame.DownloadButtonHook = lambda a, b: (
        CreateTabLayoutLambda(a, b, dataframe, statcast_data)
    )

    toplevel.mainloop()
    return pitcher_data, statcast_data, dataframe


if __name__ == "__main__":
    Main()
