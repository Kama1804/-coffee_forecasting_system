import os
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from prophet import Prophet
from sklearn.metrics import mean_absolute_percentage_error, mean_squared_error
from weather_api import fetch_future_weather

# ============================================================
#    MALAYSIA PUBLIC HOLIDAYS & DYNAMIC OPERATIONAL PATTERNS
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

    # 2027 Future Events
    ("2027-01-01", "New Year's Day"),
    ("2027-02-01", "Federal Territory Day"),
    ("2027-02-06", "Chinese New Year"),
    ("2027-03-10", "Hari Raya Aidilfitri"),
    ("2027-05-01", "Labour Day"),
    ("2027-05-17", "Hari Raya Aidiladha"),
    ("2027-08-31", "Merdeka Day"),
    ("2027-09-16", "Malaysia Day"),
    ("2027-11-08", "Deepavali"),
    ("2027-12-25", "Christmas Day"),
]

MY_SEASONS = [
    # 2026 School Terms & Ramadan Baseline
    ("2026-03-14", "2026-03-22", "School Holidays March"),
    ("2026-05-30", "2026-06-14", "School Holidays June"),
    ("2026-08-15", "2026-08-23", "School Holidays August"),
    ("2026-11-14", "2026-12-31", "Year-End School Holidays"),
    ("2026-02-18", "2026-03-19", "Ramadan"),

    # 2027 Future School Terms & Ramadan Window
    ("2027-02-07", "2027-03-08", "Ramadan 2027"),
    ("2027-03-13", "2027-03-21", "School Holidays March 2027"),
    ("2027-05-29", "2027-06-13", "School Holidays June 2027"),
    ("2027-08-21", "2027-08-29", "School Holidays August 2027"),
    ("2027-11-20", "2027-12-31", "Year-End School Holidays 2027"),
]

BRANCH_PERSONAS = {
    "Putrajaya": {
        "description": "Government & office workers (Peak demand during weekdays)",
        "holiday_effect": -0.35,
        "coords": {"lat": 2.9264, "lon": 101.6964}
    },
    "Puncak Alam": {
        "description": "University of UiTM students & residents (Peak demand during weekends/holidays)",
        "holiday_effect": +0.15,
        "coords": {"lat": 3.2353, "lon": 101.4243}
    }
}


class ForecastEngine:
    def __init__(self):
        self.db_path = os.path.join('database', 'coffee_shop.db')
        self.weather_weights = {
            'Fair / Sunny': 1.1,  # Best for coffee sales
            'Sunny': 1.1,         # Compatibility
            'Cloudy': 1.0,        # Baseline
            'Raining': 0.7,       # Significant drop
            'Thunderstorm': 0.4   # Severe drop
        }

    # ----------------------------------------------------------------
    def _build_promo_and_closure_maps(self):
        """
        Returns:
          closed_dates : set of date strings where the shop is CLOSED
          promo_dates  : dict of date_str -> plain-English promo label
                         Guaranteed to span exactly promo_window_days of open business days.
        """
        closed_dates = set()
        promo_dates  = {}

        events = {
            # Hari Raya Aidilfitri
            "Raya_Fitri_2025": ("2025-03-31", 3, 3, "Post-Raya Campaign"),
            "Raya_Fitri_2026": ("2026-03-21", 3, 3, "Post-Raya Campaign"),
            "Raya_Fitri_2027": ("2027-03-10", 3, 3, "Post-Raya Campaign"),
            # Hari Raya Aidiladha
            "Raya_Adha_2025":  ("2025-06-07", 3, 3, "Post-Raya Campaign"),
            "Raya_Adha_2026":  ("2026-05-27", 3, 3, "Post-Raya Campaign"),
            "Raya_Adha_2027":  ("2027-05-17", 3, 3, "Post-Raya Campaign"),
            # Chinese New Year
            "CNY_2025":        ("2025-01-29", 0, 2, "CNY Festive Campaign"), # Fixed to 0 closure per your true rule
            "CNY_2026":        ("2026-02-17", 0, 2, "CNY Festive Campaign"), # Fixed to 0 closure per your true rule
            "CNY_2027":        ("2027-02-06", 0, 2, "CNY Festive Campaign"),
            # Deepavali
            "Deepavali_2025":  ("2025-10-02", 0, 2, "Deepavali Campaign"),   # Fixed to 0 closure per your true rule
            "Deepavali_2026":  ("2026-10-21", 0, 2, "Deepavali Campaign"),   # Fixed to 0 closure per your true rule
            "Deepavali_2027":  ("2027-11-08", 0, 2, "Deepavali Campaign"),
        }

        for name, (start_str, close_w, promo_w, label) in events.items():
            base_dt = datetime.strptime(start_str, "%Y-%m-%d")

            # 1. Map Complete Closure Window
            for c in range(close_w):
                c_date = (base_dt + timedelta(days=c)).strftime("%Y-%m-%d")
                closed_dates.add(c_date)

            # 2. Map Promo Window sequentially across active OPEN days only
            current_dt = base_dt + timedelta(days=close_w)
            promos_assigned = 0
            
            while promos_assigned < promo_w:
                date_str = current_dt.strftime("%Y-%m-%d")
                is_sunday = (current_dt.weekday() == 6)
                is_shut = date_str in closed_dates
                
                # Only apply promo if the shop is physically open
                if not is_sunday and not is_shut:
                    promo_dates[date_str] = label
                    promos_assigned += 1
                    
                current_dt += timedelta(days=1)

        return closed_dates, promo_dates

    # ----------------------------------------------------------------
    def _resolve_promos(self, promo_dates: dict, closed_dates: set) -> dict:
        """
        Carry forward any promo that lands on a closed day (festive closure
        OR Sunday) to the next open day.  Returns a new dict with adjusted
        date keys.
        """
        resolved = {}
        for date_str, label in promo_dates.items():
            candidate = datetime.strptime(date_str, "%Y-%m-%d")
            # Shift forward until we land on an open day
            for _ in range(14):   # safety cap — never infinite loop
                c_str   = candidate.strftime("%Y-%m-%d")
                is_sun  = (candidate.weekday() == 6)   # Python weekday: 6=Sunday
                is_shut = c_str in closed_dates
                if not is_sun and not is_shut:
                    break
                candidate += timedelta(days=1)
            resolved[candidate.strftime("%Y-%m-%d")] = label
        return resolved

    # ----------------------------------------------------------------
    def _build_holidays_df(self):
        records = []
        for ds, name in MY_PUBLIC_HOLIDAYS:
            records.append({'holiday': name, 'ds': pd.Timestamp(ds),
                            'lower_window': 0, 'upper_window': 0})
        for start, end, name in MY_SEASONS:
            for d in pd.date_range(start=start, end=end, freq='D'):
                records.append({'holiday': name, 'ds': d,
                                'lower_window': 0, 'upper_window': 0})
        return pd.DataFrame(records)

    # ----------------------------------------------------------------
    def _add_promotion_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['friday_promo'] = (df['ds'].dt.dayofweek == 4).astype(int)
        _, raw_promo_map = self._build_promo_and_closure_maps()
        df['seasonal_promo'] = df['ds'].dt.strftime('%Y-%m-%d').isin(raw_promo_map).astype(int)
        return df

    # ----------------------------------------------------------------
    def _get_historical_data(self, branch_id: str):
        conn = sqlite3.connect(self.db_path)
        query = """
            SELECT transaction_date, weather_condition, Total_Bill_MYR
            FROM sales_transaction
            WHERE branch_id = ? AND Total_Bill_MYR > 0
        """
        df = pd.read_sql_query(query, conn, params=(branch_id.upper().strip(),))
        conn.close()
        if df.empty:
            return None

        dominant = (df.groupby(['transaction_date', 'weather_condition']).size()
                    .reset_index(name='cnt').sort_values('cnt', ascending=False)
                    .drop_duplicates('transaction_date')[['transaction_date', 'weather_condition']])

        daily_rev = df.groupby('transaction_date')['Total_Bill_MYR'].sum().reset_index()
        daily_df  = pd.merge(daily_rev, dominant, on='transaction_date').rename(
            columns={'transaction_date': 'ds', 'Total_Bill_MYR': 'y'})
        daily_df['weather_encoded'] = daily_df['weather_condition'].map(
            self.weather_weights).fillna(0)
        daily_df['ds'] = pd.to_datetime(daily_df['ds'])
        daily_df = self._add_promotion_features(daily_df)
        daily_df['is_weekday'] = (daily_df['ds'].dt.dayofweek < 5).astype(int)
        daily_df['is_weekend'] = (daily_df['ds'].dt.dayofweek >= 5).astype(int)
        return daily_df.sort_values('ds').reset_index(drop=True)

    # ----------------------------------------------------------------
    def _get_hourly_data(self, branch_id: str) -> list:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Hour, SUM(Total_Bill_MYR) as rev, COUNT(*) as txns "
            "FROM sales_transaction WHERE branch_id = ? "
            "GROUP BY Hour ORDER BY Hour ASC",
            (branch_id.upper().strip(),))
        rows = cursor.fetchall()
        conn.close()
        return [{'hour': int(r[0]), 'revenue': round(r[1], 2), 'transactions': r[2]}
                for r in rows if r[0] is not None]

    # ----------------------------------------------------------------
    def _get_weather_by_time(self, branch_id: str) -> list:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # Normalizing 'Fair / Sunny' to 'Sunny' for consistency with dashboard charts
        cursor.execute("""
            SELECT CASE
                WHEN CAST(Hour AS INTEGER) BETWEEN 9 AND 11 THEN 'Morning'
                WHEN CAST(Hour AS INTEGER) BETWEEN 12 AND 16 THEN 'Afternoon'
                ELSE 'Evening'
            END as shift, 
            CASE 
                WHEN weather_condition = 'Fair / Sunny' THEN 'Sunny'
                ELSE weather_condition
            END as weather, 
            COUNT(*) as cnt
            FROM sales_transaction WHERE branch_id = ?
            GROUP BY shift, weather
        """, (branch_id.upper().strip(),))
        rows = cursor.fetchall()
        conn.close()
        return [{'shift': r[0], 'weather': r[1], 'count': r[2]} for r in rows]

    # ----------------------------------------------------------------
    def _get_forecast_vs_actual(self, model, df: pd.DataFrame, fit_cols: list) -> list:
        hist   = df.tail(90).copy().reset_index(drop=True)
        fitted = model.predict(hist[fit_cols]).reset_index(drop=True)
        return [
            {
                'ds':        hist.loc[i, 'ds'].strftime('%Y-%m-%d'),
                'predicted': round(max(0.0, float(fitted.loc[i, 'yhat'])), 2),
                'actual':    round(float(hist.loc[i, 'y']), 2)
            }
            for i in range(len(hist))
        ]

    # ----------------------------------------------------------------
    def _calculate_metrics(self, model, df: pd.DataFrame) -> tuple:
        forecast = model.predict(df)
        y_true, y_pred = df['y'].values, forecast['yhat'].values
        mask = y_true != 0
        mape = mean_absolute_percentage_error(y_true[mask], y_pred[mask])
        rmse = np.sqrt(mean_squared_error(y_true, y_pred))
        return round(mape * 100, 2), round(rmse, 2), max(0, round((1 - mape) * 100, 1))

    # ================================================================
    #    MAIN GENERATE FORECAST
    # ================================================================
    def generate_5_day_forecast(self, branch_id: str, branch_name: str) -> tuple:
        branch_id = str(branch_id).upper().strip()
        persona   = BRANCH_PERSONAS.get(branch_name, BRANCH_PERSONAS["Putrajaya"])

        closed_set, resolved_promo_map = self._build_promo_and_closure_maps()

        df = self._get_historical_data(branch_id)
        if df is None or df.empty:
            return False, f"No historical data found for {branch_name} ({branch_id})."

        holidays_df = self._build_holidays_df()

        m = Prophet(
            daily_seasonality=False, yearly_seasonality=False, weekly_seasonality=True,
            interval_width=0.95, seasonality_mode='multiplicative', holidays=holidays_df
        )
        m.add_regressor('weather_encoded', standardize=True)
        m.add_regressor('friday_promo',    standardize=False, prior_scale=5.0)
        m.add_regressor('seasonal_promo',  standardize=False, prior_scale=5.0)
        m.add_regressor('is_weekday',      standardize=False)
        m.add_regressor('is_weekend',      standardize=False)

        fit_cols = ['ds', 'y', 'weather_encoded', 'friday_promo',
                    'seasonal_promo', 'is_weekday', 'is_weekend']
        m.fit(df[fit_cols])

        mape, rmse, accuracy = self._calculate_metrics(m, df[fit_cols])

        api_success, weather_data = fetch_future_weather()
        branch_future_weather = weather_data.get(branch_name, {}) if api_success else {}

        future       = m.make_future_dataframe(periods=5)
        future['ds'] = pd.to_datetime(future['ds'])

        if 'weather_encoded' in future.columns:
            future = future.drop(columns=['weather_encoded'])
        future = pd.merge(future, df[['ds', 'weather_encoded']], on='ds', how='left')
        future = self._add_promotion_features(future)

        future_weather_labels = {}
        for idx, row in future.iterrows():
            if pd.isna(row['weather_encoded']):
                date_str  = row['ds'].strftime('%Y-%m-%d')
                raw_cond  = branch_future_weather.get(date_str, 'Cloudy')
                # Align with dashboard naming
                condition = 'Sunny' if raw_cond == 'Fair / Sunny' else raw_cond
                
                future.at[idx, 'weather_encoded'] = self.weather_weights.get(raw_cond, 0)
                future_weather_labels[date_str]   = condition

        future['weather_encoded'] = future['weather_encoded'].fillna(0)
        future['is_weekday']      = (future['ds'].dt.dayofweek < 5).astype(int)
        future['is_weekend']      = (future['ds'].dt.dayofweek >= 5).astype(int)

        forecast         = m.predict(future)
        last_hist_date   = df['ds'].max()
        holiday_date_set = {h[0] for h in MY_PUBLIC_HOLIDAYS}

        conn   = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        mix_df = pd.read_sql_query("""
            SELECT product_id, product_detail,
                   SUM(transaction_qty) as total_qty,
                   SUM(Total_Bill_MYR) as total_rev
            FROM sales_transaction
            WHERE branch_id = ? AND transaction_date >= date('now', '-30 day')
            GROUP BY product_id, product_detail
        """, conn, params=(branch_id,))

        total_hist_rev = mix_df['total_rev'].sum() if not mix_df.empty else 1
        if total_hist_rev == 0:
            total_hist_rev = 1
        mix_df['qty_per_rm'] = mix_df['total_qty'] / total_hist_rev

        cursor.execute("DELETE FROM sales_forecast WHERE branch_id = ?", (branch_id,))

        fore_payload              = []
        all_5_day_predicted_items = []

        for _, row in forecast.iterrows():
            date_str = row['ds'].strftime('%Y-%m-%d')
            is_future = row['ds'] > last_hist_date

            # ── Closed-day logic ─────────────────────────────────────────
            is_sunday          = (row['ds'].dayofweek == 6)   # Python: 6=Sunday
            is_festive_closure = date_str in closed_set
            is_closed          = is_sunday or is_festive_closure

            if is_closed:
                adj_yhat   = 0.0
                yhat_lower = 0.0
                yhat_upper = 0.0
            else:
                adj_yhat = max(0.0, row['yhat'])
                if date_str in holiday_date_set:
                    adj_yhat = max(0.0, adj_yhat * (1 + persona['holiday_effect']))
                yhat_lower = max(0.0, row['yhat_lower'])
                yhat_upper = max(0.0, row['yhat_upper'])

            cursor.execute("""
                INSERT INTO sales_forecast
                    (forecast_date, branch_id, predicted_revenue,
                     lower_bound_revenue, upper_bound_revenue)
                VALUES (?, ?, ?, ?, ?)
            """, (date_str, branch_id,
                  round(adj_yhat, 2), round(yhat_lower, 2), round(yhat_upper, 2)))

            if is_future and len(fore_payload) < 5:
                is_friday          = (row['ds'].dayofweek == 4)   # Python: 4=Friday
                is_holiday_active  = date_str in holiday_date_set

                # ── Build promotion list ──────────────────────────────────
                if is_festive_closure:
                    # Shop is shut for a festive event — label it clearly
                    event_name = next(
                        (name for ds, name in MY_PUBLIC_HOLIDAYS if ds == date_str),
                        "Festive Holiday"
                    )
                    promo_list = [f"Shop Closed — {event_name}"]
                elif is_sunday:
                    promo_list = []   # Sunday closed label shown via UI flag
                else:
                    promo_list = []
                    # Use the RESOLVED promo map (already skipped closed days)
                    resolved_label = resolved_promo_map.get(date_str)
                    if resolved_label:
                        promo_list.append(resolved_label)
                    if is_friday:
                        promo_list.append("Friday Promo — 20% off Lattes")

                # ── Weather: blank out for closed days ────────────────────
                if is_closed:
                    weather = None      # signals "no weather" to frontend
                else:
                    weather = future_weather_labels.get(date_str, 'Cloudy')

                # ── Ingredient items for open days only ───────────────────
                day_items = []
                if adj_yhat > 0:
                    for _, m_row in mix_df.iterrows():
                        pred_qty = int(round(m_row['qty_per_rm'] * adj_yhat))
                        if pred_qty > 0:
                            item = {
                                'product_id':     m_row['product_id'],
                                'product_detail': m_row['product_detail'],
                                'quantity':       pred_qty
                            }
                            day_items.append(item)
                            all_5_day_predicted_items.append(item)

                fore_payload.append({
                    'ds':              date_str,
                    'yhat':            round(adj_yhat, 2),
                    'yhat_lower':      round(yhat_lower, 2),
                    'yhat_upper':      round(yhat_upper, 2),
                    'weather':         weather,        # None = closed
                    'is_holiday':      is_holiday_active,
                    'is_friday':       is_friday,
                    'is_closed':       is_closed,
                    'is_festive_close':is_festive_closure,
                    'promotions':      promo_list,
                    'predicted_items': day_items
                })

        conn.commit()
        conn.close()

        hist_payload = [
            {
                'ds':      r['ds'].strftime('%Y-%m-%d'),
                'y':       round(r['y'], 2),
                'weather': 'Sunny' if r['weather_condition'] == 'Fair / Sunny' else r['weather_condition']
            }
            for _, r in df.tail(90).iterrows()
        ]
        hourly = self._get_hourly_data(branch_id)
        wbt    = self._get_weather_by_time(branch_id)
        fva    = self._get_forecast_vs_actual(m, df, fit_cols)

        from analytics import calculate_ingredient_demand
        five_day_ingredient_demand = calculate_ingredient_demand(all_5_day_predicted_items)

        return True, {
            'mape':             mape,
            'rmse':             rmse,
            'accuracy':         accuracy,
            'persona':          persona['description'],
            'historical':       hist_payload,
            'forecast':         fore_payload,
            'hourly':           hourly,
            'forecast_vs_actual': fva,
            'weather_by_time':  wbt,
            'ingredient_demand': five_day_ingredient_demand
        }