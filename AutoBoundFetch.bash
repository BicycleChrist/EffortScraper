#!/bin/sh
cd /home/retupmoc/PycharmProjects/EffortScraper

# Activate virtual environment
source venv/bin/activate

# Change to the boundFetch directory
cd boundFetch

# Run the Python script
python3 boundFetch.py

# Deactivate the virtual environment
deactivate
