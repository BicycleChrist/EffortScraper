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
        self.geometry("1920x1080")
        if USETTKTHEMES:
            self.style = ttkthemes.ThemedStyle()
        else:
            self.style = ttk.Style()

        # ttk.Style().theme_names() lists available themes: ('clam', 'alt', 'default', 'classic')
        self.style.theme_use("default")
        self.title(self.style.theme_use())  # set title to current theme name

        self.tab_control = ttk.Notebook(self)
        self.tab_control.grid(rowspan=100, columnspan=100)
        self._LOADED_IMAGES = {}
        return

    def CreateThemeButtons(self, parent):
        themes = self.style.theme_names()
        button_frame = ttk.Frame(parent, padding=20)
        button_frame.grid(rowspan=100, columnspan=100)
        
        for index, themename in enumerate(sorted(themes)):
            # setting default arguments is required for working around python jank
            def LambdaSetTheme(name=themename):
                self.title(name)
                self.style.theme_use(name)
                print(name)
            new_button = ttk.Button(button_frame, text=themename, command=LambdaSetTheme)
            new_button.grid(column=index, row=1)
        return

    def CreateTab(self, name):
        new_frame = ttk.Frame(self.tab_control)
        new_frame.grid(rowspan=100, columnspan=100)
        self.tab_control.add(new_frame, text=name)
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
        print(f"loaded images: {[png.name for png in pngs]}")
        return self._LOADED_IMAGES[subpath]


if __name__ == "__main__":
    app = App()
    imagefolders = app.LoadImages("")  # loads all subdirectories under 'TeamLogos'
    print(imagefolders)
    scaledlogos = {}
    lastCol = 0
    lastRow = 0
    
    for sport in imagefolders.keys():
        tabframe = app.CreateTab(sport)
        logoframe = ttk.Frame(master=app)
        logoframe.grid(rowspan=100, columnspan=100, sticky="nw")
        logos = app.LoadImages(sport)
        for name, logo in logos.items():
            def printlogoname(n=name): return lambda: print(n)
            scaledlogos[name] = logo.subsample(15,15)
            newbutton = ttk.Button(master=logoframe, image=scaledlogos[name], command=printlogoname(), text=name, padding=10)
            if lastRow > 9:
                lastRow = 0
                lastCol += 1
            newbutton.grid(row=lastRow, column=lastCol)
            lastRow += 1

    app.mainloop()
