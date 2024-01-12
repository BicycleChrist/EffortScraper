import nfl_data_py as nfl
import os

# Set the year for the current season
current_year = 2019

# Import data for the current year
weekly_data = nfl.import_weekly_data(years=[current_year])
season_data = nfl.import_seasonal_data(years=[current_year])
pbp_data = nfl.import_pbp_data(years=[current_year])

# Path to the 'newdata' directory within the current directory
data_directory = './newdata'

# If the 'newdata' directory doesn't exist, create it
if not os.path.exists(data_directory):
    os.makedirs(data_directory)

# Define the full paths to save the CSV files
weekly_csv_file_path = os.path.join(data_directory, f'weekly_data_{current_year}_{current_year+1}.csv')
season_csv_file_path = os.path.join(data_directory, f'season_data_{current_year}_{current_year+1}.csv')
pbp_csv_file_path = os.path.join(data_directory, f'pbp_data_{current_year}_{current_year+1}.csv')

# Save the DataFrames to CSV files
weekly_data.to_csv(weekly_csv_file_path, index=False)
season_data.to_csv(season_csv_file_path, index=False)
pbp_data.to_csv(pbp_csv_file_path, index=False)

# Print out the paths to confirm where the files are saved
print(f"Weekly data saved to {weekly_csv_file_path}")
print(f"Season data saved to {season_csv_file_path}")
print(f"PBP data saved to {pbp_csv_file_path}")
