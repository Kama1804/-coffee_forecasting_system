import pandas as pd
import random
import json
import os
from datetime import datetime, timedelta

print("Starting generation of DSS Dummy Data v4 (Uniform Clean Promo Names)...")

# Store ID Mappings
STORE_MAPPING = {
    'Putrajaya': 'STB-PJ1',
    'Puncak Alam': 'FT-PA1'
}

# Location-specific Staffing Matrices
STAFF_ROSTER = {
    'Putrajaya': ['Badrul', 'Iman', 'Tina', 'Elysya'],
    'Puncak Alam': ['Adam', 'Syafiq', 'Putri', 'Lina', 'Qayyum']
}

# --- SEPARATED MENU LOGIC ---
MENU_BASE_DICT = {
    'Espresso-based-Low': [
        ('Espresso-based', 'Hot Espresso', 'HOT', 4.60),
        ('Espresso-based', 'Hot Americano', 'HOT', 4.70),
        ('Espresso-based', 'Iced Americano', 'ICED', 5.50),
    ],
    'Latte-Medium': [
        ('Milk-based', 'Hot Latte', 'HOT', 5.60),
        ('Milk-based', 'Ice Latte', 'ICED', 6.50),
    ],
    'Rest-Above-Medium': [
        ('Milk-based', 'Hot Cappuccino', 'HOT', 6.60),
        ('Milk-based', 'Ice Cappuccino', 'ICED', 7.40),
        ('Milk-based', 'Hot Mocha', 'HOT', 7.50),
        ('Milk-based', 'Ice Mocha', 'ICED', 8.40),
        ('Ice Blended', 'Ice Blended Mocha', 'ICED', 10.20),
        ('Ice Blended', 'Ice Blended Chocolate Chip', 'ICED', 11.20)
    ],
    'Caramel-Macchiato': [
        ('Milk-based', 'Hot Caramel Macchiato', 'HOT', 10.90),
        ('Milk-based', 'Ice Caramel Macchiato', 'ICED', 12.90)
    ],
    'Matcha-Series': [
        ('Matcha Series', 'Matcha Latte', 'HOT', 9.90),
        ('Matcha Series', 'Ice Matcha Latte', 'ICED', 10.90)
    ]
}

# --- REQUIREMENT 3: Confirmed Ramadhan Windows ---
RAMADHAN_WINDOWS = {
    2024: ("2024-03-12", "2024-04-09"),
    2025: ("2025-03-02", "2025-03-30"),
    2026: ("2026-02-19", "2026-03-20"),
    2027: ("2027-02-08", "2027-03-09")
}

# Baseline standard open holiday promotions (Using matching clean UI names)
BASE_HOLIDAYS = {
    2024: {"2024-01-01": "New Year Promo", "2024-08-31": "Merdeka Deal"},
    2025: {"2025-01-01": "New Year Promo", "2025-02-01": "FT Day Promo"},
    2026: {"2026-01-01": "New Year Promo", "2026-02-01": "FT Day Promo"}
}

# Operational Matrix
EVENTS = {
    "Raya_Fitri_2024": ("2024-04-10", 3, 3, "Post-Raya Campaign"),
    "Raya_Fitri_2025": ("2025-03-31", 3, 3, "Post-Raya Campaign"),
    "Raya_Fitri_2026": ("2026-03-21", 3, 3, "Post-Raya Campaign"),
    "Raya_Fitri_2027": ("2027-03-10", 3, 3, "Post-Raya Campaign"),
    
    "Raya_Adha_2025":  ("2025-06-07", 3, 3, "Post-Raya Campaign"),
    "Raya_Adha_2026":  ("2026-05-27", 3, 3, "Post-Raya Campaign"),
    "Raya_Adha_2027":  ("2027-05-17", 3, 3, "Post-Raya Campaign"),
    
    "CNY_2025":        ("2025-01-29", 0, 2, "CNY Festive Campaign"),
    "CNY_2026":        ("2026-02-17", 0, 2, "CNY Festive Campaign"),
    "CNY_2027":        ("2027-02-06", 0, 2, "CNY Festive Campaign"),
    
    "Deepavali_2025":  ("2025-10-02", 0, 2, "Deepavali Campaign"),
    "Deepavali_2026":  ("2026-10-21", 0, 2, "Deepavali Campaign"),
    "Deepavali_2027":  ("2027-11-08", 0, 2, "Deepavali Campaign")
}

# Build Master Configurations
DATE_CONFIG = {}
for event_key, (start_date_str, closure_days, promo_days, ui_label) in EVENTS.items():
    start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
    for c_day in range(closure_days):
        closure_dt = start_dt + timedelta(days=c_day)
        DATE_CONFIG[closure_dt.strftime('%Y-%m-%d')] = {"status": "CLOSED", "promo": None}
    
    promo_start_dt = start_dt + timedelta(days=closure_days)
    for p_day in range(promo_days):
        promo_dt = promo_start_dt + timedelta(days=p_day)
        # Uniform Fix: Passing identical string value for both database code and display label
        DATE_CONFIG[promo_dt.strftime('%Y-%m-%d')] = {"status": "OPEN", "promo": (ui_label, ui_label)}

def is_date_in_ramadhan(date_obj):
    year = date_obj.year
    if year not in RAMADHAN_WINDOWS: return False
    start, end = RAMADHAN_WINDOWS[year]
    return datetime.strptime(start, '%Y-%m-%d').date() <= date_obj.date() <= datetime.strptime(end, '%Y-%m-%d').date()

def generate_timestamp(date_obj):
    if is_date_in_ramadhan(date_obj):
        h = random.choices([17, 18, 19, 20, 21, 22, 23], weights=[5, 35, 20, 10, 25, 3, 2])[0]
        minute = random.randint(0, 59)
        if h == 17: minute = random.randint(30, 59)
        dt = date_obj.replace(hour=h, minute=minute, second=random.randint(0,59))
    else:
        h = random.choices([9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20], 
                           weights=[5, 10, 15, 15, 10, 10, 10, 8, 10, 10, 5, 2])[0]
        minute = random.randint(0, 59)
        dt = date_obj.replace(hour=h, minute=minute, second=random.randint(0,59))
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')

def generate_modifiers(temp_type):
    mods = [temp_type]
    if temp_type == 'ICED' or random.random() < 0.40:
        num_mods = random.choices([1, 2], weights=[0.80, 0.20])[0]
        selected_mods = random.choices(["Less Ice", "Less Sweet", "No Sugar"], weights=[0.45, 0.35, 0.20], k=num_mods)
        for m in set(selected_mods):
            if temp_type == 'HOT' and m == 'Less Ice': continue
            mods.append(m)
    return json.dumps(mods)

def generate_payment_id(payment_method, date_str, sequence_num, prefix):
    unique_salt = random.randint(100000, 999999)
    if payment_method == 'QR':
        return f"{prefix}-QR-{date_str.replace('-', '')}-{sequence_num}-{unique_salt}"
    return f"{prefix}-CASH-{date_str.replace('-', '')}-{sequence_num}-{unique_salt}"

COLUMNS = [
    'Transaction_ID', 'Timestamp', 'Register_ID', 'Cashier_Name', 'Store_ID',
    'Item_Name', 'Item_Category', 'Quantity_Sold', 'Modifiers', 'Order_Type',
    'Gross_Sales', 'Discount_Amount', 'Promo_Code', 'Discount_Reason',
    'Tax_Amount', 'Net_Sales', 'Payment_Type'
]

# Run full output loops for outlets
for outlet_name, store_id in STORE_MAPPING.items():
    roster = STAFF_ROSTER[outlet_name]
    file_prefix = "PJ" if "PJ" in store_id else "PA"

    for target_year in [2024, 2025, 2026]:
        branch_year_data = []
        cash_sequence = random.randint(1000, 5000)
        start_date = datetime(target_year, 1, 1)
        end_date = datetime(target_year, 12, 31) if target_year < 2026 else datetime(2026, 6, 1)

        current_date = start_date
        year_base_holidays = BASE_HOLIDAYS.get(target_year, {})

        while current_date <= end_date:
            date_str = current_date.strftime('%Y-%m-%d')
            day_config = DATE_CONFIG.get(date_str, {"status": "OPEN", "promo": None})
            
            # Absolute Zero Sales on Sunday closures and Festive Shutdown windows
            if current_date.weekday() == 6 or day_config["status"] == "CLOSED":
                current_date += timedelta(days=1)
                continue

            target_cups = random.randint(50, 110)
            in_ramadhan = is_date_in_ramadhan(current_date)
            is_friday = (current_date.weekday() == 4)
            if in_ramadhan: target_cups = int(target_cups * 1.2)
            if day_config["promo"] is not None: target_cups = int(target_cups * 1.5)

            # Assign separate dynamic menu rules based on year matrix
            menu_pools = {
                'low': MENU_BASE_DICT['Espresso-based-Low'],
                'medium': MENU_BASE_DICT['Latte-Medium'],
                'above_medium': MENU_BASE_DICT['Rest-Above-Medium'].copy()
            }
            
            # Caramel Macchiato + June 1st Matcha triggers strictly for 2026 only
            if target_year == 2026:
                menu_pools['top'] = MENU_BASE_DICT['Caramel-Macchiato']
                if current_date >= datetime(2026, 6, 1):
                    menu_pools['above_medium'] += MENU_BASE_DICT['Matcha-Series']
                pool_choices = ['low', 'medium', 'above_medium', 'top']
                pool_weights = [0.10, 0.20, 0.30, 0.40] # Caramel Macchiato gets top spot
            else:
                # 2024 & 2025: Caramel Macchiato is completely locked out
                pool_choices = ['low', 'medium', 'above_medium']
                pool_weights = [0.15, 0.30, 0.55] 

            cups_sold_today = 0
            while cups_sold_today < target_cups:
                timestamp = generate_timestamp(current_date)
                selected_pool_name = random.choices(pool_choices, weights=pool_weights, k=1)[0]
                selected_pool = menu_pools[selected_pool_name]
                
                category, item_name, temp_type, base_price = random.choice(selected_pool)
                quantity = random.choices([1, 2, 3], weights=[0.85, 0.10, 0.05])[0]
                if cups_sold_today + quantity > target_cups: quantity = target_cups - cups_sold_today

                gross_sales = round(quantity * base_price, 2)
                discount_amount = 0.0
                applied_promo = "NONE"
                applied_reason = ""

                # Evaluate promotional rules
                if is_friday and random.random() < 0.3:
                    applied_promo = "Bogof Friday"
                    discount_amount = round(gross_sales * 0.50, 2)
                    applied_reason = "Buy 1 Free 1"
                elif date_str in year_base_holidays:
                    applied_promo = year_base_holidays[date_str]
                    discount_amount = round(gross_sales * 0.15, 2)
                    applied_reason = "Holiday 15% Off"
                elif day_config["promo"] is not None:
                    # Clean Matching: Storing uniform title string directly to column
                    applied_promo = day_config["promo"][0] 
                    applied_reason = day_config["promo"][1]
                    discount_amount = round(gross_sales * 0.10, 2)

                net_sales = round(gross_sales - discount_amount, 2)
                tax_amount = round(net_sales * 0.06, 2)
                net_sales = round(net_sales + tax_amount, 2)

                payment_method = random.choices(['QR', 'Cash'], weights=[0.80, 0.20])[0]
                txn_id = generate_payment_id(payment_method, date_str, cash_sequence, file_prefix)
                if payment_method == 'Cash': cash_sequence += 1

                branch_year_data.append([
                    txn_id, timestamp, "REG-1", random.choice(roster), store_id,
                    item_name, category, quantity, generate_modifiers(temp_type), "Takeaway",
                    gross_sales, discount_amount, applied_promo, applied_reason,
                    tax_amount, net_sales, payment_method
                ])
                cups_sold_today += quantity
            current_date += timedelta(days=1)

        df = pd.DataFrame(branch_year_data, columns=COLUMNS)
        df = df.sort_values(by=['Timestamp'])
        fn = f"MiniCoffee_Raw_{outlet_name.lower().replace(' ','_')}_{target_year}.csv"
        df.to_csv(fn, index=False)
        print(f" └─ Generated: {fn} ({len(df)} rows)")

print("\nSuccess! All CSV datasets updated with identical Promo Codes and UI display names.")