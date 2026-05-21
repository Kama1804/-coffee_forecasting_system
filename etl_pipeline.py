import pandas as pd
import sqlite3
import os
import requests
from datetime import datetime

class ETLPipeline:
    def __init__(self, filepath):
        self.filepath = filepath
        self.df = None
        
        # Exact target schema required by the core analytics engine
        self.required_columns = [
            'Transaction_ID', 'Date', 'Time', 'Outlet', 'Category', 
            'Product_Name', 'Quantity_Sold', 'Unit_Price_RM', 
            'Total_Revenue_RM', 'Payment_Method'
        ]

    def _enrich_weather(self, df):
        BRANCH_COORDS = {
            'Putrajaya':   {'lat': 2.9264, 'lon': 101.6964},
            'Puncak Alam': {'lat': 3.2353, 'lon': 101.4243}
        }

        def map_weather_code(code):
            if code in [0, 1, 2]:
                return 'Sunny'
            elif code in [3, 45, 48, 51, 53, 55]:
                return 'Cloudy'
            elif code in [61, 63, 65, 80, 81, 82, 95, 96, 99]:
                return 'Raining'
            else:
                return 'Cloudy'

        def fetch_weather(date_str, branch):
            coords = BRANCH_COORDS.get(branch)
            if not coords:
                return 'Cloudy'
            if date_str > datetime.today().strftime('%Y-%m-%d'):
                return 'Cloudy'

            try:
                url = "https://archive-api.open-meteo.com/v1/archive"
                params = {
                    "latitude":   coords['lat'],
                    "longitude":  coords['lon'],
                    "start_date": date_str,
                    "end_date":   date_str,
                    "daily":      "weathercode",
                    "timezone":   "Asia/Kuala_Lumpur"
                }
                resp = requests.get(url, params=params, timeout=5)
                resp.raise_for_status()
                res_json = resp.json()
                
                if "daily" in res_json and res_json["daily"]["weathercode"]:
                    code = res_json["daily"]["weathercode"][0]
                    return map_weather_code(code)
                return 'Cloudy'
            except Exception:
                return 'Cloudy'  

        weather_cache = {}
        unique_pairs  = df[['Date', 'Outlet']].drop_duplicates()

        print(f"[ETL LOG] Fetching weather for {len(unique_pairs)} discrete vectors...")

        for _, row in unique_pairs.iterrows():
            key = (row['Date'], row['Outlet'])
            if key not in weather_cache:
                weather_cache[key] = fetch_weather(row['Date'], row['Outlet'])

        df['Weather_Condition'] = df.apply(
            lambda r: weather_cache.get((r['Date'], r['Outlet']), 'Cloudy'), axis=1
        )
        return df

    def process_data(self):
        try:
            self.df = pd.read_csv(self.filepath)
            if self.df.empty:
                return False, "Upload Failed: The uploaded CSV file contains no data rows."

            self.df.columns = self.df.columns.str.strip().str.title()
            
            header_mapping = {
                'Transaction_Id': 'Transaction_ID',     
                'Transactionid': 'Transaction_ID',
                'Sale_Date': 'Date',
                'Sales_Date': 'Date',
                'Transaction_Time': 'Time',
                'Outlet_Name': 'Outlet',
                'Product_Category': 'Category',
                'Unit_Price': 'Unit_Price_RM',
                'Unit_Price_Rm': 'Unit_Price_RM',       
                'Total_Revenue': 'Total_Revenue_RM',
                'Total_Revenue_Rm': 'Total_Revenue_RM'   
            }
            self.df = self.df.rename(columns=header_mapping)
            
            missing_cols = [col for col in self.required_columns if col not in self.df.columns]
            if missing_cols:
                return False, f"Upload Failed. Missing required columns: {', '.join(missing_cols)}"

            # ✅ SELF-HEALING UPGRADE: Automatically drop internal row collisions inside the uploaded file
            self.df = self.df.drop_duplicates(subset=['Transaction_ID'], keep='first')

            self.df = self.df.dropna(subset=['Transaction_ID', 'Date', 'Time', 'Product_Name'])

            parsed_dates = pd.to_datetime(self.df['Date'], dayfirst=True, errors='coerce')
            self.df['Time'] = pd.to_datetime(self.df['Time'], format='%H:%M', errors='coerce').dt.strftime('%H:%M')
            self.df['Date'] = parsed_dates.dt.strftime('%Y-%m-%d')
            self.df = self.df.dropna(subset=['Date', 'Time'])

            self.df['Quantity_Sold'] = pd.to_numeric(self.df['Quantity_Sold'], errors='coerce').fillna(1).astype(int)
            self.df['Unit_Price_RM'] = pd.to_numeric(self.df['Unit_Price_RM'], errors='coerce')
            self.df = self.df.dropna(subset=['Unit_Price_RM'])
            
            self.df['Total_Revenue_RM'] = self.df['Quantity_Sold'] * self.df['Unit_Price_RM']
            self.df = self.df[self.df['Total_Revenue_RM'] > 0]

            self.df['Outlet'] = self.df['Outlet'].astype(str).str.strip().str.title()
            self.df['Category'] = self.df['Category'].astype(str).str.strip().str.title()
            self.df['Product_Name'] = self.df['Product_Name'].astype(str).str.strip().str.title()
            self.df['Payment_Method'] = self.df['Payment_Method'].astype(str).str.strip().str.upper()

            self.df = self.df[self.df['Outlet'].isin(['Putrajaya', 'Puncak Alam'])]
            if self.df.empty:
                return False, "Upload Failed: No transactions matched valid branch domains."

            self.df['Weather_Condition'] = None
            self.df = self._enrich_weather(self.df)

            return True, "Data successfully processed, normalized, and validated via ETL pipeline."

        except Exception as e:
            return False, f"ETL Pipeline Error: {str(e)}"

    def save_to_database(self, db_path):
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

            # ✅ LOCK RESOLUTION UPGRADE: Extension timeout extended to 30s to bypass external locks cleanly
            conn = sqlite3.connect(db_path, timeout=30.0)
            db_df.to_sql('sales_transaction', conn, if_exists='append', index=False)
            conn.commit()
            conn.close()

            return True, f"Successfully inserted {len(db_df)} records into the database."

        except sqlite3.IntegrityError as e:
            print(f"[SQL ERROR] Integrity Error Triggered: {e}")
            return False, "Database Rejection: Double ingestion alert. These transaction IDs are already registered."
        except Exception as e:
            print(f"[DB ERROR] General DB Failure: {e}")
            error_msg = str(e)
            if "locked" in error_msg.lower():
                return False, "Database is currently locked by an external file-viewer process. Please close DB Browser and retry upload."
            return False, f"Execution failed: {error_msg}"