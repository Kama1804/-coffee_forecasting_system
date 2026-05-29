import sqlite3
import os

def initialize_database():
    # Define the database file path inside a 'database' folder
    db_folder = 'database'
    os.makedirs(db_folder, exist_ok=True)
    db_path = os.path.join(db_folder, 'coffee_shop.db')

    # Connect to SQLite (this automatically creates the file if it doesn't exist)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("Connected to SQLite. Initializing tables...")

    # 1. Create the Sales Transaction Table (Enterprise-Grade 23-Column Schema)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sales_transaction (
        transaction_id TEXT PRIMARY KEY,
        branch_id TEXT NOT NULL,
        transaction_date TEXT NOT NULL,
        transaction_time TEXT NOT NULL,
        Hour TEXT NOT NULL,
        "Day Name" TEXT NOT NULL,
        "Month Name" TEXT NOT NULL,
        Month TEXT NOT NULL,
        product_id TEXT NOT NULL,
        product_category TEXT NOT NULL,
        product_detail TEXT,
        transaction_qty INTEGER NOT NULL,
        order_type TEXT NOT NULL,
        unit_price_MYR REAL NOT NULL,
        sst_amount_MYR REAL NOT NULL,
        Total_Bill_MYR REAL NOT NULL,
        payment_method TEXT NOT NULL,
        store_location TEXT NOT NULL,
        location_type TEXT NOT NULL,
        district TEXT NOT NULL,
        state TEXT NOT NULL,
        weather_condition TEXT,
        is_public_holiday INTEGER NOT NULL
    )
    ''')
    
    # Speed Optimization Indexes for Analytics Dashboard Filters
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_date ON sales_transaction(transaction_date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_branch ON sales_transaction(branch_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_branch_date ON sales_transaction(branch_id, transaction_date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_category ON sales_transaction(product_category)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_product ON sales_transaction(product_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_hour ON sales_transaction(Hour)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_location ON sales_transaction(store_location)')

    # 2. Create the Sales Forecast Table (For Prophet outputs)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sales_forecast (
        forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
        forecast_date TEXT NOT NULL,
        branch_id TEXT NOT NULL,
        predicted_revenue REAL NOT NULL,
        lower_bound_revenue REAL NOT NULL,
        upper_bound_revenue REAL NOT NULL
    )
    ''')
    
    # Speed Optimization Indexes for AI Chatbot Context Extraction
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_forecast_date ON sales_forecast(forecast_date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_forecast_branch_date ON sales_forecast(branch_id, forecast_date)')

    # Commit changes and close the connection
    conn.commit()
    conn.close()
    
    print(f"Success! Database schema initialized successfully at {db_path}")

if __name__ == '__main__':
    initialize_database()