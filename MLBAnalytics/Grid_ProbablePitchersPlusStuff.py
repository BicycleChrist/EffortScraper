import update_importpaths
from Grid_DmFrame import InsertFrame
import pandas
import tkinter
from tkinter import ttk
from Grid_ProbablePitchersFrame import PPFrameT
from stuffsuck import get_pitching_data
#from BBSavant_statcast import scrape
from BBSplayer_ids import pitchers
from penski import GetFilepath
import pathlib


# pitcher_items = matchup_dict['pitchers'].items()
def FilloutStartingPitchers(matchupframe, matchup_dict, dataframe):
    # Collect all pitcher names
    starting_pitcher_names = list(matchup_dict['pitchers'].keys())
    
    # Create a dictionary to store the reformatted pitcher names
    # This is being done in order to perform a lookup in the BBS dict to pass into the scrape function
    formatted_pitcher_names = {}
    pitcher_id_map = {}

    for name in starting_pitcher_names:
        last_first_name = ", ".join(name.split()[::-1])
        formatted_pitcher_names[name] = last_first_name

        # Retrieve the player ID from the pitchers dictionary
        player_id = pitchers.get(last_first_name)
        pitcher_id_map[last_first_name] = player_id
    print("Formatted Pitcher Names:", formatted_pitcher_names)
    print("Pitcher ID Map:", pitcher_id_map)
    
    # Filter the dataframe once for all pitcher names
    filtered_df = dataframe[dataframe['Name'].isin(starting_pitcher_names)]
    
    # Create a dictionary to map pitcher names to their data
    pitcher_data_map = {row['Name']: row for index, row in filtered_df.iterrows()}
    
    column_headers_to_display = [
        'Stf+ FA', 'Stf+ SI', 'Stf+ FC', 'Stf+ FS',
        'Stf+ SL', 'Stf+ CU', 'Stf+ CH', 'Stf+ KC', 'Stf+ FO',
        'Stuff+', 'Location+', 'Pitching+'
    ]
    
    column = 0
    row = 4
    bottomframe = ttk.Frame(master=matchupframe)
    bottomframe.grid(row=row, column=column)
    for pitcher_name, pitcher_dict in matchup_dict['pitchers'].items():
        pitcher_frame = ttk.LabelFrame(bottomframe, text=pitcher_name)
        pitcher_frame.grid(column=column)
        for key, value in pitcher_dict.items():
            textbox = ttk.Label(master=pitcher_frame, text=f"{key}: {value}")
            textbox.grid(row=row, column=column)

        pitcher_data = pitcher_data_map.get(pitcher_name)
        if pitcher_data is not None:
            pitcher_stats_frame = ttk.LabelFrame(bottomframe, text=f"{pitcher_name} Stuff+ Stats")
            pitcher_stats_frame.grid(row=row, column=column)
            
            row += 1

            for header in column_headers_to_display:
                if header in pitcher_data and not pandas.isnull(pitcher_data[header]):
                    stat_value = pitcher_data[header]
                    stat_label = ttk.Label(pitcher_stats_frame, text=f"{header}: {stat_value}")
                    stat_label.grid(row=row, column=column)
                    
                    row += 1
        column += 1
        row = 4
    
    return

def Fillout_BP_Frame(parent_frame, possible_files:dict):
    print(f"Filling BP_Frame with files: {possible_files.items()}")
    # possible_files maps BPstat_type to a filepath
    BP_dicts: list[dict] = [] # list of dicts; maps BPstat_type to a dataframe (loaded from csv) 
    for BPstat_type, all_matching_files in possible_files.items():
        BPdata_dict = { BPstat_type : pandas.read_csv(file) for file in all_matching_files }
        #if file.exists(): #should already exist if we got it by glob
        BP_dicts.append(BPdata_dict)
    
    for BP_dict in BP_dicts:
        print(BP_dict)
        for BPstat_type, dataframe in BP_dict.items():
            print(BPstat_type)
            print(dataframe)
            print(f"Building Treeview for: {BPstat_type}")
            BPstat_frame, treeview = InsertFrame(
                parent_frame, new_widget_class=ttk.Treeview,
                columns=dataframe.columns, show='headings'
            )
            # 'show' can also take the value 'tree'
            BPstat_frame.grid()
            # is this even necessary? what was the point of passing to treeview constructor?
            for col in dataframe.columns:
                # Set column headings
                treeview.heading(col, text=col)
                # Set column widths
                treeview.column(col, anchor='center')
            # Iterate over DataFrame rows and insert  into the Treeview
            for index, row in dataframe.iterrows():
                # Extracting values for each row
                values = [row[col] for col in dataframe.columns]
                # Insert row into the Treeview
                treeview.insert("", "end", values=values)
            treeview.grid()
    print("BP_Frame done")
    return


def CreateTabLayoutCustom(matchupframe, matchup_dict):
    print("tablayout")
    
    bullpen_dir = GetFilepath('bullpen_stats', '').parent
    bullpen_files = bullpen_dir.glob("*bullpen_stats*.csv")
    
    def GetTeamname(filepath: pathlib.Path): 
        return filepath.name.split("_Bullpen", maxsplit=1)[0]
    
    bullpen_dict = { GetTeamname(bullpen_file):bullpen_file for bullpen_file in bullpen_files }
    #print(bullpen_dict.items())
    #print(matchup_dict)
    for side in ['home', 'away']:
        team_name = matchup_dict['teams'][side]['name']
        #print(team_name)
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
            bullpen_frame = ttk.LabelFrame(matchupframe, text=f"{team_name} Bullpen Usage")
            bullpen_frame.grid()
            bullpen_subframe = InsertFrame(bullpen_frame, new_toplevel=bullpen_frame, new_widget_class=None)
            bullpen_subframe.grid()
            
            # Extract column names 
            column_names = list(bullpen_data.columns)
            pitcher_names = list(bullpen_data.Player)
            default_text = tkinter.StringVar()
            default_text.set("Default Text")
            def DropdownCallback(stringvar: str):
                print(f"{stringvar} selected")
                stringvar = stringvar.strip().replace(' ', '_')  # clean up player name
                possible_files = {
                    BPstat_type : GetFilepath(BPstat_type, stringvar, append_date=False) 
                    for BPstat_type in ('adv_traits', 'splits_stats')
                }
                Fillout_BP_Frame(bullpen_subframe, possible_files)
                return
            
            dropdown = ttk.OptionMenu(bullpen_subframe, default_text, "Default Text", *pitcher_names, command=DropdownCallback)
            dropdown.grid()
            
            # Create a Treeview 
            bullpen_treeview = ttk.Treeview(bullpen_frame, columns=column_names, show='headings')
            for col in column_names:
                # Set column headings
                bullpen_treeview.heading(col, text=col)
                # Set column widths
                bullpen_treeview.column(col, width=100, anchor='center')
            bullpen_treeview.grid()
            
            # Iterate over DataFrame rows and insert  into the Treeview
            for index, row in bullpen_data.iterrows():
                # Extracting values for each row
                values = [row[col] for col in column_names]
                # Insert row into the Treeview
                bullpen_treeview.insert("", "end", values=values)
    return

def Main():
    toplevel = tkinter.Tk()
    toplevel.title("ProbablePitchers")
    PPFrame = PPFrameT(master=toplevel)
    PPFrame.pack()

    # Define callback hook
    dataframe = get_pitching_data()  # DON'T CALL THIS INLINE IN THE LAMBDA!!!!!! It will re-download EVERY ITERATION!
    def CreateTabLayoutLambda(matchupframe, matchupdict, dataframe):
        CreateTabLayoutCustom(matchupframe, matchupdict)
        FilloutStartingPitchers(matchupframe, matchupdict, dataframe)
        
    PPFrame.DownloadButtonHook = lambda a, b: (
        CreateTabLayoutLambda(a, b, dataframe)
    )
    
    print("mainloop")
    toplevel.mainloop()

if __name__ == "__main__":
    Main()








