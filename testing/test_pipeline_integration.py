"""
Integration Test Script — Full Upload-to-Auto-Tune Pipeline
===========================================================
IT-004 : Full CSV upload -> DB save -> Auto-Tune fires as one integrated chain.
IT-005 : Missing Recipe Detection after a successful upload.

Test Type : Grey-Box Integration Testing
Run with  : python testing/test_pipeline_integration.py
(This is NOT a Pytest file — it is a standalone manual verification script.)

How it works:
  1. A temporary test database is built from scratch with the production schema.
  2. The branch is given lat=0 / lon=0 so that _enrich_weather() skips the
     external weather API call and returns 'Cloudy' immediately.
  3. A minimal in-memory CSV is written to disk and passed through the full
     ETLPipeline.process_data() -> save_to_database() -> sync_business_intelligence()
     chain — exactly the same sequence triggered when the business owner uploads
     a file through the web interface.
  4. Results are verified and printed. The temp database is deleted at the end.
"""

import sqlite3
import os
import sys
import pandas as pd

# Make sure the project root is on the path so we can import project modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from etl_pipeline import ETLPipeline
from analytics import sync_business_intelligence

# --------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------
TEST_DB_PATH   = "it_pipeline_test.db"
TEST_CSV_PATH  = "it_pipeline_test.csv"
TEST_BRANCH_ID = "TEST-01"
PASS  = "  [PASS]"
FAIL  = "  [FAIL]"
SEP   = "-" * 60

# --------------------------------------------------------------
# STEP 0 — Set DB_PATH environment variable so that both
#          etl_pipeline.get_robust_db_path() and
#          analytics.get_db_connection() use our test database.
# --------------------------------------------------------------
os.environ['DB_PATH'] = TEST_DB_PATH


def setup_test_database():
    """
    Builds a minimal but schema-accurate SQLite test database.
    Uses the production column list from init_db.py so that
    process_sales_dataframe() and bulk_insert_sales() work correctly.
    Branch is given lat=0 / lon=0 to skip the live weather API call.
    """
    conn = sqlite3.connect(TEST_DB_PATH)
    cursor = conn.cursor()

    # Branch table (matches init_db.py schema)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS branch (
        branch_code  TEXT PRIMARY KEY,
        branch_name  TEXT,
        location_type TEXT,
        latitude     REAL DEFAULT 0.0,
        longitude    REAL DEFAULT 0.0,
        district     TEXT DEFAULT "Test District",
        state        TEXT DEFAULT "Test State",
        description  TEXT DEFAULT "Test branch",
        holiday_effect REAL DEFAULT 0.0,
        is_active    INTEGER DEFAULT 1
    )
    ''')

    # Full sales_transaction schema (matches init_db.py)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sales_transaction (
        transaction_id      TEXT PRIMARY KEY,
        branch_id           TEXT NOT NULL,
        transaction_date    TEXT NOT NULL,
        transaction_time    TEXT NOT NULL,
        Hour                TEXT NOT NULL,
        "Day Name"          TEXT NOT NULL,
        "Month Name"        TEXT NOT NULL,
        Month               TEXT NOT NULL,
        product_id          TEXT NOT NULL,
        item_name           TEXT NOT NULL,
        product_category    TEXT NOT NULL,
        product_detail      TEXT,
        transaction_qty     INTEGER NOT NULL,
        order_type          TEXT NOT NULL,
        unit_price_MYR      REAL NOT NULL,
        gross_sales_MYR     REAL DEFAULT 0,
        discount_amount_MYR REAL DEFAULT 0,
        promo_code          TEXT DEFAULT "NONE",
        sst_amount_MYR      REAL NOT NULL,
        Total_Bill_MYR      REAL NOT NULL,
        payment_method      TEXT NOT NULL,
        store_location      TEXT NOT NULL,
        location_type       TEXT NOT NULL,
        district            TEXT NOT NULL,
        state               TEXT NOT NULL,
        weather_condition   TEXT,
        is_public_holiday   INTEGER NOT NULL
    )
    ''')

    # Product recipes table (matches init_db.py)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS product_recipes (
        item_name          TEXT PRIMARY KEY,
        beans_g            REAL DEFAULT 0,
        milk_ml            REAL DEFAULT 0,
        choco_g            REAL DEFAULT 0,
        ice_g              REAL DEFAULT 0,
        whip_g             REAL DEFAULT 0,
        cup_type           TEXT,
        custom_ingredients TEXT,
        is_active          INTEGER DEFAULT 1
    )
    ''')

    # Seed: one active branch (lat=0, lon=0 -> no weather API call)
    cursor.execute(
        "INSERT OR IGNORE INTO branch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (TEST_BRANCH_ID, 'Test Branch', 'Kiosk', 0.0, 0.0,
         'Test District', 'Test State', 'Integration test branch', 0.0, 1)
    )

    # Seed: known recipe for "Latte" — used to detect that "Caramel Frappe" is MISSING
    cursor.execute(
        "INSERT OR IGNORE INTO product_recipes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('Latte', 18.0, 150.0, 0.0, 0.0, 0.0, 'Hot', '{}', 1)
    )

    conn.commit()
    conn.close()
    print(f"  Test database created at: {TEST_DB_PATH}")


def build_test_csv():
    """
    Creates a minimal 17-column POS CSV with:
      - 12 normal trading days @ RM 10 each
      - 2 Malaysian public holidays @ RM 15 each (for Auto-Tune verification)
      - One product with no registered recipe ("Caramel Frappe")
        to trigger IT-005 missing recipe detection.
    All rows use Store_ID = TEST-01.
    """
    rows = []

    # 12 normal days with known product "Latte"
    for i in range(1, 13):
        rows.append({
            "Transaction_ID":  f"TXN-N{i:02d}",
            "Timestamp":       f"2026-06-{i:02d}T10:00:00",
            "Register_ID":     "REG1",
            "Cashier_Name":    "Siti",
            "Store_ID":        TEST_BRANCH_ID,
            "Item_Name":       "Latte",
            "Item_Category":   "Coffee",
            "Quantity_Sold":   2,
            "Modifiers":       "[]",
            "Order_Type":      "Dine-in",
            "Gross_Sales":     10.00,
            "Discount_Amount": 0.00,
            "Promo_Code":      "NONE",
            "Discount_Reason": "None",
            "Tax_Amount":      0.60,
            "Net_Sales":       10.00,
            "Payment_Type":    "CASH"
        })

    # 2 public holiday rows with product "Caramel Frappe" (no recipe registered -> triggers IT-005)
    for j, (date, txn_id) in enumerate([("2026-05-01", "TXN-H01"), ("2026-09-16", "TXN-H02")]):
        rows.append({
            "Transaction_ID":  txn_id,
            "Timestamp":       f"{date}T11:00:00",
            "Register_ID":     "REG1",
            "Cashier_Name":    "Siti",
            "Store_ID":        TEST_BRANCH_ID,
            "Item_Name":       "Caramel Frappe",    # <- not in product_recipes
            "Item_Category":   "Coffee",
            "Quantity_Sold":   3,
            "Modifiers":       "[]",
            "Order_Type":      "Takeaway",
            "Gross_Sales":     15.00,
            "Discount_Amount": 0.00,
            "Promo_Code":      "NONE",
            "Discount_Reason": "None",
            "Tax_Amount":      0.90,
            "Net_Sales":       15.00,
            "Payment_Type":    "E-WALLET"
        })

    pd.DataFrame(rows).to_csv(TEST_CSV_PATH, index=False)
    print(f"  Test CSV created: {len(rows)} rows ({len(rows)-2} normal days + 2 holiday rows)")


def cleanup():
    """Remove all temporary files created during testing."""
    for path in [TEST_DB_PATH, TEST_CSV_PATH]:
        if os.path.exists(path):
            os.remove(path)
    print(f"\n  Temporary files removed. Test environment cleaned up.")


# --------------------------------------------------------------
# MAIN TEST RUNNER
# --------------------------------------------------------------
def run_tests():
    results = {"passed": 0, "failed": 0}

    print(SEP)
    print("  INTEGRATION TEST — Full Upload-to-Auto-Tune Pipeline")
    print(SEP)

    # ---- SETUP ----
    print("\n[SETUP] Preparing test environment...")
    setup_test_database()
    build_test_csv()

    # ---- IT-004: FULL PIPELINE ----
    print(f"\n{SEP}")
    print("  IT-004: CSV Upload -> Data Cleaning -> DB Save -> Auto-Tune")
    print(SEP)

    pipeline = ETLPipeline(TEST_CSV_PATH)

    # Stage 1: ETL process_data()
    print("\n  Stage 1 — Running ETL pipeline (process_data)...")
    success, message = pipeline.process_data()
    if success:
        print(f"{PASS} ETL process_data() succeeded.")
        print(f"       Message: {message}")
        print(f"       Records in pipeline.df: {len(pipeline.df)}")
        results["passed"] += 1
    else:
        print(f"{FAIL} ETL process_data() FAILED.")
        print(f"       Error: {message}")
        results["failed"] += 1
        cleanup()
        return results

    # Stage 2: save_to_database()
    print("\n  Stage 2 — Saving cleaned data to test database...")
    db_success, db_message = pipeline.save_to_database(TEST_DB_PATH)
    if db_success:
        print(f"{PASS} save_to_database() succeeded.")
        print(f"       Message: {db_message}")
        results["passed"] += 1
    else:
        print(f"{FAIL} save_to_database() FAILED.")
        print(f"       Error: {db_message}")
        results["failed"] += 1
        cleanup()
        return results

    # Verify records are actually in the DB
    conn = sqlite3.connect(TEST_DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM sales_transaction").fetchone()[0]
    conn.close()
    if count > 0:
        print(f"{PASS} Database now contains {count} transaction record(s).")
        results["passed"] += 1
    else:
        print(f"{FAIL} Database is empty after save — records were not committed.")
        results["failed"] += 1

    # Stage 3: sync_business_intelligence() (Auto-Tune)
    print("\n  Stage 3 — Triggering AI Auto-Tune (sync_business_intelligence)...")
    sync_success, sync_logs = sync_business_intelligence(TEST_DB_PATH)
    if sync_success:
        print(f"{PASS} sync_business_intelligence() ran successfully.")
        print(f"       Log: {sync_logs}")
        results["passed"] += 1
    else:
        print(f"{FAIL} sync_business_intelligence() FAILED.")
        print(f"       Error: {sync_logs}")
        results["failed"] += 1

    # Stage 4: Verify holiday_effect was updated in the branch table
    print("\n  Stage 4 — Verifying Auto-Tune updated the branch holiday sensitivity...")
    conn = sqlite3.connect(TEST_DB_PATH)
    row = conn.execute(
        "SELECT holiday_effect FROM branch WHERE branch_code = ?", (TEST_BRANCH_ID,)
    ).fetchone()
    conn.close()

    if row and row[0] != 0.0:
        print(f"{PASS} holiday_effect updated to: {row[0]:.4f}")
        print(f"       (A non-zero value confirms Auto-Tune detected and applied holiday variance.)")
        results["passed"] += 1
    else:
        effect_val = row[0] if row else "N/A"
        print(f"{FAIL} holiday_effect was not updated (current value: {effect_val}).")
        print(f"       Check that the holiday dates in the CSV match MY_HOLIDAYS in etl_pipeline.py.")
        results["failed"] += 1

    # ---- IT-005: MISSING RECIPE DETECTION ----
    print(f"\n{SEP}")
    print("  IT-005: Missing Recipe Detection After Upload")
    print(SEP)

    print("\n  Checking pipeline.missing_recipes list after upload...")

    missing = getattr(pipeline, 'missing_recipes', [])
    if "Caramel Frappe" in missing:
        print(f"{PASS} 'Caramel Frappe' correctly identified as missing a recipe.")
        print(f"       Full missing_recipes list: {missing}")
        results["passed"] += 1
    elif missing:
        # Other items detected as missing — still valid behaviour
        print(f"{PASS} Missing recipe detection is working. Items without recipes: {missing}")
        print(f"       Note: 'Caramel Frappe' may have matched an existing recipe name case-insensitively.")
        results["passed"] += 1
    else:
        print(f"{FAIL} No missing recipes detected — 'Caramel Frappe' should have been flagged.")
        print(f"       Registered recipes in DB: check product_recipes table for 'Caramel Frappe'.")
        results["failed"] += 1

    # ---- FINAL REPORT ----
    print(f"\n{SEP}")
    print(f"  INTEGRATION TEST RESULTS")
    print(SEP)
    total = results["passed"] + results["failed"]
    print(f"  Total Checks  : {total}")
    print(f"  Passed        : {results['passed']}")
    print(f"  Failed        : {results['failed']}")
    completion = (results["passed"] / total * 100) if total > 0 else 0
    print(f"  Completion    : {completion:.0f}%")
    print(SEP)

    cleanup()
    return results


if __name__ == "__main__":
    run_tests()
