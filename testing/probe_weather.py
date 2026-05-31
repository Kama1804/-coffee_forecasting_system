import requests
from datetime import datetime, timedelta

BRANCH_COORDS = {
    'STB-PJ1': {'lat': 2.9264, 'lon': 101.6964}, # Putrajaya
    'FT-PA1':  {'lat': 3.2353, 'lon': 101.4243}  # Puncak Alam
}

def probe():
    start_date = "2025-02-01"
    end_date = "2025-02-28"
    
    for branch_id, coords in BRANCH_COORDS.items():
        print(f"\n--- Branch: {branch_id} ---")
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": coords['lat'], "longitude": coords['lon'],
            "start_date": start_date, "end_date": end_date,
            "daily": ["weathercode", "precipitation_sum"], "timezone": "Asia/Kuala_Lumpur"
        }
        resp = requests.get(url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            dates = data["daily"]["time"]
            codes = data["daily"]["weathercode"]
            precip = data["daily"]["precipitation_sum"]
            for d, c, p in zip(dates, codes, precip):
                print(f"{d}: Code {c}, Precip {p}mm")
        else:
            print(f"Error: {resp.status_code}")

if __name__ == "__main__":
    probe()
