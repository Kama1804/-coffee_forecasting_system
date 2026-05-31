import sqlite3

conn   = sqlite3.connect('database/coffee_shop.db')
cursor = conn.cursor()

cursor.execute("""
    SELECT strftime('%Y-%m', sale_date) as month,
           weather_condition,
           COUNT(*) as cnt
    FROM sales_transaction
    GROUP BY month, weather_condition
    ORDER BY month
""")

rows = cursor.fetchall()
for r in rows:
    print(r)

conn.close()