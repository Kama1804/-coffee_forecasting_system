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

    # 1. Create the Branch Dimension Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS branch (
        branch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_name VARCHAR(100) NOT NULL,
        location_type VARCHAR(100)
    )
    ''')

    # 2. Create the Sales Transaction Table (with all granular columns)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sales_transaction (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        txn_reference VARCHAR(50) UNIQUE, 
        sale_date DATE NOT NULL,
        transaction_time VARCHAR(10) NOT NULL,
        branch_id INTEGER NOT NULL,
        product_category VARCHAR(100) NOT NULL,
        product_name VARCHAR(150) NOT NULL,
        quantity_sold INTEGER NOT NULL,
        unit_price FLOAT NOT NULL,
        total_revenue FLOAT NOT NULL,
        payment_method VARCHAR(50) NOT NULL,
        weather_condition VARCHAR(50) NOT NULL,
        FOREIGN KEY (branch_id) REFERENCES branch(branch_id)
    )
    ''')

    # 3. Create the Sales Forecast Table (For Prophet outputs)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sales_forecast (
        forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
        forecast_date DATE NOT NULL,
        branch_id INTEGER NOT NULL,
        predicted_revenue FLOAT NOT NULL,
        lower_bound_revenue FLOAT NOT NULL,
        upper_bound_revenue FLOAT NOT NULL,
        FOREIGN KEY (branch_id) REFERENCES branch(branch_id)
    )
    ''')

    # 4. Pre-populate the branch table if it's empty
    cursor.execute("SELECT COUNT(*) FROM branch")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO branch (branch_name, location_type) VALUES ('Putrajaya', 'Administrative/Office')")
        cursor.execute("INSERT INTO branch (branch_name, location_type) VALUES ('Puncak Alam', 'University/Residential')")
        print("Pre-populated branch data.")

    # Commit changes and close the connection
    conn.commit()
    conn.close()
    
    print(f"Success! Database schema initialized successfully at {db_path}")

if __name__ == '__main__':
    initialize_database()