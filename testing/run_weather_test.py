import requests
import json

def test_historical_weather(date_str, branch_id):
    BRANCH_COORDS = {
        'STB-PJ1': {'name': 'Putrajaya', 'lat': 2.9264, 'lon': 101.6964},
        'FT-PA1':  {'name': 'Puncak Alam', 'lat': 3.2353, 'lon': 101.4243}
    }

    coords = BRANCH_COORDS.get(branch_id)
    if not coords:
        print(f"Error: Invalid Branch ID '{branch_id}'")
        return

    print(f"\n--- Testing Past Weather for {coords['name']} ({branch_id}) ---")
    print(f"Target Date: {date_str}")

    try:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude":   coords['lat'],
            "longitude":  coords['lon'],
            "start_date": date_str,
            "end_date":   date_str,
            "daily":      "weathercode",
            "timezone":   "Asia/Kuala_Lumpur"
        }
        
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        res_json = resp.json()
        
        if "daily" in res_json and "weathercode" in res_json["daily"] and res_json["daily"]["weathercode"]:
            code = res_json["daily"]["weathercode"][0]
            
            # Map code using your system's exact logic
            condition = "Cloudy"
            if code in [0, 1, 2]: condition = "Sunny"
            elif code in [61, 63, 65, 80, 81, 82, 95, 96, 99]: condition = "Raining"
            
            print(f"API Response Code: {code}")
            print(f"System Interpretation: {condition}")
        else:
            print("Error: API returned no data for this date. (Note: Archive data usually takes 2-5 days to process)")
            
    except Exception as e:
        print(f"Connection Failed: {e}")

if __name__ == "__main__":
    # Testing for 28/5/2026 (formatted as 2026-05-28)
    test_date = '2026-05-28'
    test_historical_weather(test_date, 'STB-PJ1')
    test_historical_weather(test_date, 'FT-PA1')
