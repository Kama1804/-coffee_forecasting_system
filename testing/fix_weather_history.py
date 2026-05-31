import sqlite3
import requests
import time
from datetime import datetime

BRANCH_COORDS = {
    'STB-PJ1': {'lat': 2.9264, 'lon': 101.6964},
    'FT-PA1':  {'lat': 3.2353, 'lon': 101.4243}
}

def fetch_weather_logic(date_str, branch_id):
    coords = BRANCH_COORDS.get(branch_id)
    if not coords:
        return 'Cloudy'
    
    try:
        # 1. Isolate Daylight Blocks (07:00 - 19:00)
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude":   coords['lat'],
            "longitude":  coords['lon'],
            "start_date": date_str,
            "end_date":   date_str,
            "hourly":     ["weathercode", "precipitation"],
            "timezone":   "Asia/Kuala_Lumpur"
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        res_json = resp.json()
        
        if "hourly" not in res_json:
            return 'Cloudy'

        hourly_data = res_json["hourly"]
        times = hourly_data["time"]
        codes = hourly_data["weathercode"]
        precip = hourly_data["precipitation"]

        daylight_codes = []
        daylight_precip = 0.0

        for t, c, p in zip(times, codes, precip):
            hour = int(t.split('T')[1].split(':')[0])
            if 7 <= hour <= 19:
                daylight_codes.append(c)
                daylight_precip += p

        # 2. Check the Volume Threshold (2.5 mm)
        if daylight_precip < 2.5:
            # Apply Weighted Majority Vote for dry days
            # Fair / Sunny: 0, 1, 2
            # Cloudy: 3, 45, 48, 51, 53, 55
            sunny_count = sum(1 for c in daylight_codes if c in [0, 1, 2])
            cloudy_count = sum(1 for c in daylight_codes if c in [3, 45, 48, 51, 53, 55])
            
            if cloudy_count > sunny_count:
                return 'Cloudy'
            else:
                return 'Fair / Sunny'

        # 3. Identify Severe Lightning (Thunderstorm)
        if any(c in [95, 96, 99] for c in daylight_codes):
            return 'Thunderstorm'
        
        # Default if rain is >= 2.5mm but no thunderstorm
        return 'Raining'

    except Exception as e:
        print(f"Failed {date_str} ({branch_id}): {e}")
        return 'Cloudy'

def update_db_weather():
    db_path = 'database/coffee_shop.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all unique date-branch pairs in the DB
    cursor.execute("SELECT DISTINCT transaction_date, branch_id FROM sales_transaction")
    pairs = cursor.fetchall()
    
    print(f"Found {len(pairs)} unique date-branch pairs to recalibrate.")
    
    updated_count = 0

    for date_str, branch_id in pairs:
        new_condition = fetch_weather_logic(date_str, branch_id)
        
        # Update all transactions for this date/branch
        cursor.execute("""
            UPDATE sales_transaction 
            SET weather_condition = ? 
            WHERE transaction_date = ? AND branch_id = ?
        """, (new_condition, date_str, branch_id))
        
        updated_count += cursor.rowcount
        print(f"[{updated_count}] Recalibrated {date_str} ({branch_id}) -> {new_condition}")
        
        # Small sleep to respect API limits if many calls
        time.sleep(0.05)

    conn.commit()
    conn.close()
    print(f"\nDone! Total rows updated: {updated_count}")

if __name__ == "__main__":
    update_db_weather()
