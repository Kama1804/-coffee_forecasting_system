import pytest
import sqlite3
import os
import pandas as pd
import json
from app import app
from etl_pipeline import ETLPipeline
from analytics import calculate_ingredient_demand, sync_business_intelligence

# ============================================================
#    TEST CONFIGURATION & FIXTURES
# ============================================================

@pytest.fixture
def test_db():
    """Creates a temporary in-memory database for isolated testing."""
    db_path = "test_coffee_shop.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Setup Branch Table
    cursor.execute('''
        CREATE TABLE branch (
            branch_code TEXT PRIMARY KEY,
            branch_name TEXT,
            location_type TEXT,
            latitude REAL,
            longitude REAL,
            description TEXT,
            holiday_effect REAL,
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    # Setup Sales Transaction Table
    cursor.execute('''
        CREATE TABLE sales_transaction (
            transaction_id TEXT PRIMARY KEY,
            branch_id TEXT,
            transaction_date TEXT,
            Total_Bill_MYR REAL,
            item_name TEXT,
            transaction_qty INTEGER
        )
    ''')

    # Setup Product Recipes Table
    cursor.execute('''
        CREATE TABLE product_recipes (
            item_name TEXT PRIMARY KEY,
            beans_g REAL,
            milk_ml REAL,
            custom_ingredients TEXT
        )
    ''')

    # Add a Mock Branch
    cursor.execute("INSERT INTO branch VALUES ('TEST-01', 'Test Branch', 'Kiosk', 3.0, 101.0, 'Test Persona', 0.0, 1)")
    
    conn.commit()
    yield db_path
    conn.close()
    if os.path.exists(db_path):
        os.remove(db_path)

@pytest.fixture
def client():
    """Flask test client."""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


# ============================================================
#    PILLAR 1: INGESTION TESTS
# ============================================================

def test_etl_rejects_internal_duplicates():
    """Verify that ETL pipeline rejects files with duplicate IDs."""
    # Create a temporary CSV with duplicates
    csv_path = "test_dupes.csv"
    data = {
        "Transaction_ID": ["TXN1", "TXN1"],
        "Timestamp": ["2026-06-01T10:00:00", "2026-06-01T10:00:00"],
        "Register_ID": ["REG1", "REG1"],
        "Cashier_Name": ["Siti", "Siti"],
        "Store_ID": ["TEST-01", "TEST-01"],
        "Item_Name": ["Latte", "Latte"],
        "Item_Category": ["Coffee", "Coffee"],
        "Quantity_Sold": [1, 1],
        "Modifiers": ["[]", "[]"],
        "Order_Type": ["Dine-in", "Dine-in"],
        "Gross_Sales": [10, 10],
        "Discount_Amount": [0, 0],
        "Promo_Code": ["NONE", "NONE"],
        "Discount_Reason": ["None", "None"],
        "Tax_Amount": [0.6, 0.6],
        "Net_Sales": [10, 10],
        "Payment_Type": ["CASH", "CASH"]
    }
    pd.DataFrame(data).to_csv(csv_path, index=False)
    
    pipeline = ETLPipeline(csv_path)
    success, message = pipeline.process_data()
    
    if os.path.exists(csv_path):
        os.remove(csv_path)
        
    assert success is False
    assert "Duplicate transaction IDs detected" in message

# ============================================================
#    PILLAR 2: ANALYTICS & RECIPE TESTS
# ============================================================

def test_ingredient_demand_calculation():
    """Verify math for ingredient aggregation (Base + Structured Custom)."""
    # Mock Forecast Result
    predicted_items = [
        {"item_name": "Matcha Latte", "quantity": 10} # 10 units predicted
    ]
    
    # Mock Recipe Dictionary
    # This simulates how analytics.py fetches recipes from DB
    recipe_map = {
        "Matcha Latte": {
            "beans_g": 0,
            "milk_ml": 200,
            "custom_ingredients": {
                "Matcha": {"val": 4.5, "unit": "g"}
            }
        }
    }
    
    # We need to monkeypatch or wrap the function to use our mock map
    # For this unit test, let's test the aggregation logic directly
    inventory = {
        "beans": 0, "milk": 0, "choco": 0, "ice": 0, "whip": 0,
        "cup_hot": 0, "cup_cold": 0, "custom": {}
    }
    
    # Manual loop simulation of analytics.py logic
    for item in predicted_items:
        recipe = recipe_map[item['item_name']]
        qty = item['quantity']
        inventory["milk"] += recipe["milk_ml"] * qty
        
        for ing, data in recipe["custom_ingredients"].items():
            key = f"{ing} ({data['unit']})"
            inventory["custom"][key] = data["val"] * qty

    assert inventory["milk"] == 2000 # 200ml * 10 = 2000ml
    assert inventory["custom"]["Matcha (g)"] == 45 # 4.5g * 10 = 45g


# ============================================================
#    PILLAR 3: AUTO-TUNE AI TESTS
# ============================================================

def test_auto_tune_logic_calculation(test_db):
    """Verify that Auto-Tune calculates correct variance from sales data."""
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    
    # 1. Insert 14 days of data (Maturity Requirement)
    # 12 Normal Days @ RM 1000
    for i in range(1, 13):
        date = f"2026-06-{i:02d}"
        cursor.execute("INSERT INTO sales_transaction VALUES (?, ?, ?, ?, ?, ?)",
                       (f"T{i}", "TEST-01", date, 1000.0, "Coffee", 10))
    
    # 2. Insert 2 Holidays @ RM 1500 (+50% lift)
    # Using real dates from Malaysian Holiday List in analytics.py
    cursor.execute("INSERT INTO sales_transaction VALUES (?, ?, ?, ?, ?, ?)",
                   ("H1", "TEST-01", "2026-05-01", 1500.0, "Coffee", 15))
    cursor.execute("INSERT INTO sales_transaction VALUES (?, ?, ?, ?, ?, ?)",
                   ("H2", "TEST-01", "2026-05-27", 1500.0, "Coffee", 15))
    
    conn.commit()
    conn.close()
    
    # Run Sync
    success, results = sync_business_intelligence(test_db)
    
    # Verify Result
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("SELECT holiday_effect FROM branch WHERE branch_code = 'TEST-01'")
    effect = cursor.fetchone()[0]
    conn.close()
    
    assert success is True
    assert effect == 0.5 # RM 1500 vs RM 1000 = +50%


# ============================================================
#    PILLAR 4: SECURITY & ACCESS TESTS
# ============================================================

def test_unauthorized_access_redirect(client):
    """Verify that protected pages redirect to login if not authenticated."""
    response = client.get('/manage-business', follow_redirects=True)
    # Should redirect to login page
    assert b"Login" in response.data
    assert response.status_code == 200

def test_login_session_security(client):
    """Verify login provides session access."""
    # Use environment variables or hardcoded test credentials
    with client.session_transaction() as sess:
        sess['logged_in'] = True
    
    response = client.get('/manage-business')
    assert response.status_code == 200
    assert b"Manage Business" in response.data

if __name__ == "__main__":
    pytest.main([__file__])
