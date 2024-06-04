import tkinter
from tkinter import ttk


# To use this file, import it and redefine the two globals like this:
#   import DmFrameT
#   from DmFrameT import *  # don't do this
#   DmFrameT.LAYOUTMETHOD = tkinter.Widget.grid
#   DmFrameT.TOPLEVEL = ... (some toplevel widget)
# the second version of the import statement is broken

# these don't actually need to be defined here?
TOPLEVEL = None
LAYOUTMETHOD = None


class DmFrameT(ttk.Frame):
    def __init__(self, master=TOPLEVEL, name=None):
        super().__init__(master, name=name, borderwidth=10, relief="raised")
        # fallback to globals if nothing was passed
        self.TOPLEVEL = TOPLEVEL
        self.LAYOUTMETHOD = LAYOUTMETHOD
    
    def UpdateGlobals(self):
        if "LAYOUTMETHOD" in globals():
            self.LAYOUTMETHOD = LAYOUTMETHOD
        else:
            self.LAYOUTMETHOD = tkinter.Widget.grid
        if "TOPLEVEL" in globals():
            self.TOPLEVEL = TOPLEVEL
    

# if specified, the new widget is created inside the frame and returned
def InsertFrame(master=None, name=None, new_widget_class=None, new_toplevel=None, new_layoutmethod=None, **kwargs):
    global TOPLEVEL
    global LAYOUTMETHOD
    if new_toplevel is None: new_toplevel = TOPLEVEL
    else: TOPLEVEL = new_toplevel
    if new_layoutmethod is None: new_layoutmethod = LAYOUTMETHOD
    else: LAYOUTMETHOD = new_layoutmethod
    
    if name is not None:
        name = "frame_" + name
    newframe = DmFrameT(master, name)
    #newframe.UpdateGlobals()
    #if master is None:
    #    master = newframe.TOPLEVEL
    newframe.LAYOUTMETHOD(newframe)
    if new_widget_class is not None:
        new_widget = new_widget_class(master=newframe, **kwargs)
        newframe.LAYOUTMETHOD(new_widget)
        return newframe, new_widget
    return newframe


if __name__ == "__main__":
    TOPLEVEL = tkinter.Tk()
    newframe = InsertFrame(TOPLEVEL, "exampleframe")
    newframe2 = InsertFrame(TOPLEVEL, "exampleframe2")
    newframe2.grid(row=1, column=1)
    TOPLEVEL.mainloop()
