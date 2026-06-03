import pandas as pd
import sqlite3
import os
import requests
import json
from datetime import datetime
from analytics import process_sales_dataframe, bulk_insert_sales

class ETLPipeline:
    def __init__(self, filepath):
        self.filepath = filepath
        self.df = None
        
        # New 17-column Enterprise Schema
        self.required_columns = [
            'Transaction_ID', 'Timestamp', 'Register_ID', 'Cashier_Name', 
            'Store_ID', 'Item_Name', 'Item_Category', 'Quantity_Sold', 
            'Modifiers', 'Order_Type', 'Gross_Sales', 'Discount_Amount', 
            'Promo_Code', 'Discount_Reason', 'Tax_Amount', 'Net_Sales', 'Payment_Type'
        ]

    def _enrich_weather(self, df):
        BRANCH_COORDS = {
            'STB-PJ1': {'lat': 2.9264, 'lon': 101.6964},
            'FT-PA1':  {'lat': 3.2353, 'lon': 101.4243}
        }

        def fetch_weather_logic(date_str, branch_id):
            coords = BRANCH_COORDS.get(branch_id)
            if not coords:
                return 'Cloudy'
            
            try:
                # 1. Isolate Daylight Blocks (07:00 - 19:00)
                url = "https://archive-api.open-meteo.com/v1/archive"
                params = {
                    "latitude":   coords['lat'],
                    "longitude":  coords['lon'],
                    "start_date": date_str,
                    "end_date":   date_str,
                    "hourly":     ["weathercode", "precipitation"],
                    "timezone":   "Asia/Kuala_Lumpur"
                }
                resp = requests.get(url, params=params, timeout=10)
                resp.raise_for_status()
                res_json = resp.json()
                
                if "hourly" not in res_json:
                    return 'Cloudy'

                hourly_data = res_json["hourly"]
                times = hourly_data["time"]
                codes = hourly_data["weathercode"]
                precip = hourly_data["precipitation"]

                daylight_codes = []
                daylight_precip = 0.0

                for t, c, p in zip(times, codes, precip):
                    hour = int(t.split('T')[1].split(':')[0])
                    if 7 <= hour <= 19:
                        daylight_codes.append(c)
                        daylight_precip += p

                # 2. Check the Volume Threshold (2.5 mm)
                if daylight_precip < 2.5:
                    # Apply Weighted Majority Vote for dry days
                    # Fair / Sunny: 0, 1, 2
                    # Cloudy: 3, 45, 48, 51, 53, 55
                    sunny_count = sum(1 for c in daylight_codes if c in [0, 1, 2])
                    cloudy_count = sum(1 for c in daylight_codes if c in [3, 45, 48, 51, 53, 55])
                    
                    if cloudy_count > sunny_count:
                        return 'Cloudy'
                    else:
                        return 'Fair / Sunny'

                # 3. Identify Severe Lightning (Thunderstorm)
                if any(c in [95, 96, 99] for c in daylight_codes):
                    return 'Thunderstorm'
                
                # Default if rain is >= 2.5mm but no thunderstorm
                return 'Raining'

            except Exception as e:
                print(f"[WEATHER ERROR] {date_str} {branch_id}: {e}")
                return 'Cloudy'

        weather_cache = {}
        unique_pairs  = df[['transaction_date', 'branch_id']].drop_duplicates()

        print(f"[ETL LOG] Fetching weather for {len(unique_pairs)} discrete vectors (Daylight Aggregation)...")

        for _, row in unique_pairs.iterrows():
            key = (row['transaction_date'], row['branch_id'])
            if key not in weather_cache:
                weather_cache[key] = fetch_weather_logic(row['transaction_date'], row['branch_id'])

        df['weather_condition'] = df.apply(
            lambda r: weather_cache.get((r['transaction_date'], r['branch_id']), 'Cloudy'), axis=1
        )
        return df

    def _enrich_holidays(self, df):
        # Expanded multi-year public holiday index mapping (covering 2025 and 2026 operational windows)
        MY_HOLIDAYS = [
            '2025-01-01', '2025-02-01', '2025-05-01', '2025-08-31', '2025-09-16', '2025-12-25',
            '2026-01-01', '2026-02-01', '2026-02-17', '2026-02-18', '2026-05-01', '2026-08-31', '2026-09-16', '2026-12-25'
        ]
        df['is_public_holiday'] = df['transaction_date'].isin(MY_HOLIDAYS).astype(int)
        return df

    def process_data(self):
        try:
            raw_df = pd.read_csv(self.filepath)
            if raw_df.empty:
                return False, "Upload Failed: The uploaded CSV file contains no data rows."

            # Header Cleanup with Title Case preservation
            raw_df.columns = raw_df.columns.str.strip().str.title()
            
          
            column_mapping = {
                'Transaction_Id': 'Transaction_ID',
                'Register_Id': 'Register_ID',
                'Store_Id': 'Store_ID',
                'Item_Name': 'Item_Name',
                'Item_Category': 'Item_Category',
                'Quantity_Sold': 'Quantity_Sold',
                'Gross_Sales': 'Gross_Sales',
                'Discount_Amount': 'Discount_Amount',
                'Promo_Code': 'Promo_Code',
                'Discount_Reason': 'Discount_Reason',
                'Tax_Amount': 'Tax_Amount',
                'Net_Sales': 'Net_Sales',
                'Payment_Type': 'Payment_Type'
            }
            raw_df = raw_df.rename(columns=column_mapping)
            
            missing_cols = [col for col in self.required_columns if col not in raw_df.columns]
            if missing_cols:
                return False, f"Upload Failed. Missing required columns: {', '.join(missing_cols)}"

            # Text Standardization (Forcing alignment on value items to avoid data fragmentation)
            raw_df['Store_ID'] = raw_df['Store_ID'].astype(str).str.strip().str.upper()
            raw_df['Payment_Type'] = raw_df['Payment_Type'].astype(str).str.strip().str.upper()
            raw_df['Item_Name'] = raw_df['Item_Name'].astype(str).str.strip()
            raw_df['Item_Category'] = raw_df['Item_Category'].astype(str).str.strip()

            # Bad Rows Removal (Dropping negative or zero values in financial transaction items)
            raw_df = raw_df[raw_df['Quantity_Sold'] > 0]
            raw_df = raw_df[raw_df['Gross_Sales'] > 0]
            raw_df = raw_df[raw_df['Net_Sales'] > 0]

            # Drop missing values and duplicates based on unique Transaction_ID
            raw_df = raw_df.dropna(subset=['Transaction_ID', 'Timestamp', 'Store_ID', 'Item_Name'])
            raw_df = raw_df.drop_duplicates(subset=['Transaction_ID'], keep='first')

            if raw_df.empty:
                return False, "Upload Failed: Cleaning steps removed all rows (invalid business metric profiles detected)."

            # Safety Interceptor for Weather API
            try:
                raw_df['transaction_date'] = raw_df['Timestamp'].str.split('T').str[0]
                if raw_df['transaction_date'].str.contains('-').sum() == 0:
                    raw_df['transaction_date'] = pd.to_datetime(raw_df['Timestamp']).dt.strftime('%Y-%m-%d')
            except Exception:
                return False, "Upload Failed: Unable to parse dates from Timestamp formatting profile."

            # Transformation using Analytics Engine (Returns 23-column schema layout)
            self.df = process_sales_dataframe(raw_df)

            # Filtering for valid branches
            self.df = self.df[self.df['branch_id'].isin(['FT-PA1', 'STB-PJ1'])]
            if self.df.empty:
                return False, "Upload Failed: No transactions matched valid branch domains (FT-PA1, STB-PJ1)."

            # Enrichments (Guaranteed clean text dates are passed downstream here)
            self.df = self._enrich_weather(self.df)
            self.df = self._enrich_holidays(self.df)

            # --- REQUIREMENT 1: Recipe Registry Detection ---
            self.missing_recipes = self._check_for_missing_recipes(raw_df)
            
            return True, "Data successfully processed, normalized, and validated via ETL pipeline."

        except Exception as e:
            return False, f"ETL Pipeline Error: {str(e)}"

    def _check_for_missing_recipes(self, raw_df):
        """
        Compare Item_Names from CSV against the product_recipes table.
        """
        try:
            db_path = os.path.join('database', 'coffee_shop.db')
            conn = sqlite3.connect(db_path)
            
            # Get unique items from the uploaded CSV
            csv_items = set(raw_df['Item_Name'].unique())
            
            # Get existing recipes from DB
            cursor = conn.cursor()
            cursor.execute("SELECT item_name FROM product_recipes")
            db_items = set(row[0] for row in cursor.fetchall())
            conn.close()
            
            # Identify missing ones
            missing = [item for item in csv_items if item.upper().strip() not in [db_i.upper().strip() for db_i in db_items]]
            return missing
        except Exception as e:
            print(f"[RECIPE CHECK ERROR] {e}")
            return []

    def save_to_database(self, db_path):
        try:
            if self.df is None or self.df.empty:
                return False, "No data available to save."

            success, message = bulk_insert_sales(self.df, db_path)
            return success, message

        except sqlite3.IntegrityError:
            return False, "Database Rejection: Double ingestion alert. These transaction IDs are already registered."
        except Exception as e:
            error_msg = str(e)
            if "locked" in error_msg.lower():
                return False, "Database is currently locked. Please close any DB browsers and retry."
            return False, f"Execution failed: {error_msg}"