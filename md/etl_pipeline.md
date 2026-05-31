# ETL Pipeline Architecture & Technical Documentation

This document provides a comprehensive technical overview of the Extract, Transform, and Load (ETL) pipeline implemented in `etl_pipeline.py` for the Mini Coffee Shop Sales Forecasting System.

## 1. Architectural Overview
The ETL pipeline is designed to ingest raw Point of Sale (POS) transaction logs, sanitize them, enrich them with historical context, and securely load them into a relational database. 

The data flows through the following discrete stages:
1. **Extraction:** The pipeline initializes by loading a user-uploaded CSV file into a Pandas DataFrame (`self.df = pd.read_csv(self.filepath)`).
2. **Standardization:** Column headers are standardized and mapped from varied POS exports into a strict internal schema using a predefined `header_mapping` dictionary.
3. **Transformation & Validation (Self-Healing):** The pipeline executes a series of automated cleaning steps, dropping malformed rows, imputing missing values, and formatting datatypes (Dates, Times, and Numerics) to ensure data integrity.
4. **Enrichment:** The clean transactional data is grouped by distinct Date/Outlet pairs, and historical weather data is fetched from the Open-Meteo API to append context to each transaction.
5. **Loading:** Finally, the enriched DataFrame is mapped to the internal `sales_transaction` schema and bulk-inserted into the SQLite database (`coffee_shop.db`).

## 2. Self-Healing Mechanisms
The pipeline is designed to handle common data anomalies found in raw POS exports without requiring manual intervention.

### Duplicate Identification
- **Mechanism:** The pipeline identifies duplicates based on the primary transactional key, `Transaction_ID`.
- **Logic:** It utilizes `self.df.drop_duplicates(subset=['Transaction_ID'], keep='first')` to automatically purge internal row collisions within the uploaded file, preserving only the first instance of any duplicated transaction reference.

### Handling Missing Values
- **Critical Drops:** Rows lacking fundamental identifying information (`Transaction_ID`, `Date`, `Time`, or `Product_Name`) are immediately dropped to prevent database corruption.
- **Imputation:** 
  - `Quantity_Sold` is coerced to a numeric type; any resulting `NaN` values are imputed with a default value of `1` (`.fillna(1).astype(int)`).
  - Transactions resulting in `Total_Revenue_RM <= 0` or missing `Unit_Price_RM` are dropped.

### Normalization
- **String Formatting:** String-based categorical data is aggressively stripped of leading/trailing whitespace and normalized using Title Case to ensure consistency across reporting aggregations.
  - `self.df['Outlet'] = self.df['Outlet'].astype(str).str.strip().str.title()`
  - `self.df['Category'] = self.df['Category'].astype(str).str.strip().str.title()`
  - `self.df['Product_Name'] = self.df['Product_Name'].astype(str).str.strip().str.title()`
- **Date/Time Parsing:** The pipeline coerces date formats into a strict `YYYY-MM-DD` standard and times into `HH:MM`, handling potential European/US formatting discrepancies via `pd.to_datetime(..., dayfirst=True, errors='coerce')`.

## 3. Weather Enrichment Pipeline
To improve the accuracy of the Prophet forecasting engine, the ETL pipeline dynamically enriches historical transactions with weather data.

### Process Flow
1. **Vector Reduction:** To prevent API rate-limiting, the pipeline identifies unique Date and Outlet combinations from the uploaded file (`df[['Date', 'Outlet']].drop_duplicates()`).
2. **API Interaction:** For each unique vector, the `fetch_weather()` function calls the Open-Meteo Archive API.
3. **Parameters Sent:**
   - `latitude` & `longitude`: Hardcoded in `BRANCH_COORDS` based on the Outlet name.
   - `start_date` & `end_date`: The specific transaction date.
   - `daily`: `weathercode` (requests the WMO Weather interpretation code).
   - `timezone`: `Asia/Kuala_Lumpur`.
4. **Data Mapping:** The returned WMO `weathercode` is mapped to three distinct conditions used by the analytics engine:
   - **Sunny:** Codes 0, 1, 2.
   - **Cloudy:** Codes 3, 45, 48, 51, 53, 55 (includes haze/drizzle).
   - **Raining:** Codes 61, 63, 65, 80, 81, 82, 95, 96, 99 (includes thunderstorms/heavy rain).
5. **Caching & Appending:** Results are stored in an in-memory `weather_cache` dictionary to prevent duplicate API calls for the same day/branch. The final mapped condition is then applied back to every individual transaction row in the DataFrame via a lambda function.

## 4. Database Interaction Layer
The final stage of the pipeline involves a robust insertion mechanism into the SQLite database.

### Schema Mapping
Before insertion, the Pandas DataFrame columns are explicitly mapped to match the target database schema using `.rename(columns={...})` (e.g., `Transaction_ID` to `txn_reference`). The branch names are mapped to their respective integer foreign keys (`branch_id`).

### Bulk Insertion & Lock Management
- **Bulk Loading:** The pipeline utilizes Pandas' highly efficient `to_sql()` method to perform a bulk insertion:
  `db_df.to_sql('sales_transaction', conn, if_exists='append', index=False)`
- **Concurrency Management:** Because the application features a live dashboard and background AI processes, SQLite database locks are a significant risk during large CSV ingestions. 
- **Extended Timeout Configuration:** To mitigate `database is locked` exceptions, the connection string explicitly configures an extended 30-second timeout mechanism:
  `conn = sqlite3.connect(db_path, timeout=30.0)`
  This ensures the ETL pipeline will patiently wait for UI read-queries to finish before attempting the bulk write.
- **Integrity Validation:** The script actively catches `sqlite3.IntegrityError` to safely block double-ingestion (attempting to upload the same CSV twice), providing a clean error message back to the frontend rather than crashing the server.
