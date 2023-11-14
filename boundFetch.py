import csv
import time
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions

# Set up the Firefox options and WebDriver
service = FirefoxService()
options = FirefoxOptions()
# options.add_argument('-headless')  # Uncomment if you run in headless mode
driver = webdriver.Firefox(service=service, options=options)

# Open the webpage
driver.get('https://www.nba.com/stats/players/rebounding?LastNGames=1')

# Wait for the table data to load
table = WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.CLASS_NAME, 'nba-stats-content-block'))
)
headers = table.find_elements(By.TAG_NAME, 'th')
header_titles = [header.text for header in headers]

# Sort the table by clicking on the table header (sorting by rebounds)
header_to_sort = driver.find_element(By.XPATH, '//th[text()="REB"]')
header_to_sort.click()
time.sleep(1)  # Replace with a more reliable wait condition

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

# Open a new CSV file with the timestamp in the name to save the data
filename = f'rebounding_data_{timestamp}.csv'
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

