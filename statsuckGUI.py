import tkinter as tk
import tkinter.filedialog
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from tkinter.filedialog import FileDialog

import subprocess
import pandas as pd
import csv
import pathlib

import pprint
import json

GridVarSelect = "Column"
CurrentRow = 0
CurrentColumn = 2


def NextCoord():
    if GridVarSelect == "Row":
        global CurrentRow
        CurrentRow += 1
    else:
        global CurrentColumn
        CurrentColumn += 1
    return CurrentRow, CurrentColumn


class TkApp(tk.Frame):
    def __init__(self, master=None, title="NoTitle"):
        super().__init__(master)
        self.master.title(title)
        self.grid()

        # Dropdown menu
        self.stats_var = tk.StringVar()
        self.stats_var.set("Select Stats Type")
        self.stats_dropdown = ttk.Combobox(self.master, textvariable=self.stats_var,
                                           values=["Rebounding", "Passing"])
        self.stats_dropdown.grid(row=0, column=0, padx=10, pady=10)
        
        # Fetch button
        self.fetch_button = tk.Button(self.master, text="Fetch", command=self.fetch_stats)
        self.fetch_button.grid(row=0, column=1, padx=10, pady=10)

        # Textbox to display data
        self.data_textbox = ScrolledText(self.master, width=200)
        self.data_textbox.grid(row=1, column=1, padx=10, pady=10)

        self.xdelta = 5
        self.ydelta = 5
    def ExpandTextbox(self):
        self.data_textbox["width"] += self.xdelta
        self.data_textbox["height"] += self.ydelta

    # placeholder
    def fetch_stats(self):
        print("pretending to fetch stats")
        self.data_textbox.insert(tk.END, "legit stats:\n 1 2 3 4 5\n 69")
    
    # why is this even a member function?
    def fetch_stats_real(self):
        stats_type = self.stats_var.get()

        if stats_type == "Rebounding":
            subprocess.run(["bash", "AutoBoundFetch.bash"], check=True)
            csv_path = "file/path"
        elif stats_type == "Passing":
            subprocess.run(["bash", "AutoAssistFetch.bash"], check=True)
            csv_path = "file/path"
        else:  # invalid selection, somehow
            print("stats_var has an invalid state")
            return

        # Read CSV file and display in the textbox
        try:
            df = pd.read_csv(csv_path)
            self.data_textbox.delete(1.0, tk.END)
            self.data_textbox.insert(tk.END, df.to_string(index=False))
        except pd.errors.EmptyDataError:
            self.data_textbox.delete(1.0, tk.END)
            self.data_textbox.insert(tk.END, "No data available.")


csv_subdir = pathlib.Path("CSV")
csv_nested_folders = {"source": pathlib.Path(csv_subdir / "source"),
                      "saved": pathlib.Path(csv_subdir / "saved")}

if not csv_subdir.exists():
    csv_subdir.mkdir()

for x in csv_nested_folders.values():
    if not x.exists():
        x.mkdir()

json_subdir = pathlib.Path("JSON")
if not json_subdir.exists():
    json_subdir.mkdir()

#def AddDropdown(self, master=None):
#    self.stats_var = tkinter.StringVar()
#    self.stats_var.set("Select Stats Type")
#    self.stats_dropdown = ttk.Combobox(master, textvariable=self.stats_var,
#                                       values=["Rebounding", "Passing"])
#    self.stats_dropdown.grid(row=0, column=0, padx=10, pady=10)

def CreateButton(parent, text, command):
    newbutton = tk.Button(parent, text=text, command=command)
    row, column = NextCoord()
    newbutton.grid(row=row, column=column, padx=10, pady=10)
    return newbutton


def pickFile(window, preselected_index=None):
    CSV_sourcefile_paths = [*csv_nested_folders["source"].glob("*.csv")]
    selection_dict = {}
    curindex = 0
    for filepath in CSV_sourcefile_paths:
        window.data_textbox.insert(tk.END, f"{curindex} : {filepath.stem}\n")
        selection_dict.update({curindex: filepath.stem})
        curindex += 1
    if preselected_index is None:
        userchoice = int(input("choose a file: "))
    else:  # prevent the prompt from blocking the application
        userchoice = preselected_index
    if userchoice < len(selection_dict):
        return selection_dict[userchoice]
    return None


def loadCSV(csv_name, fromdir="source"):
    try:
        thepath = csv_nested_folders[fromdir]
    except KeyError:
        print("specified path does not exist")
        return None
    csv_name = csv_name + ".csv"
    csv_path = thepath / csv_name
    try:
        df = pd.read_csv(csv_path)
    except pd.errors.EmptyDataError:
        print("No data available.")
        return None
    return df


def saveCSV(dataframe, targetname):
    thepath = csv_nested_folders["saved"]
    newname = targetname + ".csv"
    newfilepath = thepath / newname
    dataframe.to_csv(newfilepath)


def OpenFileDialog():
    with tkinter.filedialog.askopenfile(defaultextension='.csv', initialdir=csv_subdir,
                                        filetypes=[('CSV_file', '*.csv')], ) as openedfile:
        loadedCSV = pd.read_csv(openedfile)
        print(loadedCSV)
        NBAStatsApp.data_textbox.delete(1.0, tkinter.END)
        NBAStatsApp.data_textbox.insert(tk.END, loadedCSV.to_string(index=False))
    return loadedCSV


def OpenFileDialog_NoPandas():
    hugedict = {}
    #with tkinter.filedialog.askopenfile(defaultextension='.csv', initialdir=csv_nested_folders["source"],
    #                                    filetypes=[('CSV_file', '*.csv')]) as openedfile:
    with open(csv_nested_folders["source"] / "passing_data.csv") as openedfile:
        reader = csv.DictReader(openedfile)
        #NBAStatsApp.data_textbox.delete(1.0, tkinter.END)
        headers = reader.fieldnames
        #hugedict.update({'fields': reader.fieldnames})
        for line in reader:
            print(line)
            #NBAStatsApp.data_textbox.insert(tk.END, str(line))
            hugedict.update({reader.line_num: line})
    return hugedict, headers


JSON_Printer = pprint.PrettyPrinter(indent=2, width=120, compact=True)

hugedict, fields = OpenFileDialog_NoPandas()
print(fields)
JSON_Printer.pprint(hugedict)
headermap = {}
for field in fields:
    entries = [line[field] for line in hugedict.values()]
    headermap.update({field: entries})
JSON_Printer.pprint(headermap)
jsonmap = {
    "hugedict": [hugedict],  # line number: {fields:values}
    "headermap": [headermap],  # field:[values]
}


def MapStatCategories(first, second):
    zipped = zip(headermap[first], headermap[second])
    statmap = {}
    for fieldone, fieldtwo in zipped:
        if fieldone not in statmap.keys():
            statmap.update({fieldone: fieldtwo})
        else:
            statmap[fieldone].append(fieldtwo)
    return statmap


def MapMulti(first, *rest):
    targetmapping = {Field: headermap[Field] for Field in rest}
    combinedtargets = zip(*[D for D in targetmapping.values()])
    somepairs = list(zip(*combinedtargets))
    zipped = zip(headermap[first], somepairs)
    #statmap = {}
    #for entry in headermap[first]:
    #    if entry not in statmap.keys():
    #        statmap.update({fieldone: fieldtwo})
    #    else:
    #        statmap[fieldone].append(fieldtwo)
    #return statmap


#MapMulti("TEAM", "PLAYER", "PotentialAssists")

if __name__ == "__main__":
    root = tk.Tk()
    NBAStatsApp = TkApp(root)
    CreateButton(root, "expand textbox", NBAStatsApp.ExpandTextbox)
    #FD = FileDialog(NBAStatsApp)  #???
    #CreateButton(root, "ChooseCSV", OpenFileDialog_NoPandas)
    for name in jsonmap.keys():
        data = jsonmap[name][0]
        dumpfilepath = json_subdir / f"{name}.json"
        jsonmap[name].append(dumpfilepath)
        #jsontext = json.dumps(data, indent=2)
        jsontext = JSON_Printer.pformat(data)
        dumpfilepath.touch()
        assert dumpfilepath.exists()
        print(f"writing {dumpfilepath.stem}")
        with open(dumpfilepath, 'w', encoding="utf-8") as outfp:
            outfp.write(jsontext)
            #outfp.writelines(jsontext.splitlines())
        #    json.dump(data, outfp, indent=2)

    hugemap = MapStatCategories('PLAYER', 'PotentialAssists')
    pprint.pprint(hugemap)


    #choice = pickFile(NBAStatsApp, 0)
    #if choice is not None:
    #    loadedCSV = loadCSV(choice)
    #else:
    #    loadedCSV = loadCSV("passing_data")
    #if loadedCSV is not None:
    #    print(loadedCSV)
    #    saveCSV(loadedCSV, "resaved")
    #    # how the fuck do we select columns/rows?
    #    NBAStatsApp.data_textbox.insert(tk.END, loadedCSV.to_string(index=False))

    NBAStatsApp.mainloop()
