import tkinter
from tkinter import ttk


# To use this file, import it and redefine the two globals like this:
#   import BaseFrame
#   from BaseFrame import *  # don't do this
#   BaseFrame.LAYOUTMETHOD = tkinter.Widget.grid
#   BaseFrame.TOPLEVEL = ... (some toplevel widget)
# the second version of the import statement is broken

# these don't actually need to be defined here?
TOPLEVEL = None
LAYOUTMETHOD = None

class BaseFrame(ttk.Frame):
    TOPLEVEL = None
    LAYOUTMETHOD = None
    def __init__(self, master=TOPLEVEL, name=None):
        super().__init__(master, name=name, borderwidth=10, relief="raised")


def Update():
    if "LAYOUTMETHOD" in globals():
        BaseFrame.LAYOUTMETHOD = LAYOUTMETHOD
    else:
        BaseFrame.LAYOUTMETHOD = tkinter.Widget.pack    
    if "TOPLEVEL" in globals():
        BaseFrame.TOPLEVEL = TOPLEVEL
    

def InsertFrame(master=None, name=None):
    if name is not None:
        name = "frame_" + name
    Update()
    if master is None:
        master = BaseFrame.TOPLEVEL
    newframe = BaseFrame(master, name)
    BaseFrame.LAYOUTMETHOD(newframe)
    #new_button = ttk.Button(newframe, text="button")
    #BaseFrame.LAYOUTMETHOD(new_button)
    return newframe


if __name__ == "__main__":
    TOPLEVEL = tkinter.Tk()
    newframe = InsertFrame(TOPLEVEL, "exampleframe")
    newframe2 = InsertFrame(TOPLEVEL, "exampleframe2")
    newframe2.grid(row=1, column=1)
    TOPLEVEL.mainloop()
