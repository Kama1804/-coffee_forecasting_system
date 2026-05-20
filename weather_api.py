import requests
import os
from datetime import datetime
from dotenv import load_dotenv
import time

# Load environment variables
load_dotenv()
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')

# GPS Coordinates for your exact branch locations
LOCATIONS = {
    'Putrajaya': {'lat': 2.9264, 'lon': 101.6964},
    'Puncak Alam': {'lat': 3.2353, 'lon': 101.4243}
}

def map_weather_condition(owm_main):
    """
    Maps OpenWeatherMap main conditions to Sunny / Cloudy / Raining.
    Tuned for Malaysian tropical climate — drizzle treated as Cloudy
    to match Open-Meteo historical mapping in etl_pipeline.py.
    """
    condition = owm_main.lower()

    if condition == 'clear':
        return 'Sunny'
    elif condition in ['clouds', 'mist', 'haze', 'fog', 'drizzle']:
        return 'Cloudy'
    elif condition in ['rain', 'thunderstorm', 'squall', 'tornado']:
        return 'Raining'
    else:
        return 'Cloudy' # Default safe fallback

def fetch_future_weather():
    """
    Fetches the 5-day forecast for both branches with a 3-attempt RETRY LOGIC.
    """
    if not OPENWEATHER_API_KEY:
        return False, "Error: OpenWeatherMap API Key is missing from .env!"

    forecast_data = {'Putrajaya': {}, 'Puncak Alam': {}}

    for branch, coords in LOCATIONS.items():
        url = f"https://api.openweathermap.org/data/2.5/forecast?lat={coords['lat']}&lon={coords['lon']}&appid={OPENWEATHER_API_KEY}&units=metric"
        
        # --- API RETRY LOGIC (Tries 3 times before failing) ---
        max_retries = 3
        api_success = False
        
        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=5) # 5 second timeout
                response.raise_for_status() 
                
                data = response.json()
                daily_conditions = {}
                
                for item in data['list']:
                    date_str = item['dt_txt'].split(' ')[0]
                    owm_main = item['weather'][0]['main']
                    mapped_condition = map_weather_condition(owm_main)
                    
                    if date_str not in daily_conditions:
                        daily_conditions[date_str] = []
                    daily_conditions[date_str].append(mapped_condition)

                for date_str, conditions_list in daily_conditions.items():
                    dominant_condition = max(set(conditions_list), key=conditions_list.count)
                    forecast_data[branch][date_str] = dominant_condition

                api_success = True
                break # Break out of the retry loop if successful

            except requests.exceptions.RequestException as e:
                print(f"Weather API attempt {attempt + 1} failed for {branch}: {e}")
                time.sleep(2) # Wait 2 seconds before trying again
                
        if not api_success:
            return False, f"Failed to fetch weather for {branch} after 3 attempts."

    return True, forecast_data

# --- TEST BLOCK ---
# This block only runs if you execute this file directly.
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
        print("\nNote: If you see a '401 Unauthorized' error, OpenWeatherMap takes 1-2 hours to activate newly generated free API keys. Please wait and try again.")