#!/bin/bash
# cronjob to run boundFetch
# BoundFetch output is in boundFetch/ReboundsArchive/

source ~/Desktop/EffortScraper/venv/bin/activate
cd ~/Desktop/EffortScraper/boundFetch/ || exit

# logging scrape
date >> ~/Desktop/EffortScraper/cronjob_success
python3 ~/Desktop/EffortScraper/boundFetch/boundFetch.py >> ~/Desktop/EffortScraper/boundFetch/cronjob_success
