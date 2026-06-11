import requests
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
import time

# Load environment variables
load_dotenv()
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')

def get_branch_coords():
    """Fetches all active branch coordinates from the database."""
    try:
        db_path = os.environ.get('DB_PATH') or os.path.join('database', 'coffee_shop.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT branch_name, latitude, longitude FROM branch WHERE is_active = 1")
        rows = cursor.fetchall()
        conn.close()
        return {row[0]: {'lat': row[1], 'lon': row[2]} for row in rows}
    except Exception as e:
        print(f"[WEATHER COORD ERROR] {e}")
        return {}

def map_weather_condition(owm_main):
    """
    Maps OpenWeatherMap main conditions to Fair / Sunny / Cloudy / Raining / Thunderstorm.
    Tuned for Malaysian tropical climate based on user-defined matrix.
    """
    condition = owm_main.lower()

    if condition == 'clear':
        return 'Fair / Sunny'
    elif condition in ['clouds', 'mist', 'haze', 'fog', 'drizzle']:
        return 'Cloudy' # Drizzle is moved to Cloudy as per requirement
    elif condition in ['rain', 'squall', 'tornado']:
        return 'Raining'
    elif condition == 'thunderstorm':
        return 'Thunderstorm'
    else:
        return 'Cloudy' # Default safe fallback

def fetch_future_weather():
    """
    Fetches the 5-day forecast for all registered active branches with a 3-attempt RETRY LOGIC.
    """
    if not OPENWEATHER_API_KEY:
        return False, "Error: OpenWeatherMap API Key is missing from .env!"

    LOCATIONS = get_branch_coords()
    if not LOCATIONS:
        return False, "Error: No active branches found in database."

    forecast_data = {name: {} for name in LOCATIONS.keys()}

    for branch, coords in LOCATIONS.items():
        # Skip branches with zero coordinates
        if coords['lat'] == 0 and coords['lon'] == 0:
            continue

        url = f"https://api.openweathermap.org/data/2.5/forecast?lat={coords['lat']}&lon={coords['lon']}&appid={OPENWEATHER_API_KEY}&units=metric"
        
        # --- API RETRY LOGIC (Tries 3 times before failing) ---
        max_retries = 3
        api_success = False
        
        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=5) # 5 second timeout
                response.raise_for_status() 
                
                data = response.json()
                daily_stats = {}
                
                for item in data['list']:
                    dt_parts = item['dt_txt'].split(' ')
                    date_str = dt_parts[0]
                    time_str = dt_parts[1]
                    hour = int(time_str.split(':')[0])
                    
                    # --- BUSINESS HOURS FILTER (09:00 - 21:00) ---
                    if hour < 9 or hour > 21:
                        continue
                        
                    owm_main = item['weather'][0]['main']
                    temp = item['main']['temp']
                    pop = item.get('pop', 0) * 100 # Convert to percentage
                    mapped_condition = map_weather_condition(owm_main)
                    
                    if date_str not in daily_stats:
                        daily_stats[date_str] = {'conditions': [], 'temps': [], 'pops': []}
                    
                    daily_stats[date_str]['conditions'].append(mapped_condition)
                    daily_stats[date_str]['temps'].append(temp)
                    daily_stats[date_str]['pops'].append(pop)

                for date_str, stats in daily_stats.items():
                    # If for some reason no data in business hours, skip
                    if not stats['conditions']:
                        continue
                        
                    dominant_condition = max(set(stats['conditions']), key=stats['conditions'].count)
                    avg_temp = sum(stats['temps']) / len(stats['temps'])
                    avg_pop = sum(stats['pops']) / len(stats['pops']) 
                    
                    forecast_data[branch][date_str] = {
                        'condition': dominant_condition,
                        'temp': round(avg_temp, 1),
                        'pop': round(avg_pop, 0),
                        'rain_level': 'Light' if avg_pop <= 30 else ('Medium' if avg_pop <= 70 else 'Heavy')
                    }

                api_success = True
                break # Break out of the retry loop if successful

            except requests.exceptions.RequestException as e:
                print(f"Weather API attempt {attempt + 1} failed for {branch}: {e}")
                time.sleep(2) # Wait 2 seconds before trying again
                
        if not api_success:
            print(f"Warning: Failed to fetch weather for {branch} after {max_retries} attempts.")

    return True, forecast_data

# --- TEST BLOCK ---
if __name__ == '__main__':
    print("Testing OpenWeatherMap API Integration...\n")
    success, result = fetch_future_weather()
    
    if success:
        print("API CONNECTION SUCCESSFUL!\n")
        for branch, forecast in result.items():
            print(f"--- {branch} Future Forecast ---")
            for date, condition in forecast.items():
                print(f"{date}: {condition}")
            print("")
    else:
        print(f"API CONNECTION FAILED:\n{result}")
