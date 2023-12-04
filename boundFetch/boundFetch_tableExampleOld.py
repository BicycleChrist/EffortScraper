from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Set up the WebDriver (make sure to specify the correct path to your driver)
driver = webdriver.Firefox()

# Open the webpage
driver.get('https://www.nba.com/stats/players/rebounding')

# Wait for the table to load
WebDriverWait(driver, 10).until(
    EC.presence_of_element_located((By.CLASS_NAME, 'nba-stats-content-block'))
)

# Locate the table by class name (or other identifier)
table = driver.find_element(By.CLASS_NAME, 'nba-stats-content-block')

# Retrieve the table HTML
table_html = table.get_attribute('outerHTML')

# You can now use BeautifulSoup to parse the table HTML if needed
# from bs4 import BeautifulSoup
# soup = BeautifulSoup(table_html, 'html.parser')
# Do something with the BeautifulSoup object...

# Don't forget to close the driver
driver.quit()

# If you want to save the table HTML to a file
with open('stats_table.html', 'w', encoding='utf-8') as f:
    f.write(table_html)
