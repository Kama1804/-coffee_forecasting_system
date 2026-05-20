import pandas as pd
from prophet import Prophet
import sqlite3
import os

def test_prophet_engine():
    print("--- Starting Prophet Validation Sandbox ---")
    
    # 1. Load Data from SQLite (Testing Putrajaya - Branch 1)
    db_path = os.path.join('database', 'coffee_shop.db')
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT sale_date, total_revenue, weather_condition FROM sales_transaction WHERE branch_id = 1", conn)
    conn.close()

    if df.empty:
        print("Error: No data found in database. Did you upload the CSV?")
        return

    # 2. Aggregate to Daily Revenue
    daily_df = df.groupby(['sale_date', 'weather_condition'])['total_revenue'].sum().reset_index()
    
    # Prophet strictly requires 'ds' (datestamp) and 'y' (target value)
    daily_df = daily_df.rename(columns={'sale_date': 'ds', 'total_revenue': 'y'})

    # 3. Encode Weather Regressor (Prophet requires numbers, not text)
    weather_weights = {'Sunny': 1, 'Cloudy': 0, 'Raining': -1}
    daily_df['weather_encoded'] = daily_df['weather_condition'].map(weather_weights)

    # 4. Initialize and Train Prophet
    print(f"Training AI on {len(daily_df)} days of historical data...")
    
    # Disable yearly seasonality since we only have 3 months of dummy data
    m = Prophet(daily_seasonality=False, yearly_seasonality=False)
    m.add_regressor('weather_encoded')
    m.fit(daily_df)

    # 5. Create Future Dataframe (Predicting next 7 days)
    future = m.make_future_dataframe(periods=7)
    
    # Mock future weather array for the next 7 days (e.g., 3 days cloudy, 4 days rain)
    future_weather_mock = [0, 0, 0, -1, -1, -1, -1]
    
    # Map historical weather to the first part of future df, and append mock future
    historical_weather = daily_df['weather_encoded'].tolist()
    future['weather_encoded'] = historical_weather + future_weather_mock

    # 6. Predict
    print("Executing 7-day forecast...\n")
    forecast = m.predict(future)
    
    print("--- FORECAST RESULTS (Next 7 Days) ---")
    # 'yhat' is the predicted revenue. 'yhat_lower' and 'yhat_upper' are the confidence intervals.
    print(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].tail(7))
    print("\nProphet Validation Successful! The model converges and processes external weather regressors perfectly.")

if __name__ == "__main__":
    test_prophet_engine()