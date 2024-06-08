import update_importpaths
from DmFrame import InsertFrame
import pandas as pd
import tkinter
from tkinter import *
from tkinter import ttk
from ProbablePitchersFrame import PPFrameT
from DmNotebook import DmNotebookT
from stuffsuck import get_pitching_data
from BBSplayer_ids import pitchers
from penski import GetFilepath
import pathlib
from pathlib import Path
from PIL import Image, ImageTk

def FilloutStartingPitchers(matchupframe, matchup_dict, dataframe):
    starting_pitcher_names = list(matchup_dict['pitchers'].keys())
    formatted_pitcher_names = {}
    pitcher_id_map = {}  # This is being done in order to perform a lookup in the BBS dict to pass into the scrape function

    for name in starting_pitcher_names:
        last_first_name = ", ".join(name.split()[::-1])
        formatted_pitcher_names[name] = last_first_name
        player_id = pitchers.get(last_first_name)
        pitcher_id_map[last_first_name] = player_id

    filtered_df = dataframe[dataframe['Name'].isin(starting_pitcher_names)]
    pitcher_data_map = {row['Name']: row for index, row in filtered_df.iterrows()}

    column_headers_to_display = [
        'Stf+ FA', 'Stf+ SI', 'Stf+ FC', 'Stf+ FS',
        'Stf+ SL', 'Stf+ CU', 'Stf+ CH', 'Stf+ KC', 'Stf+ FO',
        'Stuff+', 'Location+', 'Pitching+'
    ]

    for pitcher_name, pitcher_dict in matchup_dict['pitchers'].items():
        pitcher_frame = ttk.LabelFrame(matchupframe, text=pitcher_name)
        pitcher_frame.pack(expand=True, fill="both", side="top", anchor="nw")
        for key, value in pitcher_dict.items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.pack(expand=True, fill="both", side="top", anchor="nw")

        pitcher_data = pitcher_data_map.get(pitcher_name)
        if pitcher_data is not None:
            pitcher_stats_frame = ttk.LabelFrame(matchupframe, text=f"{pitcher_name} Stuff+ Stats")
            pitcher_stats_frame.pack(expand=True, fill="both", side="top", anchor="sw")

            for header in column_headers_to_display:
                if header in pitcher_data and not pd.isnull(pitcher_data[header]):
                    stat_value = pitcher_data[header]
                    stat_label = ttk.Label(pitcher_stats_frame, text=f"{header}: {stat_value}")
                    stat_label.pack(side="left", padx=2, pady=2)

    return

def Fillout_BP_Frame(parent_frame, possible_files: dict):
    BP_dicts = []  # list of dicts; maps BPstat_type to a dataframe (loaded from csv)
    for BPstat_type, all_matching_files in possible_files.items():
        BPdata_dict = {BPstat_type: pd.read_csv(file) for file in all_matching_files}
        BP_dicts.append(BPdata_dict)

    for BP_dict in BP_dicts:
        for BPstat_type, dataframe in BP_dict.items():
            dataframe.columns = dataframe.columns.str.strip()
            BPstat_frame = ttk.LabelFrame(parent_frame, text=BPstat_type)
            BPstat_frame.pack(expand=False, fill="none", anchor="w", side="top")

            treeview = ttk.Treeview(BPstat_frame, columns=list(dataframe.columns), show='headings')
            treeview.pack(expand=False, fill="x", side="bottom")

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
    bullpen_dir = GetFilepath('bullpen_stats', '').parent
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
                    BPstat_type: GetFilepath(BPstat_type, formatted_name, append_date=False)
                    for BPstat_type in ('adv_traits', 'splits_stats')
                }
                for widget in target_frame.winfo_children():
                    widget.pack_forget()
                Fillout_BP_Frame(target_frame, possible_files)

                # Load images
                lastname, firstname = selection.split()
                load_images(lastname, firstname, target_frame)

                return

            dropdown = ttk.OptionMenu(bullpen_frame, default_text, "Adv BP stats", *pitcher_names, command=DropdownCallback)
            dropdown.pack(expand=False, fill="none", anchor="center")

            bullpen_treeview = ttk.Treeview(matchupframe, columns=column_names, show='headings')
            for col in column_names:
                bullpen_treeview.heading(col, text=col)
                bullpen_treeview.column(col, anchor='center', width=75, stretch=NO)

            bullpen_treeview.pack(expand=False, fill="both")
            bullpen_treeview.column("Player", width=130)

            for index, row in bullpen_data.iterrows():
                values = [row[col] for col in column_names]
                bullpen_treeview.insert("", "end", values=values)

    return

def load_images(lastname, firstname, frame):
    try:
        img1_path = Path.cwd() / "MLBstats" / f"{firstname}_{lastname}_trending_div.png" # Somehow if you go lastname_firstname it wont load even though thats the correct file name
        img2_path = Path.cwd() / "MLBstats" / f"{firstname}_{lastname}_pitch_distribution_svg.png" # Easily the dumbest shit i've ever seen

        if not img1_path.exists():
            print(f"Image not found: {img1_path}")
            raise FileNotFoundError(f"Image not found: {img1_path}")
        if not img2_path.exists():
            # Adjust filename format here
            img2_path = Path.cwd() / "MLBstats" / f"{firstname}_{lastname}_pitch_distribution_svg.png"
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

        label1.pack(side="right", anchor="e", padx=10, pady=10)
        label2.pack(side="right", anchor="e", padx=10, pady=10)
    except Exception as e:
        print(f"Error loading images: {e}")


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

    toplevel.bind_all("<MouseWheel>", _on_mousewheel)
    toplevel.bind_all("<Button-4>", _on_mousewheel)
    toplevel.bind_all("<Button-5>", _on_mousewheel)

    PPFrame = PPFrameT(master=frame)
    PPFrame.pack(expand=False, side="left")

    dataframe = get_pitching_data()
    def CreateTabLayoutLambda(matchupframe, matchupdict, dataframe):
        CreateTabLayoutCustom(matchupframe, matchupdict)
        FilloutStartingPitchers(matchupframe, matchupdict, dataframe)

    PPFrame.DownloadButtonHook = lambda a, b: (
        CreateTabLayoutLambda(a, b, dataframe)
    )

    toplevel.mainloop()

if __name__ == "__main__":
    Main()
