import pathlib
import random
from tkinter import *

window = Tk()
window.title("tickertape")
dimensions = [1915, 20]

canvas = Canvas(window, bg="gray", width=dimensions[0], height=dimensions[1])
canvas.pack(side="top")  # Position the canvas at the top of the window

def read_text_file(leagueselect):
    cwd = pathlib.Path.cwd()
    file_path = cwd / "tickertape_outputs" / f"{leagueselect}.txt"
    with open(file_path, 'r') as file:
        content = file.readlines()
    return content

def move_text(text_object, lines, line_index, xdelta=2, framedelay=50):
    tcoords = canvas.coords(text_object)
    canvas.coords(text_object, tcoords[0] + xdelta, tcoords[1])  # Move from left to right

    text_width = canvas.bbox(text_object)[2] - canvas.bbox(text_object)[0]

    if tcoords[0] >= dimensions[0] - text_width:
        line_index += 1
        if line_index >= len(lines) or lines[line_index].strip() == "":
            line_index = 0  # Restart when reaching the end or a blank line
        next_line = lines[line_index].strip()
        canvas.itemconfig(text_object, text=next_line)

        # Move the text object back to the left edge to continue scrolling
        canvas.coords(text_object, -text_width, tcoords[1])

    # Adjust framedelay based on the length of the text
    adjusted_framedelay = int(framedelay * (text_width / dimensions[0]))

    # callback
    window.after(adjusted_framedelay, move_text, text_object, lines, line_index, xdelta, framedelay)


if __name__ == "__main__":
    lines = read_text_file("NFL")

    initial_text = lines[0].strip() if lines and lines[0].strip() != "" else "Default Text"
    text_object = canvas.create_text(0, dimensions[1]/2, text=initial_text, font=("arial", 10), fill="white", anchor=W)
    frame_rate = 50
    xdelta = 2

    # Start the continuous animation
    move_text(text_object, lines, 0, xdelta, frame_rate)

    window.mainloop()
