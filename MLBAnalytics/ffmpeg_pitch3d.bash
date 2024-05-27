#!/bin/bash
ffmpeg -framerate 60 -pattern_type glob -i 'savedfiles_BBSavant/pitch3d/*.png' -c:v libx264 -pix_fmt yuv420p 'savedfiles_BBSavant/pitch3d.mp4'
