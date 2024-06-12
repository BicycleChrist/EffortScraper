import update_importpaths
from DmFrame import InsertFrame
import pandas as pd
import tkinter
from tkinter import *
from tkinter import ttk
from ProbablePitchersFrame import PPFrameT
from ProbablePitchers import *
from DmNotebook import DmNotebookT
from stuffsuck import get_pitching_data
import BBSplayer_ids
import penski
import pathlib
from PIL import Image, ImageTk
import BBSavant_statcast
from BBSavant_statcast import scrape
from concurrent.futures import ThreadPoolExecutor, as_completed

#TODO: Allow mouse wheel scrolling when not hovering over scrollbar
#TODO: Boost scraping speed
#BBS scrape takes all damn day, must be miffing it via iterating through too much stuff or jenk threading implementation


def FilloutStartingPitchers(matchupframe, matchup_dict, dataframe):
    starting_pitcher_names = list(matchup_dict['pitchers'].keys())
    print(starting_pitcher_names)

    pitcher_id_map = {}
    for name in starting_pitcher_names:
        if name == 'TBD':
            pitcher_id_map[name] = None
            continue

        try:
            newname, pitcherid = BBSplayer_ids.LookupPitcher(name, reverseOrder=True)
            pitcher_id_map[newname] = pitcherid
        except KeyError as e:
            print(f"KeyError: {e} - Pitcher name {name} not found in the dictionary. Skipping this pitcher.")
            continue

    pitcher_data_results = {}
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = {
            executor.submit(BBSavant_statcast.scrape, pitchername, player_id, True): pitchername
            for pitchername, player_id in pitcher_id_map.items() if player_id is not None
        }
        pitcher_data_results = {
            pitchername: future.result()
            for future, pitchername in futures.items()
        }

    filtered_df = dataframe[dataframe['Name'].isin(starting_pitcher_names)]
    pitcher_data_map = {row['Name']: row for index, row in filtered_df.iterrows()}

    column_headers_to_display = [
        'Stf+ FA', 'Stf+ SI', 'Stf+ FC', 'Stf+ FS',
        'Stf+ SL', 'Stf+ CU', 'Stf+ CH', 'Stf+ KC', 'Stf+ FO',
        'Stuff+', 'Location+', 'Pitching+'
    ]

    def get_color(value):
        try:
            value = int(value)
            if value >= 80:
                return "#FF0000"
            elif value <= 50:
                return "#0000FF"
            elif 50 < value < 80:
                return "#027C5E"
        except ValueError:
            return "#000000"

    for reversed_pitcher_name, pitcher_data in pitcher_data_results.items():
        pitcher_name = "_".join(reversed_pitcher_name.split(", ")[::-1])
        pitcher_frame = ttk.LabelFrame(matchupframe, text=pitcher_name)
        pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")

        spaced_pitcher_name = " ".join(reversed_pitcher_name.split(", ")[::-1])
        for key, value in matchup_dict['pitchers'].get(spaced_pitcher_name, {}).items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")

        pitcher_stats_frame = ttk.LabelFrame(matchupframe, text=f"{pitcher_name} Stuff+ Stats")
        pitcher_stats_frame.pack(expand=True, fill="both", side="top", anchor="sw")

        stats_frame = Frame(pitcher_stats_frame)
        stats_frame.pack(side="top", fill="x")
        for header in column_headers_to_display:
            if header in pitcher_data_map.get(spaced_pitcher_name, {}) and not pd.isnull(pitcher_data_map[spaced_pitcher_name][header]):
                stat_value = pitcher_data_map[spaced_pitcher_name][header]
                stat_label = ttk.Label(stats_frame, text=f"{header}: {stat_value}")
                stat_label.pack(side="left", padx=2, pady=2, anchor="w")

        images_frame = Frame(pitcher_stats_frame)
        images_frame.pack(side="top", fill="x", padx=2, pady=2)

        pitchername_reformatted = "_".join(pitcher_name.split(", "))
        load_images(pitchername_reformatted, images_frame)

        scraped_data_frame = ttk.LabelFrame(matchupframe, text=f"{pitcher_name} Statcast Stats")
        scraped_data_frame.pack(expand=True, fill="both", side="top", padx=5, pady=5)

        for key, value in pitcher_data.items():
            stat_label = ttk.Label(scraped_data_frame, text=f"{key}:", font=('Helvetica', 10, 'bold'))
            stat_label.pack(anchor="w", padx=5, pady=2)

            value_label = ttk.Label(scraped_data_frame, text=f"  % Ranking: {value['value']}", foreground=get_color(value['value']), font=('Helvetica', 10))
            value_label.pack(anchor="w", padx=10, pady=2)

            stat_value_label = ttk.Label(scraped_data_frame, text=f"  Stat: {value['stat']}", font=('Helvetica', 10))
            stat_value_label.pack(anchor="w", padx=10, pady=2)
            

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
        BPdata_dict = {BPstat_type: pd.read_csv(file) for file in all_matching_files}
        BP_dicts.append(BPdata_dict)

    # Create a parent frame for the adv_traits and splits_stats frames
    adv_splits_parent_frame = Frame(parent_frame)
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
                treeview.column(col, anchor='center', width=50, stretch=NO)

            for column_name in ("Trait", "Season"):
                if column_name in dataframe.columns:
                    if column_name == "Trait":
                        treeview.column(column_name, anchor='center', width=175, stretch=NO)
                    elif column_name == "Season":
                        treeview.column(column_name, anchor='center', width=75, stretch=NO)

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
                bullpen_data = pd.read_csv(filepath)
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
                reformatted_name = f"{firstname}_{lastname}" # Firstname Lastname is secretly Lastname Firstname in this instance...Magic
                try:
                    load_images(reformatted_name, target_frame)
                except FileNotFoundError:
                    # Scrape images if not found
                    print(f"Images for {reformatted_name} not found. Attempting to scrape...")
                    try:
                        # Lookup pitcher ID
                        pitcher_key, pitcher_id = BBSplayer_ids.LookupPitcher(selection, reverseOrder=True)
                        # Perform the scrape
                        scrape(selection, pitcher_id, True)
                        # Attempt to load images again
                        load_images(reformatted_name, target_frame)
                    except Exception as e:
                        print(f"Error scraping images for {reformatted_name}: {e}")

                return

            dropdown = ttk.OptionMenu(bullpen_frame, default_text, f"{team_name} Adv BP stats", *pitcher_names, command=DropdownCallback)
            dropdown.pack(expand=False, fill="none", anchor="center")

            bullpen_treeview = ttk.Treeview(matchupframe, columns=column_names, show='headings')
            for col in column_names:
                bullpen_treeview.heading(col, text=col)
                bullpen_treeview.column(col, anchor='center', width=75, stretch=NO) # Stretch needs to used with width !!!

            bullpen_treeview.pack(expand=False, fill="both")
            bullpen_treeview.column("Player", width=130)

            for index, row in bullpen_data.iterrows():
                values = [row[col] for col in column_names]
                bullpen_treeview.insert("", "end", values=values)

    return




def load_images(pitchername, frame):
    try:
        # Correcting the reformatting of pitchername
        # Split by underscore and reverse the order
        reversed_name_parts = pitchername.split("_")
        if len(reversed_name_parts) == 2:
            corrected_pitchername = f"{reversed_name_parts[1]}_{reversed_name_parts[0]}"
        else:
            corrected_pitchername = pitchername

        img1_path = pathlib.Path.cwd() / "MLBstats" / f"{corrected_pitchername}_trending_div.png"
        img2_path = pathlib.Path.cwd() / "MLBstats" / f"{corrected_pitchername}_pitch_distribution.png"

        if not img1_path.exists():
            print(f"Image not found: {img1_path}")
            raise FileNotFoundError(f"Image not found: {img1_path}")
        if not img2_path.exists():
            print(f"Image not found: {img2_path}")
            raise FileNotFoundError(f"Image not found: {img2_path}")

        img1 = Image.open(img1_path)
        img2 = Image.open(img2_path)

        img1 = ImageTk.PhotoImage(img1)
        img2 = ImageTk.PhotoImage(img2)

        label1 = Label(frame, image=img1)
        label2 = Label(frame, image=img2)

        label1.image = img1
        label2.image = img2

        label1.pack(side="left", anchor="e", padx=1, pady=2)
        label2.pack(side="left", anchor="w", padx=1, pady=2)
    except Exception as e:
        print(f"Error loading images: {e}")
    return

    

def Main():
    toplevel = tkinter.Tk()
    toplevel.title("ProbablePitchers")

    canvas = tkinter.Canvas(toplevel)
    canvas.pack(side=tkinter.LEFT, fill=tkinter.BOTH, expand=True)
    scrollbar = ttk.Scrollbar(toplevel, orient=tkinter.VERTICAL, command=canvas.yview)
    scrollbar.pack(side=tkinter.RIGHT, fill=tkinter.Y)

    frame = ttk.Frame(canvas)
    frame.pack(fill=tkinter.BOTH, expand=True)

    canvas.create_window((0, 0), window=frame, anchor="nw")

    def on_frame_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))

    frame.bind("<Configure>", on_frame_configure)

    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    # Windows and MacOS bindings for the mouse wheel
    # Only works when hovering over scroll bar
    toplevel.bind_all("<MouseWheel>", _on_mousewheel)
    toplevel.bind_all("<Button-4>", _on_mousewheel)
    toplevel.bind_all("<Button-5>", _on_mousewheel)

    PPFrame = PPFrameT(master=frame)
    PPFrame.pack(expand=False, side="left")

    dataframe = get_pitching_data()  # DON'T CALL THIS INLINE IN THE LAMBDA!!!!!! It will re-download EVERY ITERATION!

    def CreateTabLayoutLambda(matchupframe, matchupdict, dataframe):
        CreateTabLayoutCustom(matchupframe, matchupdict)
        FilloutStartingPitchers(matchupframe, matchupdict, dataframe)

    PPFrame.DownloadButtonHook = lambda a, b: (
        CreateTabLayoutLambda(a, b, dataframe)
    )

    toplevel.mainloop()
    return


if __name__ == "__main__":
    Main()
    
    
