#!/bin/sh
set -x
cd ~/PycharmProjects/EffortScraper || exit

# Activate virtual environment
source venv/bin/activate

# Change to the dimeFetch directory
cd dimeFetch

# Run the Python script
python3 AssistFetch.py

# Deactivate the virtual environment
deactivate
