import tkinter as tk
from moviepy.editor import VideoFileClip
from PIL import Image, ImageTk
import pathlib

# Create Tkinter window
root = tk.Tk()

# Set window size
window_width = 800
window_height = 600

# Calculate window position to center it on the screen
screen_width = root.winfo_screenwidth()
screen_height = root.winfo_screenheight()
x = (screen_width - window_width) // 2
y = (screen_height - window_height) // 2

# Position the window
root.geometry(f"{window_width}x{window_height}+{x}+{y}")

# Remove window decorations
root.overrideredirect(True)

# Determine the directory of the current script
cwd = pathlib.Path.cwd()

# Construct the relative path to the video file
video_path = cwd.parent / 'EffortScraper' / 'Heftyrender.mkv'

# Load the video
video = VideoFileClip(str(video_path))  # Convert Path object to string

# Create a Canvas to display the video
canvas = tk.Canvas(root, width=window_width, height=window_height)
canvas.pack()

# Convert each frame of the video to a PIL image and display it on the Canvas
for frame in video.iter_frames():
    # Convert the frame to a PIL Image
    pil_image = Image.fromarray(frame)

    # Resize the image to fit the window
    pil_image = pil_image.resize((window_width, window_height))

    # Convert the PIL Image to a Tkinter PhotoImage
    tk_image = ImageTk.PhotoImage(pil_image)

    # Display the image on the Canvas
    canvas.create_image(0, 0, anchor=tk.NW, image=tk_image)

    # Update the Tkinter window
    root.update()

# Close the splash screen after 5 seconds (adjust as needed)
root.after(100, root.destroy)

# Run the Tkinter event loop
root.mainloop()
