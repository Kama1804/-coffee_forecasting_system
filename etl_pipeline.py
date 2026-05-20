import pandas as pd
import sqlite3
import os
import requests

class ETLPipeline:
    def __init__(self, filepath):
        self.filepath = filepath
        self.df = None
        
        # Weather_Condition removed — system handles it automatically
        self.required_columns = [
            'Transaction_ID', 'Date', 'Time', 'Outlet', 'Category', 
            'Product_Name', 'Quantity_Sold', 'Unit_Price_RM', 
            'Total_Revenue_RM', 'Payment_Method'
        ]

    def _enrich_weather(self, df):
        """
        Fetches historical weather from Open-Meteo for each unique date+branch
        and fills the Weather_Condition column automatically.
        No API key needed — Open-Meteo is free.
        """
        BRANCH_COORDS = {
            'Putrajaya':   {'lat': 2.9264, 'lon': 101.6964},
            'Puncak Alam': {'lat': 3.2353, 'lon': 101.4243}
        }

        def map_weather_code(code):
            """
            Tuned for Malaysian tropical climate.
            Drizzle (51,53,55) treated as Cloudy — light rain is normal
            and does not significantly reduce coffee shop footfall.
            Only moderate/heavy rain (61+) counts as Raining.
            """
            if code in [0, 1, 2]:
                # Clear or mainly clear
                return 'Sunny'
            elif code in [3, 45, 48, 51, 53, 55]:
                # Overcast, fog, light/moderate drizzle — treated as Cloudy
                return 'Cloudy'
            elif code in [61, 63, 65, 80, 81, 82, 95, 96, 99]:
                # Moderate to heavy rain, showers, thunderstorm
                return 'Raining'
            else:
                return 'Cloudy'

        def fetch_weather(date_str, branch):
            coords = BRANCH_COORDS.get(branch)
            if not coords:
                return 'Cloudy'
            try:
                url    = "https://archive-api.open-meteo.com/v1/archive"
                params = {
                    "latitude":   coords['lat'],
                    "longitude":  coords['lon'],
                    "start_date": date_str,
                    "end_date":   date_str,
                    "daily":      "weathercode",
                    "timezone":   "Asia/Kuala_Lumpur"
                }
                resp = requests.get(url, params=params, timeout=8)
                resp.raise_for_status()
                code = resp.json()["daily"]["weathercode"][0]
                return map_weather_code(code)
            except Exception:
                return 'Cloudy'  # safe fallback if API fails

        # Cache by (date, branch) so we don't repeat API calls for the same day
        weather_cache = {}
        unique_pairs  = df[['Date', 'Outlet']].drop_duplicates()

        print(f"Fetching weather for {len(unique_pairs)} unique date-branch combinations...")

        for _, row in unique_pairs.iterrows():
            key = (row['Date'], row['Outlet'])
            if key not in weather_cache:
                weather_cache[key] = fetch_weather(row['Date'], row['Outlet'])

        df['Weather_Condition'] = df.apply(
            lambda r: weather_cache.get((r['Date'], r['Outlet']), 'Cloudy'), axis=1
        )

        print("Weather enrichment complete.")
        return df

    def process_data(self):
        """Executes data standardization, cleaning, and enrichment."""
        try:
            self.df = pd.read_csv(self.filepath)
            self.df.columns = self.df.columns.str.strip()
            
            missing_cols = [col for col in self.required_columns if col not in self.df.columns]
            if missing_cols:
                return False, f"Upload Failed. Missing required columns: {', '.join(missing_cols)}"

            self.df = self.df.dropna(subset=['Date', 'Outlet', 'Total_Revenue_RM'])
            self.df['Date'] = pd.to_datetime(self.df['Date'], errors='coerce').dt.strftime('%Y-%m-%d')
            self.df['Time'] = pd.to_datetime(self.df['Time'], format='%H:%M', errors='coerce').dt.strftime('%H:%M')
            self.df = self.df.dropna(subset=['Date', 'Time'])

            self.df['Quantity_Sold'] = pd.to_numeric(self.df['Quantity_Sold'], errors='coerce').fillna(0).astype(int)
            self.df['Unit_Price_RM'] = pd.to_numeric(self.df['Unit_Price_RM'], errors='coerce')
            self.df['Total_Revenue_RM'] = pd.to_numeric(self.df['Total_Revenue_RM'], errors='coerce')
            self.df = self.df.dropna(subset=['Total_Revenue_RM'])

            self.df['Outlet'] = self.df['Outlet'].str.strip().str.title()
            self.df['Category'] = self.df['Category'].str.strip().str.title()
            self.df['Payment_Method'] = self.df['Payment_Method'].str.strip().str.upper()

            # --- API ENRICHMENT ---
            self.df['Weather_Condition'] = None
            self.df = self._enrich_weather(self.df)

            return True, "Data successfully processed, enriched with weather, and validated."

        except Exception as e:
            return False, f"ETL Pipeline Error: {str(e)}"

    def save_to_database(self, db_path):
        """Maps Pandas columns to SQLite schema and performs bulk insert."""
        try:
            if self.df is None or self.df.empty:
                return False, "No data available to save."

            branch_mapping = {'Putrajaya': 1, 'Puncak Alam': 2}
            self.df['branch_id'] = self.df['Outlet'].map(branch_mapping)

            self.df = self.df.dropna(subset=['branch_id'])
            self.df['branch_id'] = self.df['branch_id'].astype(int)

            db_df = self.df.rename(columns={
                'Transaction_ID': 'txn_reference',
                'Date': 'sale_date',
                'Time': 'transaction_time',
                'Category': 'product_category',
                'Product_Name': 'product_name',
                'Quantity_Sold': 'quantity_sold',
                'Unit_Price_RM': 'unit_price',
                'Total_Revenue_RM': 'total_revenue',
                'Payment_Method': 'payment_method',
                'Weather_Condition': 'weather_condition'
            })

            columns_to_keep = [
                'txn_reference', 'sale_date', 'transaction_time', 'branch_id',
                'product_category', 'product_name', 'quantity_sold', 
                'unit_price', 'total_revenue', 'payment_method', 'weather_condition'
            ]
            db_df = db_df[columns_to_keep]

            conn = sqlite3.connect(db_path)
            db_df.to_sql('sales_transaction', conn, if_exists='append', index=False)
            conn.commit()
            conn.close()

            return True, f"Successfully inserted {len(db_df)} records into the database."

        except sqlite3.IntegrityError:
            return False, "Database Error: Some of these transactions have already been uploaded (Duplicate ID)."
        except Exception as e:
            return False, f"Database Error: {str(e)}"