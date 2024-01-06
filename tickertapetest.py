import random
from tkinter import *

window = Tk()
window.title("tickertape")
dimensions = [960, 20]

canvas = Canvas(window, bg="gray", width=dimensions[0], height=dimensions[1])
canvas.pack(side="top")  # Position the canvas at the top of the window

def read_text_file(file_path):
    with open(file_path, 'r') as file:
        content = file.read()
    return content


def move_text(text_object, xdelta=2, framedelay=20):
    tcoords = canvas.coords(text_object)
    canvas.coords(text_object, tcoords[0] + xdelta, tcoords[1])  # Move from left to right
    if tcoords[0] >= dimensions[0]:
        canvas.coords(text_object, 0, tcoords[1])  # Reset to the left edge when reaching the right edge
    # callback
    window.after(framedelay, move_text, text_object, xdelta, framedelay)
    return text_object


if __name__ == "__main__":
    mytext = "Maximum Effort"
    text_object = canvas.create_text(dimensions[0]/2, dimensions[1]/2, text=mytext, font=("arial", 10), fill="white", anchor=CENTER)
    frame_rate = 20
    xdelta = 2

    # Start the continuous animation
    move_text(text_object, xdelta, frame_rate)

    window.mainloop()
