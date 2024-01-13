import pandas as pd
from sklearn.model_selection import train_test_split, cross_val_score, KFold
from sklearn.impute import SimpleImputer
from xgboost import XGBRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import numpy as np
import joblib
import os
from somepaths import basefolder


# Load data
file_path = (basefolder / 'nflrip/newdata/weekly_data_2018_2019.csv')
df = pd.read_csv(file_path)

# Drop non-predictive columns
df.drop(columns=['player_id', 'player_name', 'player_display_name', 'headshot_url','passing_yards'], inplace=True)

# Define the target variable, e.g., 'receiving_yards'
target = 'receiving_yards'
y = df[target]

# Select only the relevant independent variables
include_cols = ['targets', 'target_share', 'air_yards_share', 'wopr']
X = df[include_cols]

# Split the dataset into training and testing sets
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# Initialize the XGBoost regressor
xgb_regressor = XGBRegressor(n_estimators=100, learning_rate=0.1, max_depth=3, random_state=420)

# Train the model on the training set
xgb_regressor.fit(X_train, y_train)

# Predict on the test set
y_pred = xgb_regressor.predict(X_test)
# Evaluate the model
rmse = np.sqrt(mean_squared_error(y_test, y_pred))
mae = mean_absolute_error(y_test, y_pred)
r2 = r2_score(y_test, y_pred)

print(f'Root Mean Squared Error: {rmse}')
print(f'Mean Absolute Error: {mae}')
print(f'R-squared: {r2}')

# Cross-Validation
kfold = KFold(n_splits=5, shuffle=True, random_state=42)
cv_results = cross_val_score(xgb_regressor, X, y, cv=kfold, scoring='neg_root_mean_squared_error')
cv_rmse = np.mean(np.abs(cv_results))

print(f'Cross-Validation RMSE: {cv_rmse}')

# Save the model to a file
joblib.dump(xgb_regressor, 'xgb_regressor.joblib')

# Confirmation message
print("The model has been trained, evaluated, and saved as 'xgb_regressor.joblib'.")
