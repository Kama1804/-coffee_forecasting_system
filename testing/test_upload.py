import os
import sys
import pandas as pd
import sqlite3

# Ensure project root is in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from init_db import initialize_database
from analytics import process_sales_dataframe

# Set temporary database path
test_db = "test_upload.db"
os.environ['DB_PATH'] = test_db

# Initialize database schema and seeds
initialize_database()

# Use one of the generated raw files in uploads
csv_path = os.path.join(os.path.dirname(__file__), '..', 'uploads', 'MiniCoffee_Raw_putrajaya_2026.csv')

if not os.path.exists(csv_path):
    print(f"File not found: {csv_path}")
    sys.exit(1)

print(f"Testing upload of {csv_path}...")
df = pd.read_csv(csv_path)

# Mock the ETL header cleanup
df.columns = df.columns.str.strip().str.title()
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
df = df.rename(columns=column_mapping)

print("Processing dataframe...")
processed_df = process_sales_dataframe(df)

print("Attempting bulk insert...")
try:
    conn = sqlite3.connect(test_db)
    processed_df.to_sql('sales_transaction', conn, if_exists='append', index=False)
    conn.commit()
    conn.close()
    print("Success!")
except Exception as e:
    print(f"FAILED with error: {e}")
    import traceback
    traceback.print_exc()
finally:
    if os.path.exists(test_db):
        os.remove(test_db)
        print("Cleaned up temporary database.")
