import sqlite3
import os

def initialize_database():
    # Read DB_PATH from environment variable (Railway volume) with local fallback
    db_path = os.environ.get('DB_PATH', os.path.join('database', 'coffee_shop.db'))

    # Extract the folder from the path and ensure it exists
    db_folder = os.path.dirname(db_path)
    if db_folder:
        os.makedirs(db_folder, exist_ok=True)

    # Connect to SQLite (this automatically creates the file if it doesn't exist)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("Connected to SQLite. Initializing tables...")

    # 1. Sales Transaction Table (Main Data Warehouse)
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
        item_name TEXT NOT NULL,
        product_category TEXT NOT NULL,
        product_detail TEXT,
        transaction_qty INTEGER NOT NULL,
        order_type TEXT NOT NULL,
        unit_price_MYR REAL NOT NULL,
        gross_sales_MYR REAL DEFAULT 0,
        discount_amount_MYR REAL DEFAULT 0,
        promo_code TEXT DEFAULT 'NONE',
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
    
    # Speed Optimization Indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_date ON sales_transaction(transaction_date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_branch ON sales_transaction(branch_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_category ON sales_transaction(product_category)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_hour ON sales_transaction(Hour)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_location ON sales_transaction(store_location)')

    # 2. Sales Forecast Table (AI Output Cache)
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
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_forecast_date ON sales_forecast(forecast_date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_forecast_branch_date ON sales_forecast(branch_id, forecast_date)')

    # 3. Branch Registry (Dynamic Business Management)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS branch (
        branch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_code VARCHAR(10) UNIQUE,
        branch_name VARCHAR(100) NOT NULL,
        location_type VARCHAR(100),
        latitude REAL DEFAULT 0.0,
        longitude REAL DEFAULT 0.0,
        district VARCHAR(100) DEFAULT 'Unknown',
        state VARCHAR(100) DEFAULT 'Unknown',
        description TEXT DEFAULT 'Standard coffee outlet profile',
        holiday_effect REAL DEFAULT 0.0,
        is_active INTEGER DEFAULT 1
    )
    ''')

    # 4. Product Recipes Table (Dynamic Inventory Tracking)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS product_recipes (
        item_name TEXT PRIMARY KEY,
        beans_g REAL DEFAULT 0,
        milk_ml REAL DEFAULT 0,
        choco_g REAL DEFAULT 0,
        ice_g REAL DEFAULT 0,
        whip_g REAL DEFAULT 0,
        cup_type TEXT,
        custom_ingredients TEXT,  -- JSON string for extensibility
        is_active INTEGER DEFAULT 1
    )
    ''')

    # Backfill default branches if table is empty
    cursor.execute("SELECT COUNT(*) FROM branch")
    if cursor.fetchone()[0] == 0:
        print("Backfilling default branches...")
        branches_data = [
            ('STB-PJ1', 'Putrajaya', 'Stall Booth', 2.921218, 101.683220, 'Putrajaya', 'WP Putrajaya', 'Government & office workers (Peak demand during weekdays)', -0.35),
            ('FT-PA1', 'Puncak Alam', 'Food Truck', 3.215075, 101.455976, 'Kuala Selangor', 'Selangor', 'University of UiTM students & residents (Peak demand during weekends/holidays)', 0.15)
        ]
        cursor.executemany("""
            INSERT INTO branch (branch_code, branch_name, location_type, latitude, longitude, district, state, description, holiday_effect, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, branches_data)

    conn.commit()
    conn.close()
    
    print(f"Success! Master database blueprint initialized at {db_path}")

if __name__ == '__main__':
    initialize_database()
