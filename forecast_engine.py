import pandas as pd
from prophet import Prophet
import sqlite3
import os
import numpy as np
from weather_api import fetch_future_weather
from sklearn.metrics import mean_absolute_percentage_error, mean_squared_error
from datetime import datetime, timedelta

# ============================================================
#   MALAYSIA PUBLIC HOLIDAYS & DYNAMIC OPERATIONAL PATTERNS
# ============================================================
MY_PUBLIC_HOLIDAYS = [
    # 2025 Base Events
    ("2025-01-01", "New Year's Day"),
    ("2025-01-29", "Chinese New Year"),
    ("2025-02-01", "Federal Territory Day"),
    ("2025-03-31", "Hari Raya Aidilfitri"),
    ("2025-05-01", "Labour Day"),
    ("2025-05-12", "Wesak Day"),
    ("2025-06-02", "Agong Birthday"),
    ("2025-06-07", "Hari Raya Aidiladha"),
    ("2025-08-31", "Merdeka Day"),
    ("2025-09-16", "Malaysia Day"),
    ("2025-10-02", "Deepavali"),
    ("2025-12-25", "Christmas Day"),
    # 2026 Base Events
    ("2026-01-01", "New Year's Day"),
    ("2026-02-01", "Federal Territory Day"),
    ("2026-02-17", "Chinese New Year"),
    ("2026-03-21", "Hari Raya Aidilfitri"),
    ("2026-05-01", "Labour Day"),
    ("2026-05-27", "Hari Raya Aidiladha"),
    ("2026-08-31", "Merdeka Day"),
    ("2026-09-16", "Malaysia Day"),
    ("2026-10-21", "Deepavali"),
    ("2026-12-25", "Christmas Day"),
]

MY_SEASONS = [
    ("2026-03-14", "2026-03-22", "School Holidays March"),
    ("2026-05-30", "2026-06-14", "School Holidays June"),
    ("2026-08-15", "2026-08-23", "School Holidays August"),
    ("2026-11-14", "2026-12-31", "Year-End School Holidays"),
    ("2026-02-18", "2026-03-19", "Ramadan"),
]

BRANCH_PERSONAS = {
    "Putrajaya": {
        "description":    "Government & office workers (Peak demand during weekdays)",
        "holiday_effect": -0.35,
        "coords":         {"lat": 2.9264, "lon": 101.6964}
    },
    "Puncak Alam": {
        "description":    "University of UITM students & residents (Peak demand during weekends/holidays)",
        "holiday_effect": +0.15,
        "coords":         {"lat": 3.2353, "lon": 101.4243}
    }
}


class ForecastEngine:
    def __init__(self):
        self.db_path         = os.path.join('database', 'coffee_shop.db')
        self.weather_weights = {'Sunny': 1, 'Cloudy': 1, 'Raining': 0}

    # ----------------------------------------------------------------
    def _build_promo_and_closure_maps(self):
        """Builds static lookup maps for custom operational rules across 2025-2026"""
        closed_dates = set()
        promo_dates = {}

        # Mapped anchors as per client business specifications
        events = {
            "Raya_Fitri_2025": ("2025-03-31", 3, 3), # Anchor, Closure Window, Post-Promo Window
            "Raya_Adha_2025":  ("2025-06-07", 3, 3),
            "Raya_Fitri_2026": ("2026-03-21", 3, 3),
            "Raya_Adha_2026":  ("2026-05-27", 3, 3),
            "CNY_2025":        ("2025-01-29", 0, 2), # 0 Days Closed, 2 Days Campaign
            "Deepavali_2025":  ("2025-10-02", 0, 2),
            "CNY_2026":        ("2026-02-17", 0, 2),
            "Deepavali_2026":  ("2026-10-21", 0, 2)
        }

        for name, (start_str, close_w, promo_w) in events.items():
            base_dt = datetime.strptime(start_str, "%Y-%m-%d")
            
            # Map structural operational closures
            for c in range(close_w):
                c_date = (base_dt + timedelta(days=c)).strftime("%Y-%m-%d")
                closed_dates.add(c_date)
                
            # Map marketing discount intervals immediately trailing closures
            start_promo_offset = close_w
            for p in range(promo_w):
                p_date = (base_dt + timedelta(days=start_promo_offset + p)).strftime("%Y-%m-%d")
                label = "3-Day Post-Raya Campaign" if "Raya" in name else "2-Day Festive Promo"
                promo_dates[p_date] = label

        return closed_dates, promo_dates

    # ----------------------------------------------------------------
    def _build_holidays_df(self):
        records = []
        for ds, name in MY_PUBLIC_HOLIDAYS:
            records.append({
                'holiday':      name,
                'ds':           pd.Timestamp(ds),
                'lower_window': 0,
                'upper_window': 0
            })
        for start, end, name in MY_SEASONS:
            for d in pd.date_range(start=start, end=end, freq='D'):
                records.append({
                    'holiday':      name,
                    'ds':           d,
                    'lower_window': 0,
                    'upper_window': 0
                })
        return pd.DataFrame(records)

    # ----------------------------------------------------------------
    def _add_promotion_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['friday_promo'] = (df['ds'].dt.dayofweek == 4).astype(int)
        
        _, promo_map = self._build_promo_and_closure_maps()
        df['seasonal_promo'] = df['ds'].dt.strftime('%Y-%m-%d').isin(promo_map).astype(int)
        return df

    # ----------------------------------------------------------------
    def _get_historical_data(self, branch_id: int):
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query(
            "SELECT sale_date, weather_condition, total_revenue "
            "FROM sales_transaction WHERE branch_id = ?",
            conn, params=(branch_id,)
        )
        conn.close()

        if df.empty:
            return None

        dominant = (
            df.groupby(['sale_date', 'weather_condition'])
              .size().reset_index(name='cnt')
              .sort_values('cnt', ascending=False)
              .drop_duplicates('sale_date')[['sale_date', 'weather_condition']]
        )
        daily_rev = df.groupby('sale_date')['total_revenue'].sum().reset_index()
        daily_df  = pd.merge(daily_rev, dominant, on='sale_date')
        daily_df  = daily_df.rename(columns={'sale_date': 'ds', 'total_revenue': 'y'})
        daily_df['weather_encoded'] = daily_df['weather_condition'].map(self.weather_weights).fillna(0)
        daily_df['ds'] = pd.to_datetime(daily_df['ds'])
        daily_df = self._add_promotion_features(daily_df)
        
        daily_df['is_weekday'] = (daily_df['ds'].dt.dayofweek < 5).astype(int)
        daily_df['is_weekend'] = (daily_df['ds'].dt.dayofweek >= 5).astype(int)

        return daily_df.sort_values('ds').reset_index(drop=True)

    # ----------------------------------------------------------------
    def _get_hourly_data(self, branch_id: int) -> list:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT CAST(substr(transaction_time, 1, instr(transaction_time, ':')-1) AS INTEGER) as hr,
                   SUM(total_revenue) as rev, COUNT(*) as txns
            FROM sales_transaction WHERE branch_id = ? AND instr(transaction_time, ':') > 0
            GROUP BY hr ORDER BY hr ASC
        """, (branch_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{'hour': r[0], 'revenue': round(r[1], 2), 'transactions': r[2]} for r in rows if r[0] is not None]

    # ----------------------------------------------------------------
    def _get_weather_by_time(self, branch_id: int) -> list:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                CASE
                    WHEN CAST(substr(transaction_time, 1, 2) AS INTEGER) BETWEEN 9 AND 11 THEN 'Morning'
                    WHEN CAST(substr(transaction_time, 1, 2) AS INTEGER) BETWEEN 12 AND 16 THEN 'Afternoon'
                    ELSE 'Evening'
                END as shift, weather_condition, COUNT(*) as cnt
            FROM sales_transaction WHERE branch_id = ? GROUP BY shift, weather_condition
        """, (branch_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{'shift': r[0], 'weather': r[1], 'count': r[2]} for r in rows]

    # ----------------------------------------------------------------
    def _get_forecast_vs_actual(self, branch_id: int, model, df: pd.DataFrame, fit_cols: list) -> list:
        hist = df.tail(90).copy().reset_index(drop=True)
        fitted = model.predict(hist[fit_cols]).reset_index(drop=True)
        result = []
        for i in range(len(hist)):
            yhat = fitted.loc[i, 'yhat'] if i < len(fitted) else 0.0
            result.append({
                'ds':        hist.loc[i, 'ds'].strftime('%Y-%m-%d'),
                'predicted': round(max(0.0, float(yhat)), 2),
                'actual':    round(float(hist.loc[i, 'y']), 2)
            })
        return result

    # ----------------------------------------------------------------
    def _calculate_metrics(self, model, df: pd.DataFrame) -> tuple:
        forecast = model.predict(df)
        y_true   = df['y'].values
        y_pred   = forecast['yhat'].values
        mask     = y_true != 0
        mape     = mean_absolute_percentage_error(y_true[mask], y_pred[mask])
        rmse     = np.sqrt(mean_squared_error(y_true, y_pred))
        accuracy = max(0, round((1 - mape) * 100, 1))
        return round(mape * 100, 2), round(rmse, 2), accuracy

    # ================================================================
    #   MAIN GENERATE FORECAST
    # ================================================================
    def generate_7_day_forecast(self, branch_id: int, branch_name: str) -> tuple:
        persona = BRANCH_PERSONAS.get(branch_name, BRANCH_PERSONAS["Putrajaya"])
        closed_set, promo_map = self._build_promo_and_closure_maps()

        df = self._get_historical_data(branch_id)
        if df is None or df.empty:
            return False, f"No historical data found for {branch_name}."

        holidays_df = self._build_holidays_df()

        m = Prophet(
            daily_seasonality=False, yearly_seasonality=False, weekly_seasonality=True,
            interval_width=0.95, seasonality_mode='multiplicative', holidays=holidays_df
        )
        
        # IMPROVEMENT: Explicit prior scaling to protect model from regressor overfitting variance
        m.add_regressor('weather_encoded', standardize=True)
        m.add_regressor('friday_promo',    standardize=False, prior_scale=5.0)
        m.add_regressor('seasonal_promo',  standardize=False, prior_scale=5.0)
        m.add_regressor('is_weekday',      standardize=False)
        m.add_regressor('is_weekend',      standardize=False)

        fit_cols = ['ds', 'y', 'weather_encoded', 'friday_promo', 'seasonal_promo', 'is_weekday', 'is_weekend']
        m.fit(df[fit_cols])

        mape, rmse, accuracy = self._calculate_metrics(m, df[fit_cols])

        api_success, weather_data = fetch_future_weather()
        branch_future_weather = weather_data.get(branch_name, {}) if api_success else {}

        future       = m.make_future_dataframe(periods=7)
        future['ds'] = pd.to_datetime(future['ds'])
        
        # IMPROVEMENT: Clear column clash potential before rebuilding regressors dynamically
        if 'weather_encoded' in future.columns:
            future = future.drop(columns=['weather_encoded'])
            
        future = pd.merge(future, df[['ds', 'weather_encoded']], on='ds', how='left')
        future = self._add_promotion_features(future)

        future_weather_labels = {}
        for idx, row in future.iterrows():
            if pd.isna(row['weather_encoded']):
                date_str = row['ds'].strftime('%Y-%m-%d')
                condition = branch_future_weather.get(date_str, 'Cloudy')
                future.at[idx, 'weather_encoded'] = self.weather_weights.get(condition, 0)
                future_weather_labels[date_str] = condition

        future['weather_encoded'] = future['weather_encoded'].fillna(0)
        future['is_weekday']      = (future['ds'].dt.dayofweek < 5).astype(int)
        future['is_weekend']      = (future['ds'].dt.dayofweek >= 5).astype(int)

        forecast = m.predict(future)
        last_hist_date = df['ds'].max()
        
        # Database clear & save sequence
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sales_forecast WHERE branch_id = ?", (branch_id,))

        fore_payload = []
        for _, row in forecast.iterrows():
            date_str = row['ds'].strftime('%Y-%m-%d')
            is_future = row['ds'] > last_hist_date
            
            is_sunday = (row['ds'].dayofweek == 6)
            is_closed_holiday = date_str in closed_set
            
            # Apply hard operational drop thresholds (RM 0.00 for closures)
            if is_sunday or is_closed_holiday:
                adj_yhat = 0.0
                yhat_lower = 0.0
                yhat_upper = 0.0
            else:
                adj_yhat = max(0.0, row['yhat'])
                if date_str in {h[0] for h in MY_PUBLIC_HOLIDAYS}:
                    adj_yhat = max(0.0, adj_yhat * (1 + persona['holiday_effect']))
                yhat_lower = max(0.0, row['yhat_lower'])
                yhat_upper = max(0.0, row['yhat_upper'])

            # Save ALL rows to database for monthly report "Predicted vs Actual" support
            cursor.execute("""
                INSERT INTO sales_forecast (forecast_date, branch_id, predicted_revenue, lower_bound_revenue, upper_bound_revenue)
                VALUES (?, ?, ?, ?, ?)
            """, (date_str, branch_id, round(adj_yhat, 2), round(yhat_lower, 2), round(yhat_upper, 2)))

            # Only add to the 7-day payload if it's in the future window
            if is_future and len(fore_payload) < 7:
                weather   = future_weather_labels.get(date_str, 'Cloudy')
                is_friday = (row['ds'].dayofweek == 4)
                season    = promo_map.get(date_str, None)
                
                fore_payload.append({
                    'ds':         date_str,
                    'yhat':       round(adj_yhat, 2),
                    'yhat_lower': round(yhat_lower, 2),
                    'yhat_upper': round(yhat_upper, 2),
                    'weather':    weather,
                    'is_holiday': date_str in {h[0] for h in MY_PUBLIC_HOLIDAYS},
                    'is_friday':  is_friday,
                    'season':     season,
                    'promotions': [season] if season else ([] if not is_friday else ["Friday: 20% off all Lattes"])
                })

        conn.commit()
        conn.close()

        hist_payload = [{'ds': r['ds'].strftime('%Y-%m-%d'), 'y': round(r['y'], 2), 'weather': r['weather_condition']} for _, r in df.tail(90).iterrows()]
        hourly = self._get_hourly_data(branch_id)
        wbt    = self._get_weather_by_time(branch_id)
        fva    = self._get_forecast_vs_actual(branch_id, m, df, fit_cols)

        return True, {
            'mape': mape, 'rmse': rmse, 'accuracy': accuracy, 'persona': persona['description'],
            'historical': hist_payload, 'forecast': fore_payload, 'hourly': hourly, 'forecast_vs_actual': fva, 'weather_by_time': wbt
        }