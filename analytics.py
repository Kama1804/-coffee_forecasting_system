import os
import sqlite3
import pandas as pd
import json
from datetime import datetime, timedelta

# ============================================================
#    EXACT CLIENT SKU MAPPING & PRODUCT DICTIONARY (11 ITEMS)
# ============================================================
SKU_MAPPING = {
    "HOT ESPRESSO": "SKU-1001",
    "HOT AMERICANO": "SKU-1002",
    "ICED AMERICANO": "SKU-1003",
    "HOT LATTE": "SKU-1004",
    "ICE LATTE": "SKU-1005",
    "HOT CAPPUCCINO": "SKU-1006",
    "ICE CAPPUCCINO": "SKU-1007",
    "HOT MOCHA": "SKU-1008",
    "ICE MOCHA": "SKU-1009",
    "ICE BLENDED MOCHA": "SKU-1010",
    "ICE BLENDED CHOCOLATE CHIP": "SKU-1011"
}

def get_db_connection():
    db_path = os.path.join('database', 'coffee_shop.db')
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
    def assign_sku(item_name):
        name_upper = str(item_name).upper().strip()
        return SKU_MAPPING.get(name_upper, "SKU-9999")

    processed_df['product_id'] = df['Item_Name'].apply(assign_sku)
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
    processed_df['sst_amount_MYR'] = df['Tax_Amount']
    processed_df['Total_Bill_MYR'] = df['Net_Sales']
    processed_df['payment_method'] = df['Payment_Type']

    # Layer 5: Location Context (System Generated)
    location_map = {
        'FT-PA1': {
            'store_location': 'Puncak Alam',
            'location_type': 'Food Truck',
            'district': 'Kuala Selangor',
            'state': 'Selangor'
        },
        'STB-PJ1': {
            'store_location': 'Putrajaya',
            'location_type': 'Stall Booth',
            'district': 'Putrajaya',
            'state': 'WP Putrajaya'
        }
    }
    
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

def bulk_insert_sales(df, db_path):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        df.to_sql('sales_transaction', conn, if_exists='append', index=False)
        conn.commit()
        return True, f"Successfully inserted {len(df)} records."
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
    Calculates operational inventory drawdown mapped strictly to your 11 menu items.
    """
    inventory_demand = {
        "beans_g": 0,
        "milk_ml": 0,
        "choco_g": 0,
        "ice_g": 0,
        "whip_g": 0,
        "cup_hot": 0,
        "cup_cold": 0
    }

    for item in forecasted_sales_list:
        sku = item.get('product_id', '').upper().strip()
        qty = item.get('quantity', 0)

        # ── 1. ESPRESSO & AMERICANOS ────────────────────────────────────────
        if sku in ["SKU-1001", "SKU-1002"]:  # Hot Espresso, Hot Americano
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["cup_hot"] += 1 * qty
        elif sku == "SKU-1003":  # Iced Americano
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["ice_g"] += 150 * qty
            inventory_demand["cup_cold"] += 1 * qty

        # ── 2. MILK-BASED ESPRESSO CORES ────────────────────────────────────
        elif sku == "SKU-1004":  # Hot Latte
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["milk_ml"] += 200 * qty
            inventory_demand["cup_hot"] += 1 * qty
        elif sku == "SKU-1005":  # Ice Latte
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["milk_ml"] += 220 * qty
            inventory_demand["ice_g"] += 120 * qty
            inventory_demand["cup_cold"] += 1 * qty

        elif sku == "SKU-1006":  # Hot Cappuccino
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["milk_ml"] += 150 * qty
            inventory_demand["cup_hot"] += 1 * qty
        elif sku == "SKU-1007":  # Ice Cappuccino
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["milk_ml"] += 180 * qty
            inventory_demand["ice_g"] += 120 * qty
            inventory_demand["cup_cold"] += 1 * qty

        elif sku == "SKU-1008":  # Hot Mocha
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["milk_ml"] += 180 * qty
            inventory_demand["choco_g"] += 20 * qty
            inventory_demand["cup_hot"] += 1 * qty
        elif sku == "SKU-1009":  # Ice Mocha
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["milk_ml"] += 200 * qty
            inventory_demand["choco_g"] += 25 * qty
            inventory_demand["ice_g"] += 120 * qty
            inventory_demand["cup_cold"] += 1 * qty

        # ── 3. PREMIUM ICE BLENDED LINES ────────────────────────────────────
        elif sku == "SKU-1010":  # Ice Blended Mocha
            inventory_demand["beans_g"] += 18 * qty
            inventory_demand["milk_ml"] += 120 * qty
            inventory_demand["choco_g"] += 25 * qty
            inventory_demand["ice_g"] += 200 * qty
            inventory_demand["whip_g"] += 20 * qty
            inventory_demand["cup_cold"] += 1 * qty

        elif sku == "SKU-1011":  # Ice Blended Chocolate Chip
            inventory_demand["milk_ml"] += 150 * qty
            inventory_demand["choco_g"] += 40 * qty
            inventory_demand["ice_g"] += 200 * qty
            inventory_demand["whip_g"] += 20 * qty
            inventory_demand["cup_cold"] += 1 * qty

    return inventory_demand

def revenue_decline_and_product_mix_profiler(branch_id=None):
    conn = get_db_connection()
    query = "SELECT transaction_date, product_category, product_id, transaction_qty, Total_Bill_MYR FROM sales_transaction"
    if branch_id:
        query += f" WHERE branch_id = '{branch_id.upper().strip()}'"
        
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    if df.empty:
        return {}
        
    df['transaction_date'] = pd.to_datetime(df['transaction_date'])
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
    branch_map = {'FT-PA1': 'Puncak Alam', 'STB-PJ1': 'Putrajaya'}
    df['branch_name'] = df['branch_id'].map(branch_map)
    conn.close()
    
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