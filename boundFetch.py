import random
import numpy as np
import csv
import time
import os
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.common.exceptions import TimeoutException

# Set up the Firefox options and WebDriver
service = FirefoxService()
options = FirefoxOptions()
options.add_argument('-headless')  # Uncomment if you run in headless mode
options.add_argument("--window-size=1920,1080") # Not sure if setting the window at full screen shits out, or if we need to make the window smaller
driver = webdriver.Firefox(service=service, options=options)

# Open the webpage
driver.get('https://www.nba.com/stats/players/rebounding?LastNGames=1')


# Wait for the table data to load
table = WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.CLASS_NAME, 'nba-stats-content-block'))
)
headers = table.find_elements(By.TAG_NAME, 'th')
header_titles = [header.text for header in headers]

if len(header_titles) > 9:
    header_titles[9] = "SecondaryAssists"
if len(header_titles) > 10:
    header_titles[10] = "PotentialAssists"
if len(header_titles) > 11:
    header_titles[11] = "AssistPointsCreated"



driver.get_screenshot_as_file("debug_screenshot.png")

xbutton = WebDriverWait(driver,3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".onetrust-close-btn-handler.onetrust-close-btn-ui.banner-close-button.ot-close-icon")))
xbutton.click()
# Sort the table by clicking on the table header (sorting by rebounds)
header_to_sort = driver.find_element(By.XPATH, '//th[text()="REB"]')

header_to_sort = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, '//th[text()="REB"]')))

# using numpy & random to vary wait times cuz why not
time.sleep(np.random.uniform(5.0,6.0))  # Allow time for any elements that are loafing it
header_to_sort = driver.find_element(By.XPATH, '//th[text()="REB"]')
time.sleep(np.random.uniform(0.8,1.1))
header_to_sort.click()
time.sleep(np.random.uniform(2.75,3.25))  # If this takes any longer than 3 seconds you're a certified dial-up warrior

# Locate the dropdown by the known prefix of the class name
dropdown_prefix = "DropDown_select"  # This is the consistent prefix of the class name
dropdown = WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.XPATH, f"//select[starts-with(@class, '{dropdown_prefix}')]"))
)

# Select the 'All' option by its value
all_option_value = "-1"
all_option = dropdown.find_element(By.XPATH, f"//option[@value='{all_option_value}']")
all_option.click()

# Get the current date and time for the filename
current_time = datetime.now()
timestamp = current_time.strftime("%Y%m%d_%H%M%S")

# save .csv files to archive folder
directory = "ReboundsArchive"
if not os.path.exists(directory):
    os.makedirs(directory)
    print(f"Directory '{directory}' created")

# Open a new CSV file with the timestamp in the name to save the data
filename = f'ReboundsArchive/rebounding_data_{timestamp}.csv'
table = WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.CLASS_NAME, 'nba-stats-content-block')))
with open(filename, 'w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(header_titles)  # Write the headers to the CSV
    rows = table.find_elements(By.TAG_NAME, 'tr')
    for row in rows:
        # Get all the columns for the row
        cols = row.find_elements(By.TAG_NAME, 'td')
        # Write the columns to the CSV
        writer.writerow([col.text for col in cols])

# Close the driver
driver.quit()

