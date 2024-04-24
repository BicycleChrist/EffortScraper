import tkinter
from tkinter import ttk
import pathlib

# ttkthemes requires pip install
USETTKTHEMES = True
if USETTKTHEMES:
    import ttkthemes
# https://ttkthemes.readthedocs.io/en/latest/themes.html


class App(tkinter.Tk):
    def __init__(self):
        super().__init__()
        self.geometry("800x600")
        if USETTKTHEMES:
            self.style = ttkthemes.ThemedStyle()
        else:
            self.style = ttk.Style()
        
        # ttk.Style().theme_names() lists available themes: ('clam', 'alt', 'default', 'classic')
        self.style.theme_use("default")
        self.title(self.style.theme_use())  # set title to current theme name

        self.tab_control = ttk.Notebook(self)
        self.tab_control.pack(expand=True, fill="both")
        return

    def CreateThemeButtons(self, parent):
        themes = self.style.theme_names()
        button_frame = ttk.Frame(parent, padding=20)
        button_frame.pack(fill="both", expand=True)

        for themename in sorted(themes):
            # setting default arguments is required for working around python jank
            def LambdaSetTheme(name=themename):
                self.title(name)
                self.style.theme_use(name)
                print(name)
            new_button = ttk.Button(button_frame, text=themename, command=LambdaSetTheme)
            new_button.pack(side="left", anchor="nw")
        return

    def CreateTab(self, name):
        new_frame = ttk.Frame(self.tab_control)
        self.tab_control.add(new_frame, text=name)
        self.CreateThemeButtons(new_frame)
        return new_frame


# from TkExperiments
# https://docs.python.org/3/library/tkinter.html#images

def LoadImages(master, subpath):
    print(tkinter.image_types())  # prints list of supported types
    # 'photo', 'bitmap' are your options. PNG and GIF are supported.
    # see the manpage (photo.tk3) for info on custom format-loaders. also see: image.tk3
    cwd = pathlib.Path.cwd()
    imagepath = cwd / subpath
    if not (imagepath.exists() and imagepath.is_dir()):
        print(f"image folder: {subpath} not found")
        return []
    pngs = list(imagepath.glob('*.png'))
    loaded = [tkinter.PhotoImage(master=master, file=path, name=path.stem) for path in pngs]
    # you can assign a loaded image to a widget by using it's name in the 'image=' argument!
    return loaded


if __name__ == "__main__":
    app = App()
    teamlogos = LoadImages(app, "CHN/teamlogos/")
    print(f"loaded images: = {[img.name for img in teamlogos]}")
    scaledlogos = [img.subsample(5, 5) for img in teamlogos]
    # TODO: figure out how to keep the names for the scaled images
    
    for sport in ("NBA", "NHL", "NFL", "MLB"):
        newframe = app.CreateTab(sport)
        if sport == "NHL":
            for image in scaledlogos:
                tkinter.Label(master=newframe, image=image).pack(anchor="nw", side="left")

    graphframe = app.CreateTab("graphs")
    graphimages = LoadImages(graphframe, "NHLvacuum/nhlteamreports/COL/generalTRdata/2024-04-22/rollingavggraphs/")
    for graph in graphimages:
        tkinter.Label(master=graphframe, image=graph).pack(anchor="nw", side="top")

    app.mainloop()
