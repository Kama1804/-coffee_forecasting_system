import pandas as pd
import sqlite3
import os

def get_dashboard_metrics(branch_id=None):
    """
    Extracts and aggregates data for the frontend dashboard visuals.
    """
    db_path = os.path.join('database', 'coffee_shop.db')
    conn = sqlite3.connect(db_path)
    
    # Build query (filter by branch if requested)
    query = "SELECT * FROM sales_transaction"
    if branch_id:
        query += f" WHERE branch_id = {branch_id}"
        
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return None

    # 1. Peak Hours Aggregation (Extract the hour from "14:30")
    df['hour'] = df['transaction_time'].str[:2] + ":00"
    peak_hours = df.groupby('hour')['quantity_sold'].sum().reset_index()
    peak_hours = peak_hours.sort_values('hour')

    # 2. Product Mix Aggregation
    product_mix = df.groupby('product_category')['total_revenue'].sum().reset_index()
    product_mix = product_mix.sort_values('total_revenue', ascending=False)

    return {
        'peak_hours': peak_hours.to_dict('records'),
        'product_mix': product_mix.to_dict('records')
    }

# Quick test if run directly
if __name__ == '__main__':
    metrics = get_dashboard_metrics(branch_id=1) # Test Putrajaya
    print("--- Peak Hours ---")
    print(metrics['peak_hours'][:3]) # Print first 3 rows
    print("\n--- Product Mix ---")
    print(metrics['product_mix'])