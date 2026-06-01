from forecast_engine import ForecastEngine
import os
import sys

# Ensure current directory is in path
sys.path.append(os.getcwd())

engine = ForecastEngine()

print("--- System Accuracy Report ---")

# Putrajaya
success, result = engine.generate_5_day_forecast('STB-PJ1', 'Putrajaya')
if success:
    print(f"Putrajaya (STB-PJ1): {result['accuracy']}%")
else:
    print(f"Putrajaya Error: {result}")

# Puncak Alam (Using FT-PA1 from DB)
success, result = engine.generate_5_day_forecast('FT-PA1', 'Puncak Alam')
if success:
    print(f"Puncak Alam (FT-PA1): {result['accuracy']}%")
else:
    print(f"Puncak Alam Error: {result}")
