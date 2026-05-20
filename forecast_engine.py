import pandas as pd
from prophet import Prophet
import sqlite3
import os
import numpy as np
from weather_api import fetch_future_weather
from sklearn.metrics import mean_absolute_percentage_error, mean_squared_error
from datetime import datetime, timedelta

# ============================================================
#   MALAYSIA PUBLIC HOLIDAYS 2025-2026
# ============================================================
MY_PUBLIC_HOLIDAYS = [
    # 2025
    ("2025-01-01", "New Year's Day"),
    ("2025-01-29", "Chinese New Year"),
    ("2025-01-30", "Chinese New Year Day 2"),
    ("2025-02-01", "Federal Territory Day"),
    ("2025-03-31", "Hari Raya Aidilfitri"),
    ("2025-04-01", "Hari Raya Aidilfitri Day 2"),
    ("2025-05-01", "Labour Day"),
    ("2025-05-12", "Wesak Day"),
    ("2025-06-02", "Agong Birthday"),
    ("2025-06-07", "Hari Raya Aidiladha"),
    ("2025-06-27", "Awal Muharram"),
    ("2025-08-31", "Merdeka Day"),
    ("2025-09-05", "Nabi Muhammad Birthday"),
    ("2025-09-16", "Malaysia Day"),
    ("2025-10-02", "Deepavali"),
    ("2025-12-25", "Christmas Day"),
    # 2026
    ("2026-01-01", "New Year's Day"),
    ("2026-02-01", "Federal Territory Day"),
    ("2026-02-17", "Chinese New Year"),
    ("2026-02-18", "Chinese New Year Day 2"),
    ("2026-03-21", "Hari Raya Aidilfitri"),
    ("2026-03-22", "Hari Raya Aidilfitri Day 2"),
    ("2026-05-01", "Labour Day"),
    ("2026-05-27", "Hari Raya Aidiladha"),
    ("2026-06-01", "Agong Birthday"),
    ("2026-08-17", "Awal Muharram"),
    ("2026-08-31", "Merdeka Day"),
    ("2026-09-16", "Malaysia Day"),
    ("2026-10-21", "Deepavali"),
    ("2026-10-26", "Nabi Muhammad Birthday"),
    ("2026-12-25", "Christmas Day"),
]

# Malaysian seasons / school holiday periods
MY_SEASONS = [
    ("2026-03-14", "2026-03-22", "School Holidays March"),
    ("2026-05-30", "2026-06-14", "School Holidays June"),
    ("2026-08-15", "2026-08-23", "School Holidays August"),
    ("2026-11-14", "2026-12-31", "Year-End School Holidays"),
    ("2026-02-18", "2026-03-19", "Ramadan"),
    ("2026-02-14", "2026-02-20", "Chinese New Year Season"),
    ("2026-03-19", "2026-03-25", "Hari Raya Season"),
]

# Branch demand personas
BRANCH_PERSONAS = {
    "Putrajaya": {
        "description":    "Government & office workers (Peak demand during weekdays)",
        "holiday_effect": -0.35,
        "ramadan_effect": +0.20,
        "season_effect":  +0.10,
        "coords":         {"lat": 2.9264, "lon": 101.6964}
    },
    "Puncak Alam": {
        "description":    "University of UITM students & residents (Peak demand during weekends/holidays)",
        "holiday_effect": +0.15,
        "ramadan_effect": +0.25,
        "season_effect":  +0.20,
        "coords":         {"lat": 3.2353, "lon": 101.4243}
    }
}


class ForecastEngine:
    def __init__(self):
        self.db_path         = os.path.join('database', 'coffee_shop.db')
        self.weather_weights = {'Sunny': 1, 'Cloudy': 1, 'Raining': 0}

    # ----------------------------------------------------------------
    def _build_holidays_df(self):
        records = []
        for ds, name in MY_PUBLIC_HOLIDAYS:
            records.append({
                'holiday':      name,
                'ds':           pd.Timestamp(ds),
                'lower_window': -1,
                'upper_window': 1
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

        season_dates = set()
        for start, end, _ in MY_SEASONS:
            d = datetime.strptime(start, '%Y-%m-%d')
            e = datetime.strptime(end,   '%Y-%m-%d')
            while d <= e:
                season_dates.add(d.strftime('%Y-%m-%d'))
                d += timedelta(days=1)

        df['seasonal_promo'] = (
            df['ds'].dt.strftime('%Y-%m-%d').isin(season_dates).astype(int)
        )
        return df

    # ----------------------------------------------------------------
    def _get_historical_data(self, branch_id: int):
        conn = sqlite3.connect(self.db_path)
        df   = pd.read_sql_query(
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
        daily_df['weather_encoded'] = (
            daily_df['weather_condition'].map(self.weather_weights).fillna(0)
        )
        daily_df['ds']         = pd.to_datetime(daily_df['ds'])
        daily_df               = self._add_promotion_features(daily_df)
        daily_df['is_weekday'] = (daily_df['ds'].dt.dayofweek < 5).astype(int)
        daily_df['is_weekend'] = (daily_df['ds'].dt.dayofweek >= 5).astype(int)

        return daily_df.sort_values('ds').reset_index(drop=True)

    # ----------------------------------------------------------------
    def _get_hourly_data(self, branch_id: int) -> list:
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                CAST(
                    CASE
                        WHEN instr(transaction_time, ':') > 0
                        THEN substr(transaction_time, 1, instr(transaction_time, ':')-1)
                        ELSE transaction_time
                    END
                AS INTEGER) as hr,
                SUM(total_revenue) as rev,
                COUNT(*) as txns
            FROM sales_transaction
            WHERE branch_id = ?
            GROUP BY hr
            ORDER BY hr ASC
        """, (branch_id,))
        rows = cursor.fetchall()
        conn.close()
        return [
            {'hour': r[0], 'revenue': round(r[1], 2), 'transactions': r[2]}
            for r in rows if r[0] is not None
        ]

    # ----------------------------------------------------------------
    def _get_weather_by_time(self, branch_id: int) -> list:
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                CASE
                    WHEN CAST(substr(transaction_time, 1, 2) AS INTEGER) BETWEEN 9 AND 11 THEN 'Morning'
                    WHEN CAST(substr(transaction_time, 1, 2) AS INTEGER) BETWEEN 12 AND 16 THEN 'Afternoon'
                    ELSE 'Evening'
                END as shift,
                weather_condition,
                COUNT(*) as cnt
            FROM sales_transaction
            WHERE branch_id = ?
            GROUP BY shift, weather_condition
        """, (branch_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{'shift': r[0], 'weather': r[1], 'count': r[2]} for r in rows]

    # ----------------------------------------------------------------
    def _get_forecast_vs_actual(self, branch_id: int, model, df: pd.DataFrame, fit_cols: list) -> list:
        """
        Returns Prophet in-sample fitted values vs actual revenue for the last 90 days.
        This always has data immediately after fitting — no need to wait for future dates.
        """
        hist   = df.tail(90).copy().reset_index(drop=True)
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

    # ----------------------------------------------------------------
    def _get_promotions_for_date(self, date_str, is_friday, season_name):
        promos = []
        if is_friday:
            promos.append("Friday: 20% off all Lattes")
        if season_name:
            promos.append(f"{season_name}: Buy 2 Free 1 on all drinks")
        return promos

    # ================================================================
    #   MAIN — GENERATE 7-DAY FORECAST
    # ================================================================
    def generate_7_day_forecast(self, branch_id: int, branch_name: str) -> tuple:
        print(f"\n--- Initiating Forecast: {branch_name} (branch_id={branch_id}) ---")

        persona = BRANCH_PERSONAS.get(branch_name, BRANCH_PERSONAS["Putrajaya"])

        df = self._get_historical_data(branch_id)
        if df is None or df.empty:
            return False, f"No historical data found for {branch_name}. Upload a CSV first."
        print(f"Loaded {len(df)} days of historical data.")

        holidays_df = self._build_holidays_df()

        print("Training Prophet with Malaysian holidays + promotions...")
        m = Prophet(
            daily_seasonality    = False,
            yearly_seasonality   = False,
            weekly_seasonality   = True,
            interval_width       = 0.95,
            seasonality_mode     = 'multiplicative',
            holidays             = holidays_df,
            holidays_prior_scale = 10.0,
        )
        m.add_regressor('weather_encoded', standardize=True)
        m.add_regressor('friday_promo',    standardize=False)
        m.add_regressor('seasonal_promo',  standardize=False)
        m.add_regressor('is_weekday',      standardize=False)
        m.add_regressor('is_weekend',      standardize=False)

        fit_cols = [
            'ds', 'y', 'weather_encoded', 'friday_promo',
            'seasonal_promo', 'is_weekday', 'is_weekend'
        ]
        m.fit(df[fit_cols])

        mape, rmse, accuracy = self._calculate_metrics(m, df[fit_cols])
        print(f"Accuracy — MAPE: {mape}%  RMSE: RM {rmse}  Overall: {accuracy}%")

        # ── Fetch live weather ──────────────────────────────────────
        print("Fetching live weather forecast...")
        api_success, weather_data = fetch_future_weather()
        branch_future_weather     = weather_data.get(branch_name, {}) if api_success else {}
        if not api_success:
            print("Weather API failed. Defaulting to Cloudy.")

        # ── Build future dataframe ──────────────────────────────────
        future       = m.make_future_dataframe(periods=7)
        future['ds'] = pd.to_datetime(future['ds'])

        future = pd.merge(future, df[['ds', 'weather_encoded']], on='ds', how='left')
        future = self._add_promotion_features(future)

        future_weather_labels = {}
        for idx, row in future.iterrows():
            if pd.isna(row['weather_encoded']):
                date_str  = row['ds'].strftime('%Y-%m-%d')
                condition = branch_future_weather.get(date_str, 'Cloudy')
                future.at[idx, 'weather_encoded'] = self.weather_weights.get(condition, 0)
                future_weather_labels[date_str]   = condition

        future['weather_encoded'] = future['weather_encoded'].fillna(0)
        future['friday_promo']    = future['friday_promo'].fillna(0)
        future['seasonal_promo']  = future['seasonal_promo'].fillna(0)
        future['is_weekday']      = (future['ds'].dt.dayofweek < 5).astype(int)
        future['is_weekend']      = (future['ds'].dt.dayofweek >= 5).astype(int)

        # ── Run prediction ──────────────────────────────────────────
        print("Executing prediction...")
        forecast = m.predict(future)

        # FIX: Filter strictly to dates AFTER the last historical date — guarantees 7 real future rows
        last_hist_date  = df['ds'].max()
        future_forecast = (
            forecast[forecast['ds'] > last_hist_date]
            [['ds', 'yhat', 'yhat_lower', 'yhat_upper']]
            .head(7)
            .copy()
        )
        future_forecast['yhat']       = future_forecast['yhat'].clip(lower=0)
        future_forecast['yhat_lower'] = future_forecast['yhat_lower'].clip(lower=0)

        # ── Holiday / season maps ───────────────────────────────────
        holiday_set = {h[0] for h in MY_PUBLIC_HOLIDAYS}
        season_map  = {}
        for start, end, name in MY_SEASONS:
            d = datetime.strptime(start, '%Y-%m-%d')
            e = datetime.strptime(end,   '%Y-%m-%d')
            while d <= e:
                season_map[d.strftime('%Y-%m-%d')] = name
                d += timedelta(days=1)

        # ── Save to DB ──────────────────────────────────────────────
        print("Saving forecast to database...")
        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sales_forecast WHERE branch_id = ?", (branch_id,))
        for _, row in future_forecast.iterrows():
            cursor.execute("""
                INSERT INTO sales_forecast
                    (forecast_date, branch_id, predicted_revenue,
                     lower_bound_revenue, upper_bound_revenue)
                VALUES (?, ?, ?, ?, ?)
            """, (
                row['ds'].strftime('%Y-%m-%d'), branch_id,
                round(row['yhat'], 2),
                round(row['yhat_lower'], 2),
                round(row['yhat_upper'], 2)
            ))
        conn.commit()
        conn.close()

        # ── Build historical payload (last 90 days) ─────────────────
        hist_data    = df.tail(90)[['ds', 'y', 'weather_condition']].copy()
        hist_payload = [
            {
                'ds':      r['ds'].strftime('%Y-%m-%d'),
                'y':       round(r['y'], 2),
                'weather': r['weather_condition']
            }
            for _, r in hist_data.iterrows()
        ]

        # ── Build 7-day forecast payload ────────────────────────────
        fore_payload = []
        for _, row in future_forecast.iterrows():
            date_str   = row['ds'].strftime('%Y-%m-%d')
            weather    = future_weather_labels.get(date_str, 'Cloudy')
            is_holiday = date_str in holiday_set
            is_friday  = (row['ds'].dayofweek == 4)
            season     = season_map.get(date_str)

            adj_yhat = row['yhat']
            if is_holiday:
                adj_yhat = max(0, adj_yhat * (1 + persona['holiday_effect']))

            fore_payload.append({
                'ds':         date_str,
                'yhat':       round(adj_yhat, 2),
                'yhat_lower': round(row['yhat_lower'], 2),
                'yhat_upper': round(row['yhat_upper'], 2),
                'weather':    weather,
                'is_holiday': is_holiday,
                'is_friday':  is_friday,
                'season':     season,
                'promotions': self._get_promotions_for_date(date_str, is_friday, season)
            })

        # ── Supplementary data ──────────────────────────────────────
        hourly = self._get_hourly_data(branch_id)
        wbt    = self._get_weather_by_time(branch_id)

        # FVA: in-sample fitted vs actual (always has data — last 90 days)
        fva = self._get_forecast_vs_actual(branch_id, m, df, fit_cols)

        print("Forecast complete.")
        return True, {
            'mape':               mape,
            'rmse':               rmse,
            'accuracy':           accuracy,
            'persona':            persona['description'],
            'historical':         hist_payload,
            'forecast':           fore_payload,
            'hourly':             hourly,
            'forecast_vs_actual': fva,
            'weather_by_time':    wbt,
        }