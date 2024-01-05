import random
from tkinter import *

window = Tk()
window.title("tickertape")

canvas = Canvas(window, bg="gray", width=1915, height=25)
canvas.pack(side="top")  # Position the canvas at the top of the window

def read_text_file(file_path):
    with open(file_path, 'r') as file:
        content = file.read()
    return content

def create_text():
    text_content = create_text = ("Maximum Effort               Maximum Effort ")
    return text_content

def display_text():
    text_content = create_text()
    text_object = canvas.create_text(957, 15, text=text_content, font=("arial", 10), fill="white", anchor=CENTER)  # Center horizontally and vertically
    return text_object

text_object = display_text()

def move_text():
    global text_object
    tcoords = canvas.coords(text_object)

    if tcoords[0] >= 1915:
        canvas.coords(text_object, 0, 15)  # Reset to the left edge when reaching the right edge
    else:
        canvas.coords(text_object, tcoords[0] + 2, tcoords[1])  # Move from left to right

    window.after(20, move_text)

# Start the continuous animation
move_text()

window.mainloop()
