import tkinter as tk
from PIL import Image, ImageTk

def main():
    # Create the Tkinter window
    root = tk.Tk()
    root.title("Icon Demo")

    # Load the .ico file using Pillow
    icon_path = "Eff0rt.ico"  # Assuming the icon file is in the same directory
    icon = Image.open(icon_path)
    photo = ImageTk.PhotoImage(icon)

    # Set the icon of the window
    root.iconphoto(True, photo)

    # Add some widgets or functionality
    label = tk.Label(root, text="Hello, world!")
    label.pack(padx=20, pady=20)

    # Run the Tkinter event loop
    root.mainloop()

if __name__ == "__main__":
    main()
