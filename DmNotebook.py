import tkinter
from tkinter import ttk


class DmNotebookT(ttk.Notebook):
    def __init__(self, master=None):
        super().__init__(master)
        self.pack(expand=1, fill="both")
        self.WidgetStorage = {}  # maps tab-names (human-readable) to contained widget
        self.TabList = []  # list of dicts, in the same form as "current_tab" below
        # "name" is human-readable, "TCLname" is required for calling 'notebook.select' and such
        self.current_tab = { "name": None, "TCLname": None, "index": None }
        self.bind("<<NotebookTabChanged>>", self.Callback_TabChanged, add='+')
        self.enable_traversal()  # allows navigation with Ctrl+Tab
    
    # adds tab with a widget placed inside it; returns the widget
    def AddTab(self, name, new_widget_class=None):
        lastindex = self.index("end")
        internal_name = "tab_" + name  # uppercase names are invalid in TCL, so we prepend to be safe
        if new_widget_class is not None:
            new_widget = new_widget_class(master=self, name=internal_name)
        else:
            new_widget = ttk.Frame(master=self, name=internal_name, borderwidth=4, relief="ridge")
        new_widget.pack(expand=1, fill="both")
        self.WidgetStorage[name] = new_widget
        self.add(new_widget, text=name)
        # doesn't seem necessary?
        #self.select(lastindex)  # selects newly-added tab
        tab_metadata = { "name": name, "TCLname": self.select(), "index": lastindex }
        self.TabList.append(tab_metadata)
        return new_widget
    
    def Callback_TabChanged(self, event):
        self.current_tab = { 
            "name": self.tab('current', option='text'), 
            "TCLname": self.select(), 
            "index": self.index(self.select()) 
        }
        print(self.current_tab)
    
    def AddCallback(self, callbackFn):
        self.bind("<<NotebookTabChanged>>", callbackFn, add='+')



if __name__ == "__main__":
    toplevel = tkinter.Tk(sync=False)
    notebook = DmNotebookT(toplevel)
    
    textwidget = notebook.AddTab("ABC", tkinter.Text)
    textwidget.insert(tkinter.END, "epic placeholder text!")
    notebook.WidgetStorage["ABC"].insert(tkinter.END, "\ntest2!")  # inserting text directly into widget
    notebook.AddTab("tabtwo", tkinter.Text)
    print(notebook.TabList)
    print(notebook.WidgetStorage)
    
    toplevel.mainloop()
    print("plz dont exit")
