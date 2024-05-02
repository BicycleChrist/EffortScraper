import tkinter
from tkinter import ttk
import pathlib


# ttkthemes requires pip install
USETTKTHEMES = False
if USETTKTHEMES:
    import ttkthemes
# https://ttkthemes.readthedocs.io/en/latest/themes.html


class App(tkinter.Tk):
    def __init__(self):
        super().__init__()
        if USETTKTHEMES:
            self.style = ttkthemes.ThemedStyle()
        else:
            self.style = ttk.Style()

        # ttk.Style().theme_names() lists available themes: ('clam', 'alt', 'default', 'classic')
        self.style.theme_use("default")
        self.title(self.style.theme_use())  # set title to current theme name

        self._LOADED_IMAGES = {}
        self._LOGOS_STORAGE = []  # required to keep images from unloading 
        self._SCALED_LOGOS_STORAGE = []  # (specifically the logos in 'DrawLogos' function)
        
        self.BUTTON_FRAMES_ENABLED = True
        self.ButtonFrameToggle = tkinter.Checkbutton(master=self, text="Draw Button Frames", 
                                                     command=self.ToggleButtonFrames, takefocus=False, 
                                                     relief="raised", overrelief="sunken", borderwidth=3)
        self.ButtonFrameToggle.select()  # sets initial state to true
        self.ButtonFrameToggle.grid()
        
        self.tab_control = ttk.Notebook(self, padding=(5,5,5,5))
        self.tab_control.grid()
        return
    
    
    def ToggleButtonFrames(self):
        self.BUTTON_FRAMES_ENABLED = not self.BUTTON_FRAMES_ENABLED
        tabnames = [self.tab_control.tab(tab, "text") for tab in self.tab_control.tabs()]
        currentTab = self.tab_control.select()
        # redrawing all logos/tabs. once deferred loading is implemented this won't be necessary
        for tabname in tabnames:
            self.DrawLogos(tabname)
        self.tab_control.select(currentTab)  # re-selecting previous tab
        return
    
    
    def CreateThemeButtons(self, parent):
        themes = self.style.theme_names()
        button_frame = ttk.Frame(parent, padding=20)
        button_frame.grid()
        
        for index, themename in enumerate(sorted(themes)):
            # setting default arguments is required for working around python jank
            def LambdaSetTheme(name=themename):
                self.title(name)
                self.style.theme_use(name)
                print(name)
            new_button = ttk.Button(button_frame, text=themename, command=LambdaSetTheme)
            new_button.grid(column=index, row=0)
        return
    
    
    def CreateTab(self, tabname):
        # for some reason names that start uppercase are illegal in Tcl/Tk so we have to prepend something
        new_frame = ttk.Frame(self.tab_control, name=f"tab_{tabname}")
        new_frame.grid()
        self.tab_control.add(new_frame, text=tabname)
        self.CreateThemeButtons(new_frame)
        return new_frame
    
    
    # from TkExperiments
    # https://docs.python.org/3/library/tkinter.html#images
    def LoadImages(self, subpath: str):
        #print(tkinter.image_types())  # prints list of supported types
        # 'photo', 'bitmap' are your options. PNG and GIF are supported.
        # see the manpage (photo.tk3) for info on custom format-loaders. also see: image.tk3
        imagepath = pathlib.Path.cwd() / "TeamLogos" / subpath
        if not (imagepath.exists() and imagepath.is_dir()):
            print(f"image folder: {subpath} not found")
            return {}
        if subpath in self._LOADED_IMAGES and self._LOADED_IMAGES[subpath] is not None:
            return self._LOADED_IMAGES[subpath]  # already loaded
        
        pngs = list(imagepath.glob('*.png'))
        # load and map all images under subpath. Make an entry for every subdirectory (mapped to None)
        # the idea is to defer the searching/loading of subdirectories
        self._LOADED_IMAGES[subpath] = {
            **{subfolder.name: None for subfolder in imagepath.glob('*') if subfolder.is_dir()},
            **{path.stem: tkinter.PhotoImage(master=self, file=path, name=path.stem) for path in pngs},
        }
        # you can assign a loaded image to a widget by using it's name in the 'image=' argument!
        print(f"\nloaded images: {[png.name for png in pngs]}\n")
        return self._LOADED_IMAGES[subpath]
    
    
    def DrawLogos(self, sportname):
        tabframe = self.CreateTab(sportname)
        logos = self.LoadImages(sportname)
        scaledlogos = {}
        
        # relief can be: flat, groove, raised, ridge, solid, or sunken
        logoframe = ttk.Frame(master=tabframe, relief="flat", borderwidth=5)
        logoframe.grid()
        outer_subframe = ttk.Frame(master=logoframe, relief="ridge", border=5, borderwidth=10, padding=(0,0,0,0))
        lastCol = 0
        lastRow = 0
        
        for name, logo in logos.items():
            scaledlogos[name] = logo.subsample(10,10)
            def printlogoname(n=name): return lambda: print(n)
            
            # the 'sticky' option basically controls the expansion of the element.
            # setting all directions causes all elements to basically become square
            outer_subframe.grid(sticky="NSEW")
            if not self.BUTTON_FRAMES_ENABLED:
                subframe = outer_subframe
            else:
                subframe = ttk.Frame(master=outer_subframe, relief="raised", borderwidth=10, border=10, padding=(0,0,0,0)) # padding can be negative here (left, top, right, bottom)
            # the "compound" option here causes it to display both text and the image together.
            # 'width' sets the minimum size, in case the images are smaller. Otherwise it should scale to image. (there is no height, lmao)
            newbutton = ttk.Button(master=subframe, image=scaledlogos[name], command=printlogoname(), text=name, compound="top", width=15, padding=(0,0,0,0))
            newbutton.grid(row=lastRow, column=lastCol, padx=2, pady=2, ipadx=0, ipady=0, sticky="SNEW")
            subframe.grid(row=lastRow, column=lastCol, padx=0, pady=0, ipadx=0, ipady=0, sticky="WE")
            
            lastCol += 1
            if lastCol > 10:
                lastCol = 0
                lastRow += 1
        
        # must store the images otherwise they'll unload
        self._LOGOS_STORAGE.append(logos)
        self._SCALED_LOGOS_STORAGE.append(scaledlogos)
        return


if __name__ == "__main__":
    app = App()
    imagefolders = app.LoadImages("")  # loads all subdirectories under 'TeamLogos'
    print(imagefolders)
    
    for sport in imagefolders.keys():
        app.DrawLogos(sport)
    
    app.mainloop()
