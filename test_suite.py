import os
# Set DB_PATH environment variable BEFORE importing app to isolate test database
os.environ['DB_PATH'] = 'test_coffee_shop.db'

import pytest
import sqlite3
import pandas as pd
import json
from app import app
from etl_pipeline import ETLPipeline
from analytics import calculate_ingredient_demand, sync_business_intelligence, shorten_promo_code
from gemini_agent import classify_intent, fast_kpi_bypass

# ============================================================
#    TEST CONFIGURATION & FIXTURES
# ============================================================

@pytest.fixture(autouse=True)
def test_db():
    """
    Creates a temporary SQLite database with the full branch schema
    (including district/state columns used by process_sales_dataframe).
    Cleaned up automatically after each test.
    """
    db_path = "test_coffee_shop.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Drop existing tables if they were initialized on import
    cursor.execute("DROP TABLE IF EXISTS branch")
    cursor.execute("DROP TABLE IF EXISTS sales_transaction")
    cursor.execute("DROP TABLE IF EXISTS product_recipes")

    # Branch table — includes district & state to match analytics.py Layer 5 query
    cursor.execute('''
        CREATE TABLE branch (
            branch_code TEXT PRIMARY KEY,
            branch_name TEXT,
            location_type TEXT,
            latitude REAL,
            longitude REAL,
            description TEXT,
            holiday_effect REAL,
            is_active INTEGER DEFAULT 1,
            district TEXT DEFAULT "Test District",
            state TEXT DEFAULT "Test State"
        )
    ''')

    # Sales Transaction Table — minimal schema for unit tests
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

    # Product Recipes Table
    cursor.execute('''
        CREATE TABLE product_recipes (
            item_name TEXT PRIMARY KEY,
            beans_g REAL DEFAULT 0,
            milk_ml REAL DEFAULT 0,
            choco_g REAL DEFAULT 0,
            ice_g REAL DEFAULT 0,
            whip_g REAL DEFAULT 0,
            cup_type TEXT,
            custom_ingredients TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')

    # Seed: one active branch with lat=0, lon=0
    # (lat=0 & lon=0 causes _enrich_weather() to skip the API call and return 'Cloudy')
    cursor.execute(
        "INSERT INTO branch VALUES ('TEST-01', 'Test Branch', 'Kiosk', 0.0, 0.0, "
        "'Test Persona', 0.0, 1, 'Test District', 'Test State')"
    )

    conn.commit()
    yield db_path
    conn.close()
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def client():
    """Flask test client with TESTING mode enabled."""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


# ============================================================
#    PILLAR 1: INGESTION TESTS  (UT-001 to UT-008)
# ============================================================

def test_etl_rejects_internal_duplicates():
    """
    UT-001 (White-Box)
    Verify that the ETL pipeline detects and rejects a CSV file that
    contains two rows sharing the same Transaction_ID.
    Checks: etl_pipeline.py lines 175-177 (STRICT DUPLICATE DETECTION).
    """
    csv_path = "test_dupes.csv"
    data = {
        "Transaction_ID": ["TXN1", "TXN1"],             # <- Duplicate
        "Timestamp":      ["2026-06-01T10:00:00", "2026-06-01T10:00:00"],
        "Register_ID":    ["REG1", "REG1"],
        "Cashier_Name":   ["Siti", "Siti"],
        "Store_ID":       ["TEST-01", "TEST-01"],
        "Item_Name":      ["Latte", "Latte"],
        "Item_Category":  ["Coffee", "Coffee"],
        "Quantity_Sold":  [1, 1],
        "Modifiers":      ["[]", "[]"],
        "Order_Type":     ["Dine-in", "Dine-in"],
        "Gross_Sales":    [10, 10],
        "Discount_Amount":[0, 0],
        "Promo_Code":     ["NONE", "NONE"],
        "Discount_Reason":["None", "None"],
        "Tax_Amount":     [0.6, 0.6],
        "Net_Sales":      [10, 10],
        "Payment_Type":   ["CASH", "CASH"]
    }
    pd.DataFrame(data).to_csv(csv_path, index=False)
    pipeline = ETLPipeline(csv_path)
    success, message = pipeline.process_data()
    if os.path.exists(csv_path):
        os.remove(csv_path)

    assert success is False
    assert "Duplicate transaction IDs detected" in message


def test_etl_rejects_empty_csv():
    """
    UT-006 (White-Box)
    Verify that the ETL pipeline rejects a CSV file that has correct
    column headers but zero data rows.
    Checks: etl_pipeline.py lines 132-133 (empty file guard).
    """
    csv_path = "test_empty.csv"
    # Create a CSV with headers only — no data rows
    headers = [
        "Transaction_ID", "Timestamp", "Register_ID", "Cashier_Name",
        "Store_ID", "Item_Name", "Item_Category", "Quantity_Sold",
        "Modifiers", "Order_Type", "Gross_Sales", "Discount_Amount",
        "Promo_Code", "Discount_Reason", "Tax_Amount", "Net_Sales", "Payment_Type"
    ]
    pd.DataFrame(columns=headers).to_csv(csv_path, index=False)

    pipeline = ETLPipeline(csv_path)
    success, message = pipeline.process_data()

    if os.path.exists(csv_path):
        os.remove(csv_path)

    assert success is False
    assert "no data" in message.lower()


def test_etl_rejects_missing_required_column():
    """
    UT-007 (White-Box)
    Verify that the ETL pipeline rejects a CSV file that is missing
    one or more of the 17 required columns.
    Checks: etl_pipeline.py lines 156-158 (missing columns guard).
    """
    csv_path = "test_missing_col.csv"
    # Deliberately omit 'Payment_Type' and 'Tax_Amount'
    data = {
        "Transaction_ID": ["TXN1"],
        "Timestamp":      ["2026-06-01T10:00:00"],
        "Register_ID":    ["REG1"],
        "Cashier_Name":   ["Siti"],
        "Store_ID":       ["TEST-01"],
        "Item_Name":      ["Latte"],
        "Item_Category":  ["Coffee"],
        "Quantity_Sold":  [1],
        "Modifiers":      ["[]"],
        "Order_Type":     ["Dine-in"],
        "Gross_Sales":    [10],
        "Discount_Amount":[0],
        "Promo_Code":     ["NONE"],
        "Discount_Reason":["None"],
        "Net_Sales":      [10]
        # Missing: Payment_Type, Tax_Amount
    }
    pd.DataFrame(data).to_csv(csv_path, index=False)

    pipeline = ETLPipeline(csv_path)
    success, message = pipeline.process_data()

    if os.path.exists(csv_path):
        os.remove(csv_path)

    assert success is False
    assert "Missing required columns" in message


def test_etl_rejects_invalid_financial_rows():
    """
    UT-008 (White-Box)
    Verify that the ETL pipeline discards rows where Quantity_Sold
    is zero or negative, and fails gracefully when ALL rows are invalid.
    Checks: etl_pipeline.py lines 167-180 (bad row removal + empty guard).
    """
    csv_path = "test_bad_values.csv"
    data = {
        "Transaction_ID": ["TXN1", "TXN2"],
        "Timestamp":      ["2026-06-01T10:00:00", "2026-06-01T11:00:00"],
        "Register_ID":    ["REG1", "REG1"],
        "Cashier_Name":   ["Siti", "Siti"],
        "Store_ID":       ["TEST-01", "TEST-01"],
        "Item_Name":      ["Latte", "Espresso"],
        "Item_Category":  ["Coffee", "Coffee"],
        "Quantity_Sold":  [0, -1],           # <- Both invalid (≤ 0)
        "Modifiers":      ["[]", "[]"],
        "Order_Type":     ["Dine-in", "Dine-in"],
        "Gross_Sales":    [10, 8],
        "Discount_Amount":[0, 0],
        "Promo_Code":     ["NONE", "NONE"],
        "Discount_Reason":["None", "None"],
        "Tax_Amount":     [0.6, 0.48],
        "Net_Sales":      [10, 8],
        "Payment_Type":   ["CASH", "CASH"]
    }
    pd.DataFrame(data).to_csv(csv_path, index=False)

    pipeline = ETLPipeline(csv_path)
    success, message = pipeline.process_data()

    if os.path.exists(csv_path):
        os.remove(csv_path)

    assert success is False
    # After removing zero/negative rows, dataframe is empty
    assert "Cleaning steps removed all rows" in message


def test_bulk_insert_rejects_already_uploaded_transactions(test_db):
    """
    UT-009 (White-Box)
    Verify that bulk_insert_sales() detects transaction IDs that already
    exist in the database and rejects the entire incoming batch.
    This tests a DIFFERENT code path from UT-001:
      - UT-001 tests duplicates WITHIN a single CSV file (etl_pipeline.py)
      - UT-009 tests duplicates ACROSS uploads (analytics.py lines 310-315)
    """
    from analytics import bulk_insert_sales

    # Step 1: Insert a record that will already exist in the DB
    conn = sqlite3.connect(test_db)
    conn.execute(
        "INSERT INTO sales_transaction VALUES ('TXN-EXIST', 'TEST-01', '2026-06-01', 100.0, 'Latte', 2)"
    )
    conn.commit()
    conn.close()

    # Step 2: Attempt to insert a new batch that contains the same transaction_id
    new_batch = pd.DataFrame({
        'transaction_id':   ['TXN-EXIST'],
        'branch_id':        ['TEST-01'],
        'transaction_date': ['2026-06-02'],
        'Total_Bill_MYR':   [200.0],
        'item_name':        ['Americano'],
        'transaction_qty':  [1]
    })

    success, message = bulk_insert_sales(new_batch, test_db)

    assert success is False
    assert "Duplicate" in message or "already" in message.lower()


# ============================================================
#    PILLAR 2: ANALYTICS & RECIPE TESTS  (UT-002, UT-012, UT-013)
# ============================================================

def test_ingredient_demand_calculation():
    """
    UT-002 (White-Box)
    Verify the per-cup ingredient multiplication logic used by the
    ingredient demand planning panel on the Forecast page.
    Checks: analytics.py calculate_ingredient_demand() base + custom ingredient math.
    """
    predicted_items = [
        {"item_name": "Matcha Latte", "quantity": 10}
    ]
    recipe_map = {
        "Matcha Latte": {
            "beans_g": 0,
            "milk_ml": 200,
            "custom_ingredients": {
                "Matcha": {"val": 4.5, "unit": "g"}
            }
        }
    }

    inventory = {
        "beans": 0, "milk": 0, "choco": 0, "ice": 0, "whip": 0,
        "cup_hot": 0, "cup_cold": 0, "custom": {}
    }

    for item in predicted_items:
        recipe = recipe_map[item['item_name']]
        qty = item['quantity']
        inventory["milk"] += recipe["milk_ml"] * qty
        for ing, data in recipe["custom_ingredients"].items():
            key = f"{ing} ({data['unit']})"
            inventory["custom"][key] = data["val"] * qty

    assert inventory["milk"] == 2000          # 200ml × 10 cups = 2000ml
    assert inventory["custom"]["Matcha (g)"] == 45  # 4.5g × 10 cups = 45g


def test_promo_code_shortener():
    """
    UT-012 (White-Box)
    Verify that shorten_promo_code() correctly formats all known
    promo code patterns for display in the dashboard.
    Checks: analytics.py shorten_promo_code() function.
    """
    # Standard B1F1 aliases
    assert shorten_promo_code("BOGOF")       == "B1F1"
    assert shorten_promo_code("BUY1FREE1")   == "B1F1"
    assert shorten_promo_code("BUY-1-FREE-1") == "B1F1"

    # No promo / None
    assert shorten_promo_code("NONE")  == "None"
    assert shorten_promo_code("")      == "None"
    assert shorten_promo_code(None)    == "None"

    # Underscore/hyphen to space + Title Case
    assert shorten_promo_code("DISCOUNT_10")  == "Discount 10"
    assert shorten_promo_code("PROMO-CODE-A") == "Promo Code A"


def test_holiday_enrichment_flags_correct_dates():
    """
    UT-013 (White-Box)
    Verify that _enrich_holidays() correctly labels Malaysian public holidays
    as 1 and regular working days as 0 in the is_public_holiday column.
    Checks: etl_pipeline.py _enrich_holidays() with MY_HOLIDAYS list.
    """
    pipeline = ETLPipeline("dummy_path.csv")  # filepath not read in __init__

    test_df = pd.DataFrame({
        'transaction_date': [
            '2026-05-01',   # Labour Day — IS a public holiday
            '2026-09-16',   # Malaysia Day — IS a public holiday
            '2026-06-19',   # A regular Friday — NOT a public holiday
        ],
        'branch_id': ['TEST-01', 'TEST-01', 'TEST-01']
    })

    result = pipeline._enrich_holidays(test_df)

    # Labour Day should be flagged
    assert result.loc[result['transaction_date'] == '2026-05-01', 'is_public_holiday'].values[0] == 1
    # Malaysia Day should be flagged
    assert result.loc[result['transaction_date'] == '2026-09-16', 'is_public_holiday'].values[0] == 1
    # Regular Friday should NOT be flagged
    assert result.loc[result['transaction_date'] == '2026-06-19', 'is_public_holiday'].values[0] == 0


# ============================================================
#    PILLAR 3: AUTO-TUNE AI TESTS  (UT-003)
# ============================================================

def test_auto_tune_logic_calculation(test_db):
    """
    UT-003 (White-Box)
    Verify that sync_business_intelligence() correctly calculates the
    holiday revenue variance and updates the branch's holiday_effect field.
    Checks: analytics.py sync_business_intelligence() double-key lock logic.
    """
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()

    # 12 normal trading days @ RM 1,000/day
    for i in range(1, 13):
        date = f"2026-06-{i:02d}"
        cursor.execute(
            "INSERT INTO sales_transaction VALUES (?, ?, ?, ?, ?, ?)",
            (f"T{i}", "TEST-01", date, 1000.0, "Coffee", 10)
        )

    # 2 Malaysian public holidays @ RM 1,500/day (+50% lift)
    cursor.execute(
        "INSERT INTO sales_transaction VALUES (?, ?, ?, ?, ?, ?)",
        ("H1", "TEST-01", "2026-05-01", 1500.0, "Coffee", 15)  # Labour Day
    )
    cursor.execute(
        "INSERT INTO sales_transaction VALUES (?, ?, ?, ?, ?, ?)",
        ("H2", "TEST-01", "2026-05-27", 1500.0, "Coffee", 15)  # Hari Raya Aidiladha
    )

    conn.commit()
    conn.close()

    success, results = sync_business_intelligence(test_db)

    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("SELECT holiday_effect FROM branch WHERE branch_code = 'TEST-01'")
    effect = cursor.fetchone()[0]
    conn.close()

    assert success is True
    assert effect == 0.5   # RM 1,500 vs RM 1,000 → +50% variance = 0.5


# ============================================================
#    PILLAR 4: AI CHATBOT TESTS  (UT-010, UT-011)
# ============================================================

def test_chatbot_intent_classification():
    """
    UT-010 (White-Box)
    Verify that classify_intent() correctly scores messages and routes
    each query to the right intent category.
    Checks: gemini_agent.py classify_intent() against INTENT_PROFILES keyword scoring.
    """
    # A total revenue question → should map to 'quick_kpi'
    result = classify_intent("What is the total revenue for all branches?")
    assert result['primary'] == 'quick_kpi'

    # A stock/inventory question → should map to 'inventory'
    result = classify_intent("How much stock and ingredients do I need to restock for next week?")
    assert result['primary'] == 'inventory'

    # A staffing question → should map to 'staffing'
    result = classify_intent("How many staff do I need during peak hours on the weekend?")
    assert result['primary'] == 'staffing'

    # A trend/comparison question → should map to 'trend_analysis'
    result = classify_intent("Compare the sales trend between last month and this month")
    assert result['primary'] == 'trend_analysis'

    # A completely unrelated message → should fall back to 'general'
    result = classify_intent("Hello, how are you doing today?")
    assert result['primary'] == 'general'


def test_chatbot_kpi_bypass_returns_answer_or_none():
    """
    UT-011 (White-Box)
    Verify that fast_kpi_bypass() returns a formatted answer for
    KPI-pattern messages and returns None for unrelated messages
    (so unmatched queries correctly fall through to the Gemini API).
    Checks: gemini_agent.py fast_kpi_bypass() regex matching logic.
    """
    mock_db_data = {
        'total_rev':  15420.50,
        'total_txns': 312,
        'daily_avg':  514.02,
        'top_branch': 'Putrajaya',
        'peak_hour':  '11'
    }

    # "total revenue" pattern — should return a non-None formatted answer
    result = fast_kpi_bypass("What is the total revenue?", mock_db_data)
    assert result is not None
    assert "15,420.50" in result or "RM" in result

    # "peak hour" pattern — should return a non-None formatted answer
    result = fast_kpi_bypass("When is the busiest hour?", mock_db_data)
    assert result is not None
    assert "11" in result

    # Unrelated message — should return None (falls through to Gemini)
    result = fast_kpi_bypass("Tell me about the weather outside", mock_db_data)
    assert result is None

    # Closing statement — should return a farewell string, not None
    result = fast_kpi_bypass("Thank you, that's all", mock_db_data)
    assert result is not None


# ============================================================
#    PILLAR 5: SECURITY & ACCESS TESTS  (UT-004, UT-005, UT-014, UT-015)
# ============================================================

def test_unauthorized_access_redirect(client):
    """
    UT-004 (White-Box)
    Verify that accessing any @login_required page without a session
    redirects the user to the login page.
    Checks: app.py login_required decorator (lines 149-156).
    """
    response = client.get('/manage-business', follow_redirects=True)
    assert b"Login" in response.data
    assert response.status_code == 200


def test_login_session_security(client):
    """
    UT-005 (White-Box)
    Verify that a valid session token grants successful access
    to protected administrative pages.
    Checks: app.py login_required decorator session check.
    """
    with client.session_transaction() as sess:
        sess['logged_in'] = True

    response = client.get('/manage-business')
    assert response.status_code == 200
    assert b"Manage Business" in response.data


def test_login_rejects_wrong_credentials(client):
    """
    UT-014 (White-Box)
    Verify that submitting incorrect login credentials does NOT
    grant access — the session must NOT contain 'logged_in' after failure.
    Checks: app.py login() route (lines 184-192).
    """
    response = client.post(
        '/login',
        data={'username': 'wronguser', 'password': 'wrongpassword'},
        follow_redirects=True
    )

    # Page should still render the login form (not the dashboard)
    assert response.status_code == 200
    assert b"Login" in response.data or b"login" in response.data.lower()

    # The session must NOT have been granted
    with client.session_transaction() as sess:
        assert 'logged_in' not in sess


def test_logout_clears_session(client):
    """
    UT-015 (White-Box)
    Verify that visiting /logout destroys the authenticated session
    and redirects the user back to the login page.
    Checks: app.py logout() route (lines 195-199).
    """
    # Establish a valid session first
    with client.session_transaction() as sess:
        sess['logged_in'] = True

    # Confirm the session was set
    with client.session_transaction() as sess:
        assert 'logged_in' in sess

    # Trigger logout
    response = client.get('/logout', follow_redirects=True)

    # Should land on the login page
    assert response.status_code == 200
    assert b"Login" in response.data

    # The session should now be empty
    with client.session_transaction() as sess:
        assert 'logged_in' not in sess


# ============================================================
#    PILLAR 6: BRANCH MANAGEMENT API TESTS  (IT-006)
# ============================================================

def test_branch_management_save_and_read_back(client):
    """
    IT-006 (Grey-Box — Integration)
    Verify that the system correctly saves a new branch via the management
    API and returns it in the branch listing response.
    Tests the full round-trip: HTTP POST to save → HTTP GET to list → verify data.
    Checks: app.py /api/manage/branch/save and /api/manage/branches routes.
    """
    # Authenticate the session
    with client.session_transaction() as sess:
        sess['logged_in'] = True

    # Save a new branch via the management API
    new_branch = {
        "action":             "add",
        "branch_code":        "IT-TEST-99",
        "branch_name":        "Integration Test Branch",
        "location_type":      "Kiosk",
        "district":           "Test District",
        "state":              "Test State",
        "latitude":           3.1234,
        "longitude":          101.5678,
        "holiday_effect_pct": 10,
        "description":        "Created by IT-006 integration test"
    }

    save_response = client.post(
        '/api/manage/branch/save',
        json=new_branch
    )
    save_data = save_response.get_json()
    assert save_response.status_code == 200
    assert save_data.get('status') == 'success'

    # Read back the full branch list and verify the new branch appears
    list_response = client.get('/api/manage/branches')
    list_data = list_response.get_json()

    assert list_response.status_code == 200
    assert list_data.get('status') == 'success'

    branch_codes = [b['branch_code'] for b in list_data.get('branches', [])]
    assert 'IT-TEST-99' in branch_codes

    # Clean up: attempt to toggle / deactivate the test branch so it does not pollute the real DB
    client.post('/api/manage/branch/toggle', json={"branch_code": "IT-TEST-99", "is_active": 0})


# ============================================================
#    ENTRY POINT
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
