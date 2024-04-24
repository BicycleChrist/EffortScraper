import tkinter
from tkinter import ttk


class App(tkinter.Tk):
    def __init__(self):
        super().__init__()
        self.geometry("800x600")
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

        for themename in themes:
            # setting default arguments is required for working around python jank
            def LambdaSetTheme(name=themename):
                self.title(name)
                self.style.theme_use(name)
                print(name)
            new_button = ttk.Button(button_frame, text=themename, command=LambdaSetTheme)
            new_button.pack(pady=2)
        return

    def CreateTab(self, name):
        new_frame = ttk.Frame(self.tab_control)
        self.tab_control.add(new_frame, text=name)
        self.CreateThemeButtons(new_frame)
        return


if __name__ == "__main__":
    app = App()
    for sport in ("NBA", "NHL", "NFL", "MLB"):
        app.CreateTab(sport)

    app.mainloop()
