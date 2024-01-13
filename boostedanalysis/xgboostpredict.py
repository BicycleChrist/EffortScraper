import pandas as pd
from sklearn.model_selection import train_test_split, cross_val_score, KFold
from sklearn.impute import SimpleImputer
from xgboost import XGBRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import numpy as np
import joblib
import pathlib
from somepaths import basefolder

#cwd = pathlib.Path().cwd()
file_path = (basefolder / 'nflrip/newdata/nflgeniepredictwtf.csv')
xgb_regressor = joblib.load((basefolder / 'boostedanalysis/xgb_regressor.joblib'))
df = pd.read_csv(file_path)
target = 'receiving_yards'
y = df[target]

# Select only the relevant independent variables
include_cols = ['targets', 'target_share', 'air_yards_share', 'wopr']
X = df[include_cols]

# Use the trained XGBoost model to make predictions for the new data
predictions = xgb_regressor.predict(X)

with open((basefolder / 'nflrip/newdata/genieresult.csv'), 'w', encoding='utf-8') as file:
    for thing in predictions:
        file.write(str(thing))
        file.write('\n')


# Print the predictions
print("predictions: ")
print(predictions)
print("actual: ")
print(y)

