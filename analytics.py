import os
import sqlite3
import pandas as pd
import json
from datetime import datetime, timedelta

# ============================================================
#    DYNAMIC PRODUCT DICTIONARY (Requirement 1)
# ============================================================
# SKU_MAPPING removed in favor of Database Recipe Registry

# Malaysian Public Holiday List (Matches Forecast Engine)
HOLIDAYS = [
    "2024-01-01", "2024-02-10", "2024-02-11", "2024-04-10", "2024-04-11", 
    "2024-05-01", "2024-05-22", "2024-06-03", "2024-06-17", "2024-08-31", 
    "2024-09-16", "2024-10-31", "2024-12-25",
    "2025-01-01", "2025-01-29", "2025-02-01", "2025-03-31", "2025-05-01", 
    "2025-05-12", "2025-06-02", "2025-06-07", "2025-08-31", "2025-09-16", 
    "2025-10-02", "2025-12-25",
    "2026-01-01", "2026-02-01", "2026-02-17", "2026-03-21", "2026-05-01", 
    "2026-05-27", "2026-08-31", "2026-09-16", "2026-10-21", "2026-12-25",
    "2027-01-01", "2027-02-01", "2027-02-06", "2027-03-10", "2027-05-01", 
    "2027-05-17", "2027-08-31", "2027-09-16"
]

def get_db_connection():
    db_path = os.path.join('database', 'coffee_shop.db')
    for k, v in os.environ.items():
        if k.strip() == 'DB_PATH' and v.strip():
            db_path = v.strip()
            break
    return sqlite3.connect(db_path)

# ============================================================
#    CORE DATA PROCESSING ENGINE
# ============================================================
def process_sales_dataframe(df):
    """
    Transforms the 17-column raw POS CSV into the 23-column enterprise schema.
    """
    processed_df = pd.DataFrame()

    # Layer 1: Core Identification Data
    processed_df['transaction_id'] = df['Transaction_ID']
    processed_df['branch_id'] = df['Store_ID']

    # Layer 2: Temporal Data (Time and Date)
    dt_series = pd.to_datetime(df['Timestamp'])
    processed_df['transaction_date'] = dt_series.dt.strftime('%Y-%m-%d')
    processed_df['transaction_time'] = dt_series.dt.strftime('%H:%M:%S')
    processed_df['Hour'] = dt_series.dt.strftime('%H')
    processed_df['Day Name'] = dt_series.dt.day_name()
    processed_df['Month Name'] = dt_series.dt.month_name()
    processed_df['Month'] = dt_series.dt.strftime('%m')

    # Layer 3: Product and Order Details
    processed_df['item_name'] = df['Item_Name'].str.upper().str.strip()
    
    # Generate a temporary product_id if not present or mapping is dynamic
    processed_df['product_id'] = processed_df['item_name'].apply(lambda x: f"SKU-{hash(x) % 10000:04}")
    
    processed_df['product_category'] = df['Item_Category']
    
    def format_modifiers(mod_json):
        try:
            mods = json.loads(mod_json)
            if isinstance(mods, list):
                return ", ".join(mods)
            return str(mods)
        except:
            return ""

    processed_df['product_detail'] = df['Modifiers'].apply(format_modifiers)
    processed_df['transaction_qty'] = df['Quantity_Sold'].astype(int)
    processed_df['order_type'] = df['Order_Type']

    # Layer 4: Financial and Payment Data
    processed_df['unit_price_MYR'] = df['Gross_Sales'] / df['Quantity_Sold']
    processed_df['gross_sales_MYR'] = df['Gross_Sales']
    processed_df['discount_amount_MYR'] = df['Discount_Amount']
    processed_df['promo_code'] = df['Promo_Code'].fillna('NONE').str.upper().str.strip()
    processed_df['sst_amount_MYR'] = df['Tax_Amount']
    processed_df['Total_Bill_MYR'] = df['Net_Sales']
    processed_df['payment_method'] = df['Payment_Type']

    # Layer 5: Location Context (Database Driven)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT branch_code, branch_name, location_type, district, state FROM branch WHERE is_active = 1")
        branches = cursor.fetchall()
        conn.close()
        
        location_map = {
            row[0]: {
                'store_location': row[1],
                'location_type': row[2],
                'district': row[3],
                'state': row[4]
            } for row in branches
        }
    except Exception as e:
        print(f"[METADATA ERROR] Falling back to defaults: {e}")
        location_map = {}
    
    loc_data = df['Store_ID'].map(lambda x: location_map.get(x, {
        'store_location': 'Unknown', 'location_type': 'Unknown', 
        'district': 'Unknown', 'state': 'Unknown'
    }))
    
    processed_df['store_location'] = loc_data.apply(lambda x: x['store_location'])
    processed_df['location_type'] = loc_data.apply(lambda x: x['location_type'])
    processed_df['district'] = loc_data.apply(lambda x: x['district'])
    processed_df['state'] = loc_data.apply(lambda x: x['state'])

    # Layer 6: AI Contextual Variables (Enriched by ETL)
    processed_df['weather_condition'] = 'Cloudy' # Placeholder, filled by ETL pipeline
    processed_df['is_public_holiday'] = 0 # Placeholder, filled by ETL pipeline

    return processed_df

def shorten_promo_code(code):
    """
    Refactored Label Engine:
    - Direct database-to-UI mapping
    - Replaces underscores/hyphens with spaces
    - Maintains B1F1 abbreviation
    """
    if not code or code == 'NONE':
        return 'None'

    # 1. B1F1 Mappings
    temp = code.upper().replace('BOGOF', 'B1F1').replace('BUY1FREE1', 'B1F1').replace('BUY-1-FREE-1', 'B1F1')
    
    # 2. Database formatting: Underscore/Hyphen to Space
    temp = temp.replace('_', ' ').replace('-', ' ').strip()

    # 3. Final Clean formatting
    return temp.title().replace('B1f1', 'B1F1')

def promo_efficiency_analyzer(where_clause="", params=None):
    """
    Requirement 2: Promotion Intelligence
    Analyzes historical promo codes to identify high-volume vs high-value campaigns.
    """
    if params is None:
        params = []
        
    conn = get_db_connection()
    query = f"""
        SELECT promo_code, 
               SUM(transaction_qty) as total_qty,
               SUM(gross_sales_MYR) as total_gross,
               SUM(discount_amount_MYR) as total_discount,
               SUM(Total_Bill_MYR) as total_net
        FROM sales_transaction s
        {where_clause} {"AND" if where_clause else "WHERE"} promo_code != 'NONE'
        GROUP BY promo_code
    """
    
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    
    if df.empty:
        return []
        
    df['discount_ratio'] = (df['total_discount'] / df['total_gross']).round(4)
    # Apply shortening to the display version
    df['promo_display'] = df['promo_code'].apply(shorten_promo_code)
    
    def categorize_promo(row):
        # Logic: If Quantity is high relative to Net sales (Gross is roughly double Net), it's likely B1F1
        # Also check if discount ratio is near 0.5 for B1F1
        if row['discount_ratio'] >= 0.45:
            return "Volume (B1F1)"
        elif row['discount_ratio'] > 0:
            return "Percentage"
        return "Fixed"
        
    df['promo_type'] = df.apply(categorize_promo, axis=1)
    return df.to_dict('records')

# ============================================================
#    REQUIREMENT 3: RAMADHAN MODE FILTERS
# ============================================================
def get_ramadhan_peak_hours(branch_id=None, time_filter='all'):
    """
    Requirement 3: Analyzes peak hours strictly during the fasting month.
    Focuses on the 4:30 PM - 12:00 AM night shift.
    """
    ramadhan_windows = [
        ('2024-03-12', '2024-04-09'),
        ('2025-03-02', '2025-03-30'),
        ('2026-02-19', '2026-03-20'),
        ('2027-02-08', '2027-03-09')
    ]
    
    date_conds = " OR ".join([f"transaction_date BETWEEN '{s}' AND '{e}'" for s, e in ramadhan_windows])
    
    conn = get_db_connection()
    conds = [f"({date_conds})"]
    
    if branch_id:
        conds.append(f"branch_id = '{branch_id.upper().strip()}'")
        
    if time_filter == 'current_week':
        max_date = pd.read_sql_query("SELECT MAX(transaction_date) FROM sales_transaction", conn).iloc[0, 0]
        if not max_date:
            max_date = datetime.today().strftime('%Y-%m-%d')
        conds.append(f"transaction_date >= date('{max_date}', '-7 days')")
    elif time_filter and time_filter.startswith('year_'):
        year = time_filter.split('_')[1]
        conds.append(f"strftime('%Y', transaction_date) = '{year}'")
    elif time_filter and time_filter.startswith('month_'):
        month = time_filter.split('_')[1]
        conds.append(f"strftime('%Y-%m', transaction_date) = '{month}'")
        
    where_clause = "WHERE " + " AND ".join(conds)
    
    query = f"""
        SELECT 
            Hour, 
            "Day Name" as day_name,
            SUM(transaction_qty) as quantity_sold, 
            SUM(Total_Bill_MYR) as revenue
        FROM sales_transaction
        {where_clause}
        GROUP BY Hour, "Day Name" 
        ORDER BY 
            CASE "Day Name"
                WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2 WHEN 'Wednesday' THEN 3
                WHEN 'Thursday' THEN 4 WHEN 'Friday' THEN 5 WHEN 'Saturday' THEN 6
                WHEN 'Sunday' THEN 7
            END ASC, CAST(Hour AS INTEGER) ASC
    """
    
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df.to_dict('records')

def get_regular_peak_hours(branch_id=None, time_filter='all'):
    """
    Requirement 3: Analyzes peak hours excluding the fasting month.
    Preserves the morning/afternoon operational baseline.
    """
    ramadhan_windows = [
        ('2024-03-12', '2024-04-09'),
        ('2025-03-02', '2025-03-30'),
        ('2026-02-19', '2026-03-20'),
        ('2027-02-08', '2027-03-09')
    ]
    
    date_conds = " AND ".join([f"transaction_date NOT BETWEEN '{s}' AND '{e}'" for s, e in ramadhan_windows])
    
    conn = get_db_connection()
    conds = [f"({date_conds})"]
    
    if branch_id:
        conds.append(f"branch_id = '{branch_id.upper().strip()}'")
        
    if time_filter == 'current_week':
        max_date = pd.read_sql_query("SELECT MAX(transaction_date) FROM sales_transaction", conn).iloc[0, 0]
        if not max_date:
            max_date = datetime.today().strftime('%Y-%m-%d')
        conds.append(f"transaction_date >= date('{max_date}', '-7 days')")
    elif time_filter and time_filter.startswith('year_'):
        year = time_filter.split('_')[1]
        conds.append(f"strftime('%Y', transaction_date) = '{year}'")
    elif time_filter and time_filter.startswith('month_'):
        month = time_filter.split('_')[1]
        conds.append(f"strftime('%Y-%m', transaction_date) = '{month}'")
        
    where_clause = "WHERE " + " AND ".join(conds)
    
    query = f"""
        SELECT 
            Hour, 
            "Day Name" as day_name,
            SUM(transaction_qty) as quantity_sold, 
            SUM(Total_Bill_MYR) as revenue
        FROM sales_transaction
        {where_clause}
        GROUP BY Hour, "Day Name" 
        ORDER BY 
            CASE "Day Name"
                WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2 WHEN 'Wednesday' THEN 3
                WHEN 'Thursday' THEN 4 WHEN 'Friday' THEN 5 WHEN 'Saturday' THEN 6
                WHEN 'Sunday' THEN 7
            END ASC, CAST(Hour AS INTEGER) ASC
    """
    
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df.to_dict('records')

def bulk_insert_sales(df, db_path):
    """
    Inserts data into sales_transaction with Smart Deduplication:
    - If 100% of rows are duplicates, reject the file as already ingested.
    - If mixed (duplicates + new), skip duplicates and insert ONLY new rows.
    - If 100% new, insert all rows.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cursor = conn.cursor()
        incoming_ids = df['transaction_id'].tolist()
        
        # Check existing IDs in database using parameterized chunking
        existing_ids_set = set()
        chunk_size = 500
        for i in range(0, len(incoming_ids), chunk_size):
            chunk = incoming_ids[i:i+chunk_size]
            placeholders = ', '.join(['?'] * len(chunk))
            cursor.execute(f"SELECT transaction_id FROM sales_transaction WHERE transaction_id IN ({placeholders})", chunk)
            for row in cursor.fetchall():
                existing_ids_set.add(row[0])

        total_rows = len(df)
        dup_count = len(df[df['transaction_id'].isin(existing_ids_set)])

        # Case A: 100% Duplicates -> Reject
        if dup_count == total_rows:
            return False, "Database Rejection: Double ingestion alert. These transaction IDs are already registered."

        # Case B & C: Filter out duplicates and insert new rows only
        df_new = df[~df['transaction_id'].isin(existing_ids_set)].copy()
        new_count = len(df_new)

        if new_count > 0:
            df_new.to_sql('sales_transaction', conn, if_exists='append', index=False)
            conn.commit()

        if dup_count > 0:
            return True, f"Smart Ingestion: Skipped {dup_count} duplicate records and successfully loaded {new_count} new records."
        else:
            return True, f"Successfully loaded {new_count} new records."
            
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()

# ============================================================
#    ANALYTICS & AGGREGATION METHODS
# ============================================================
def get_dashboard_metrics(branch_id=None):
    conn = get_db_connection()
    query = "SELECT * FROM sales_transaction"
    if branch_id:
        query += f" WHERE branch_id = '{branch_id.upper().strip()}'"
        
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return None

    peak_hours = df.groupby('Hour')['transaction_qty'].sum().reset_index()
    peak_hours['hour_label'] = peak_hours['Hour'] + ":00"
    peak_hours = peak_hours.sort_values('Hour')

    product_mix = df.groupby('product_category')['Total_Bill_MYR'].sum().reset_index()
    product_mix = product_mix.sort_values('Total_Bill_MYR', ascending=False)

    return {
        'peak_hours': peak_hours.rename(columns={'hour_label': 'hour', 'transaction_qty': 'quantity_sold'}).to_dict('records'),
        'product_mix': product_mix.rename(columns={'Total_Bill_MYR': 'total_revenue'}).to_dict('records')
    }

def calculate_ingredient_demand(forecasted_sales_list):
    """
    Calculates operational inventory drawdown by querying the Product Recipe Registry.
    Requirement 1: Dynamic Math Engine
    """
    inventory_demand = {
        "beans_g": 0,
        "milk_ml": 0,
        "choco_g": 0,
        "ice_g": 0,
        "whip_g": 0,
        "cup_hot": 0,
        "cup_cold": 0,
        "custom": {}  # For dynamic aggregation of extra ingredients
    }

    if not forecasted_sales_list:
        return inventory_demand

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get all recipes from DB for mapping
    cursor.execute("SELECT * FROM product_recipes")
    recipes_raw = cursor.fetchall()
    # Map by item_name (upper)
    recipe_map = {}
    for r in recipes_raw:
        recipe_map[r[0].upper().strip()] = {
            "beans_g": r[1],
            "milk_ml": r[2],
            "choco_g": r[3],
            "ice_g":   r[4],
            "whip_g":  r[5],
            "cup_type": r[6],
            "custom_ingredients": json.loads(r[7] or '{}')
        }
    conn.close()

    for item in forecasted_sales_list:
        # Use item_name if provided, otherwise fallback to product_id (which might be the name now)
        name = item.get('item_name') or item.get('product_id', '')
        name_key = str(name).upper().strip()
        qty = item.get('quantity', 0)
        
        recipe = recipe_map.get(name_key)
        if not recipe:
            continue
            
        inventory_demand["beans_g"] += recipe["beans_g"] * qty
        inventory_demand["milk_ml"] += recipe["milk_ml"] * qty
        inventory_demand["choco_g"] += recipe["choco_g"] * qty
        inventory_demand["ice_g"]   += recipe["ice_g"]   * qty
        inventory_demand["whip_g"]  += recipe["whip_g"]  * qty
        
        if recipe["cup_type"] == "Hot":
            inventory_demand["cup_hot"] += 1 * qty
        elif recipe["cup_type"] == "Cold":
            inventory_demand["cup_cold"] += 1 * qty
            
        # Handle custom ingredients
        for key, data in recipe["custom_ingredients"].items():
            # NORMALIZATION LOGIC: 
            # Merge "Matcha (g)" (old) and "Matcha" with {unit: "g"} (new)
            
            raw_name = key
            val = 0
            unit = 'unit'

            if isinstance(data, dict):
                # New Format: {"val": 10, "unit": "ml"}
                val = data.get('val', 0)
                unit = data.get('unit', 'unit')
                name = raw_name
            else:
                # Old Format: {"Syrup (ml)": 10}
                val = data
                # Extract unit from name if present
                import re
                unit_match = re.search(r'\((ml|g|pcs)\)', raw_name, re.IGNORECASE)
                if unit_match:
                    unit = unit_match.group(1).toLowerCase() if hasattr(unit_match.group(1), 'toLowerCase') else unit_match.group(1).lower()
                    name = raw_name.replace(unit_match.group(0), '').strip()
                else:
                    name = raw_name

            # Standardized key for aggregation
            agg_key = f"{name} ({unit})"
            
            if agg_key not in inventory_demand["custom"]:
                inventory_demand["custom"][agg_key] = 0
            inventory_demand["custom"][agg_key] += val * qty

    return inventory_demand

def format_to_ops_units(demand_dict):
    """
    Converts raw ml and g into L and kg for operational clarity.
    """
    formatted = {}
    for k, v in demand_dict.items():
        if k == 'custom':
            formatted['custom'] = v
            continue
        
        # Convert ml to L
        if 'ml' in k:
            new_key = k.replace('_ml', '_L')
            formatted[new_key] = round(v / 1000, 2)
        # Convert g to kg
        elif '_g' in k:
            new_key = k.replace('_g', '_kg')
            formatted[new_key] = round(v / 1000, 2)
        else:
            formatted[k] = v
    return formatted

def get_promo_roi_report(promo_code_fuzzy):
    """
    Requirement 2: Advanced Promotion ROI
    Analyzes a specific campaign across all years it appeared.
    """
    conn = get_db_connection()
    query = f"""
        SELECT 
            strftime('%Y', transaction_date) as yr,
            promo_code,
            SUM(transaction_qty) as total_qty,
            SUM(gross_sales_MYR) as total_gross,
            SUM(discount_amount_MYR) as total_discount,
            SUM(Total_Bill_MYR) as total_net
        FROM sales_transaction
        WHERE promo_code LIKE ?
        GROUP BY yr, promo_code
        ORDER BY yr DESC
    """
    df = pd.read_sql_query(query, conn, params=(f"%{promo_code_fuzzy.upper()}%",))
    conn.close()
    
    if df.empty:
        return []
        
    # Calculate Multiplier: Net Revenue generated for every RM 1 of discount given
    df['roi_multiplier'] = (df['total_net'] / df['total_discount']).replace([float('inf'), -float('inf')], 0).fillna(0).round(2)
    # Worth It Logic: Multiplier > 8x is Excellent, > 5x is Good, < 3x is Poor
    def evaluate_roi(val):
        if val >= 8: return "EXCELLENT (Highly Worth It)"
        if val >= 5: return "GOOD (Sustainable)"
        return "LOW (High Burn / Low ROI)"
        
    df['worth_it_score'] = df['roi_multiplier'].apply(evaluate_roi)
    return df.to_dict('records')

def get_seasonal_comparison_report(season_name, years):
    """
    Requirement 3: Seasonal Year-over-Year Comparison
    Uses predefined windows (like Ramadhan) to compare performance.
    """
    windows = {
        'ramadhan': [
            ('2024', '2024-03-12', '2024-04-09'),
            ('2025', '2025-03-02', '2025-03-30'),
            ('2026', '2026-02-19', '2026-03-20'),
            ('2027', '2027-02-08', '2027-03-09')
        ]
        # Add other seasons if needed
    }
    
    target_windows = windows.get(season_name.lower())
    if not target_windows:
        return []
        
    results = []
    conn = get_db_connection()
    for yr, start, end in target_windows:
        if yr not in [str(y) for y in years]:
            continue
            
        query = """
            SELECT 
                SUM(Total_Bill_MYR) as revenue,
                COUNT(transaction_id) as transactions,
                COUNT(DISTINCT transaction_date) as days
            FROM sales_transaction
            WHERE transaction_date BETWEEN ? AND ?
        """
        df = pd.read_sql_query(query, conn, params=(start, end))
        if not df.empty and df['revenue'].iloc[0]:
            row = df.iloc[0].to_dict()
            row['year'] = yr
            row['daily_avg'] = round(row['revenue'] / max(row['days'], 1), 2)
            results.append(row)
    conn.close()
    return results

def revenue_decline_and_product_mix_profiler(branch_id=None, reference_date=None):
    conn = get_db_connection()
    query = "SELECT transaction_date, product_category, product_id, transaction_qty, Total_Bill_MYR FROM sales_transaction"
    if branch_id:
        query += f" WHERE branch_id = '{branch_id.upper().strip()}'"

    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return {}

    df['transaction_date'] = pd.to_datetime(df['transaction_date'])

    if reference_date:
        max_date = pd.to_datetime(reference_date)
    else:
        max_date = df['transaction_date'].max()

    last_30_start = max_date - timedelta(days=30)
    prev_30_start = last_30_start - timedelta(days=30)
    
    last_30_df = df[df['transaction_date'] > last_30_start]
    prev_30_df = df[(df['transaction_date'] <= last_30_start) & (df['transaction_date'] > prev_30_start)]
    
    rev_last = last_30_df['Total_Bill_MYR'].sum()
    rev_prev = prev_30_df['Total_Bill_MYR'].sum()
    
    ord_last = last_30_df['transaction_qty'].sum()
    ord_prev = prev_30_df['transaction_qty'].sum()
    
    aov_last = rev_last / ord_last if ord_last > 0 else 0
    aov_prev = rev_prev / ord_prev if ord_prev > 0 else 0
    
    product_performance_last = last_30_df.groupby(['product_category', 'product_id'])['Total_Bill_MYR'].sum().reset_index()
    product_performance_prev = prev_30_df.groupby(['product_category', 'product_id'])['Total_Bill_MYR'].sum().reset_index()
    
    merged_perf = pd.merge(product_performance_prev, product_performance_last, on=['product_category', 'product_id'], how='outer', suffixes=('_prev', '_last')).fillna(0)
    merged_perf['variance'] = merged_perf['Total_Bill_MYR_last'] - merged_perf['Total_Bill_MYR_prev']
    slow_movers = merged_perf[merged_perf['variance'] < 0].sort_values('variance').head(5).to_dict('records')
    
    return {
        'revenue_variance_rm': rev_last - rev_prev,
        'revenue_variance_pct': ((rev_last - rev_prev) / rev_prev * 100) if rev_prev > 0 else 0,
        'order_count_variance': ord_last - ord_prev,
        'aov_shift_rm': aov_last - aov_prev,
        'slow_moving_products': slow_movers
    }

def multi_month_chart_pre_packager(months=3):
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT transaction_date, branch_id, Total_Bill_MYR FROM sales_transaction", conn)
    
    # Dynamic Branch Name Mapping
    cursor = conn.cursor()
    cursor.execute("SELECT branch_code, branch_name FROM branch")
    branch_map = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    
    df['branch_name'] = df['branch_id'].map(branch_map).fillna(df['branch_id'])
    
    if df.empty:
        return "{}"
        
    df['transaction_date'] = pd.to_datetime(df['transaction_date'])
    df['month'] = df['transaction_date'].dt.to_period('M').astype(str)
    
    all_months = sorted(df['month'].unique())
    recent_months = all_months[-months:] if len(all_months) >= months else all_months
    
    recent_df = df[df['month'].isin(recent_months)]
    grouped = recent_df.groupby(['branch_name', 'month'])['Total_Bill_MYR'].sum().unstack(fill_value=0)
    
    datasets = []
    for branch in grouped.index:
        datasets.append({
            "label": branch,
            "data": [round(v, 2) for v in grouped.loc[branch].tolist()]
        })
        
    return json.dumps({
        "type": "bar",
        "title": f"{len(recent_months)}-Month Branch Comparison",
        "labels": grouped.columns.tolist(),
        "datasets": datasets
    })

def weather_payday_cross_tabulation(where_clause="", params=None):
    if params is None:
        params = []
    conn = get_db_connection()
    
    # ── 1. Weather vs Temperature Impact ──
    # We use 's' as alias to match build_where in app.py
    weather_query = f'''
        SELECT s.weather_condition, 
               CASE WHEN s.product_detail LIKE '%ICED%' THEN 'ICED' ELSE 'HOT' END as temp_type,
               SUM(s.transaction_qty) as total_qty
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.weather_condition, temp_type
    '''
    weather_df = pd.read_sql_query(weather_query, conn, params=params)
    
    # ── 2. Payday Spend Analysis ──
    # We calculate if the date falls in the 25th-28th window
    payday_query = f'''
        SELECT 
            CASE 
                WHEN cast(strftime('%d', s.transaction_date) as integer) BETWEEN 25 AND 28 THEN 'Payday Window'
                ELSE 'Standard Window'
            END as period,
            AVG(s.Total_Bill_MYR / s.transaction_qty) as average_spend
        FROM sales_transaction s
        {where_clause} {"AND" if where_clause else "WHERE"} s.transaction_qty > 0
        GROUP BY period
    '''
    payday_df = pd.read_sql_query(payday_query, conn, params=params)
    conn.close()
    
    weather_tab = weather_df.rename(columns={'temp_type': 'temperature_type'}).to_dict('records') if not weather_df.empty else []
    payday_tab = payday_df.to_dict('records') if not payday_df.empty else []
    
    return {
        'weather_temperature_impact': weather_tab,
        'payday_spend_analysis': payday_tab
    }

# ============================================================
#    AUTO-TUNE AI INTELLIGENCE (Self-Learning Logic)
# ============================================================
def sync_business_intelligence(db_path):
    """
    Analyzes historical sales to calculate exact holiday impacts for each branch.
    Requires at least 14 days baseline and 2 holiday samples to 'Unlock' Auto-Tune.
    """
    from datetime import datetime
    
    # Malaysian Public Holiday List (Matches Forecast Engine)
    HOLIDAYS = [
        "2024-01-01", "2024-02-10", "2024-02-11", "2024-04-10", "2024-04-11", 
        "2024-05-01", "2024-05-22", "2024-06-03", "2024-06-17", "2024-08-31", 
        "2024-09-16", "2024-10-31", "2024-12-25",
        "2025-01-01", "2025-01-29", "2025-02-01", "2025-03-31", "2025-05-01", 
        "2025-05-12", "2025-06-02", "2025-06-07", "2025-08-31", "2025-09-16", 
        "2025-10-02", "2025-12-25",
        "2026-01-01", "2026-02-01", "2026-02-17", "2026-03-21", "2026-05-01", 
        "2026-05-27", "2026-08-31", "2026-09-16", "2026-10-21", "2026-12-25",
        "2027-01-01", "2027-02-01", "2027-02-06", "2027-03-10", "2027-05-01", 
        "2027-05-17", "2027-08-31", "2027-09-16"
    ]

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 1. Fetch all active branches
        cursor.execute("SELECT branch_code FROM branch")
        branches = [r[0] for r in cursor.fetchall()]
        
        tuning_results = []

        for b_code in branches:
            # 2. Get daily revenue aggregates for this branch
            query = """
                SELECT transaction_date, SUM(Total_Bill_MYR) as daily_rev
                FROM sales_transaction
                WHERE branch_id = ?
                GROUP BY transaction_date
            """
            df = pd.read_sql_query(query, conn, params=(b_code,))
            
            if df.empty: continue
            
            # Split into Holiday vs Normal
            df['is_holiday'] = df['transaction_date'].isin(HOLIDAYS)
            
            holiday_df = df[df['is_holiday']]
            normal_df  = df[~df['is_holiday']]
            
            total_days    = len(df)
            holiday_count = len(holiday_df)
            
            # --- DOUBLE KEY LOCK: 14 Days + 2 Holidays ---
            if total_days >= 14 and holiday_count >= 2:
                avg_holiday = holiday_df['daily_rev'].mean()
                avg_normal  = normal_df['daily_rev'].mean()
                
                if avg_normal > 0:
                    variance = (avg_holiday - avg_normal) / avg_normal
                    
                    # 3. Update Database
                    cursor.execute("""
                        UPDATE branch 
                        SET holiday_effect = ? 
                        WHERE branch_code = ?
                    """, (round(variance, 4), b_code))
                    
                    tuning_results.append(f"{b_code}: Auto-tuned to {variance*100:.1f}%")
            else:
                tuning_results.append(f"{b_code}: Learning in progress ({total_days}/14 days, {holiday_count}/2 holidays)")

        conn.commit()
        conn.close()
        return True, tuning_results

    except Exception as e:
        return False, [str(e)]