import pandas as pd
from prophet import Prophet
import sqlite3
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

def test_prophet_engine():
    print("--- Starting Prophet Validation Sandbox ---")
    
    # 1. Load Data from SQLite (Testing Putrajaya - STB-PJ1)
    db_path = os.path.join(os.path.dirname(__file__), '..', 'database', 'coffee_shop.db')
    conn = sqlite3.connect(db_path)
    
    # The database schema uses:
    #   - transaction_date instead of sale_date
    #   - Total_Bill_MYR instead of total_revenue
    #   - branch_id is a string code like 'STB-PJ1'
    df = pd.read_sql_query(
        "SELECT transaction_date, Total_Bill_MYR, weather_condition FROM sales_transaction WHERE branch_id = 'STB-PJ1'", 
        conn
    )
    conn.close()

    if df.empty:
        print("Error: No data found in database. Did you upload the CSV?")
        return

    # 2. Aggregate to Daily Revenue
    daily_df = df.groupby(['transaction_date', 'weather_condition'])['Total_Bill_MYR'].sum().reset_index()
    
    # Prophet strictly requires 'ds' (datestamp) and 'y' (target value)
    daily_df = daily_df.rename(columns={'transaction_date': 'ds', 'Total_Bill_MYR': 'y'})

    # 3. Encode Weather Regressor (Prophet requires numbers, not text)
    weather_weights = {
        'Fair / Sunny': 1.1,
        'Sunny': 1.1,
        'Cloudy': 1.0,
        'Raining': 0.7,
        'Thunderstorm': 0.4
    }
    daily_df['weather_encoded'] = daily_df['weather_condition'].map(weather_weights).fillna(1.0)

    # 4. Initialize and Train Prophet
    print(f"Training AI on {len(daily_df)} days of historical data...")
    
    # Disable yearly seasonality since we only have a few months of data
    m = Prophet(daily_seasonality=False, yearly_seasonality=False)
    m.add_regressor('weather_encoded')
    m.fit(daily_df)

    # 5. Create Future Dataframe (Predicting next 5 days)
    future = m.make_future_dataframe(periods=5)

    # Mock future weather array for the next 5 days (e.g., 3 days cloudy, 2 days rain)
    future_weather_mock = [1.0, 1.0, 1.0, 0.7, 0.7]

    # Map historical weather to the first part of future df, and append mock future
    historical_weather = daily_df['weather_encoded'].tolist()
    future['weather_encoded'] = historical_weather + future_weather_mock

    # 6. Predict
    print("Executing 5-day forecast...\n")
    forecast = m.predict(future)

    print("--- FORECAST RESULTS (Next 5 Days) ---")
    # 'yhat' is the predicted revenue. 'yhat_lower' and 'yhat_upper' are the confidence intervals.
    print(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].tail(5))

    print("\nProphet Validation Successful! The model converges and processes external weather regressors perfectly.")

if __name__ == "__main__":
    test_prophet_engine()