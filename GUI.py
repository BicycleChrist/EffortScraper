import tkinter
from tkinter import ttk
import pathlib
#from SplashScreen import *
# uncomment above for suspect Splash Screen render

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
    # Load NHL logos
    nhl_logos = LoadImages(app, "TeamLogos/NHLlogos")
    scaled_nhl_logos = [img.subsample(5, 5) for img in nhl_logos]

    # Load MLB logos
    mlb_logos = LoadImages(app, "TeamLogos/MLBlogos")
    scaled_mlb_logos = [img.subsample(10, 10) for img in mlb_logos]

    # Load NFL logos
    nfl_logos = LoadImages(app, "TeamLogos/NFLlogos")
    scaled_nfl_logos = [img.subsample(10, 10) for img in nfl_logos]

    # Load NBA logos
    nba_logos = LoadImages(app, "TeamLogos/NBAlogos")
    scaled_nba_logos = [img.subsample(10, 10) for img in nba_logos]

    for sport, logos in (("NHL", scaled_nhl_logos), ("MLB", scaled_mlb_logos), ("NFL", scaled_nfl_logos), ("NBA", scaled_nba_logos)):
        new_frame = app.CreateTab(sport)
        for image in logos:
            tkinter.Label(master=new_frame, image=image).pack(anchor="nw", side="left")

    graph_frame = app.CreateTab("graphs")
    graph_images = LoadImages(graph_frame, "NHLvacuum/nhlteamreports/BOS/generalTRdata/2024-04-27/rollingavggraphs/")
    for graph in graph_images:
        tkinter.Label(master=graph_frame, image=graph).pack(anchor="nw", side="top")


app.mainloop()
