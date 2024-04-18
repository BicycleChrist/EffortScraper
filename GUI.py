import tkinter
from tkinter import ttk
import ttkthemes



class App(tkinter.Tk):
    def __init__(self):
        super().__init__()
        #self.title("Sports Betting App")
        self.geometry("800x600")
        #self.configure(bg="#FF0000")
        
        # Create the tab control
        self.tab_control = ttk.Notebook(self)
        
        # Configure tab styles
        self.style = ttkthemes.ThemedStyle()
        # ttk.Style().theme_names() lists available themes: ('clam', 'alt', 'default', 'classic')
        # got the additional theme pack to work with significantly more options, list of additional themes --> (https://wiki.tcl-lang.org/page/List+of+ttk+Themes)
        self.style.theme_use("aqua")
        #self.style.configure("alt", background="#FF0000")
        #self.style.configure("TNotebook", background="#2c2c2c")
        #self.style.configure("TNotebook.Tab", background="#404040", foreground="white", font=("Helvetica", 12))
        #self.style.map("TNotebook.Tab", background=[("selected", "#666666")])
        
        self.title(self.style.theme_use())
        
        # Position the tab control
        self.tab_control.pack(expand=True, fill="both")
    # end of init
    
    
    def CreateThemeButtons(self, parent):
        themes = self.style.theme_names()
        # Create a frame to hold the buttons
        button_frame = ttk.Frame(parent, padding=20)
        button_frame.pack(fill="both", expand=True)
        
        for themename in themes:
            # setting default arguments is required for working around python jank
            def LambdaSetTheme(name=themename):
                self.title(name)
                self.style.theme_use(name)
                print(name)
            new_button = ttk.Button(button_frame, text=themename, command=LambdaSetTheme)
            new_button.pack(pady=10)
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
