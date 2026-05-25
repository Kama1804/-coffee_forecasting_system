from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify, make_response
import os
import sqlite3
import time
import io
from functools import wraps
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Core Module Dependencies
from init_db import initialize_database
from etl_pipeline import ETLPipeline
from gemini_agent import get_ai_insight, build_chat_system_context
from forecast_engine import ForecastEngine, MY_PUBLIC_HOLIDAYS, MY_SEASONS

load_dotenv(override=True)

app = Flask(__name__)

# ============================================================
#    CONFIGURATION
# ============================================================
app.secret_key       = os.getenv('FLASK_SECRET_KEY', 'fallback_secret_key')
ADMIN_USERNAME       = os.getenv('ADMIN_USERNAME')
ADMIN_PASSWORD       = os.getenv('ADMIN_PASSWORD')
OPENWEATHER_API_KEY  = os.getenv('OPENWEATHER_API_KEY')
GEMINI_API_KEY       = os.getenv('GEMINI_API_KEY')

if OPENWEATHER_API_KEY and GEMINI_API_KEY:
    print("System Check: Both API Keys successfully loaded from .env")
else:
    print("Warning: One or more API keys are missing!")

app.config['UPLOAD_FOLDER'] = 'uploads'
ALLOWED_EXTENSIONS = {'csv'}
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ============================================================
#    DATABASE INIT
# ============================================================
DB_PATH = os.path.join('database', 'coffee_shop.db')
if not os.path.exists(DB_PATH):
    print("Database not found. Running initialization script...")
    initialize_database()
else:
    print("Database found. System ready.")

# ============================================================
#    HELPERS
# ============================================================
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def build_where(branch_filter, time_filter, max_date, alias='s', branch_alias='b'):
    conds, params = [], []
    if branch_filter != 'all':
        conds.append(f"{branch_alias}.branch_name = ?")
        params.append(branch_filter)
    if time_filter == 'current_week':
        conds.append(f"{alias}.sale_date >= date(?, '-7 days')")
        params.append(max_date)
    elif time_filter.startswith('year_'):
        conds.append(f"strftime('%Y', {alias}.sale_date) = ?")
        params.append(time_filter.split('_')[1])
    elif time_filter.startswith('month_'):
        conds.append(f"strftime('%Y-%m', {alias}.sale_date) = ?")
        params.append(time_filter.split('_')[1])
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ============================================================
#    AUTH ROUTES
# ============================================================
@app.route('/')
def home():
    if 'logged_in' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'logged_in' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            flash('Welcome back! You are now logged in.', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Login Failed. Wrong username or password.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('You have been securely logged out.', 'success')
    return redirect(url_for('login'))


# ============================================================
#    PAGE ROUTES
# ============================================================
@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/forecast')
@login_required
def forecast():
    return render_template('forecast.html')

@app.route('/chatbot')
@login_required
def chatbot():
    return render_template('chatbot.html')

@app.route('/report')
@login_required
def report():
    return render_template('report.html')

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part detected.', 'error')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'error')
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            pipeline = ETLPipeline(filepath)
            success, message = pipeline.process_data()
            if success:
                db_success, db_message = pipeline.save_to_database(DB_PATH)
                if db_success:
                    flash(f'Success! {filename} was cleaned and loaded. {db_message}', 'success')
                else:
                    flash(f'Data cleaned but failed to save: {db_message}', 'error')
                    if os.path.exists(filepath):
                        os.remove(filepath)
            else:
                flash(f'ETL Error: {message}', 'error')
                if os.path.exists(filepath):
                    os.remove(filepath)
            return redirect(url_for('upload_file'))
        else:
            flash('Invalid file type. Please upload a .csv file only.', 'error')
            return redirect(request.url)

    profile = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as total FROM sales_transaction")
        total = cursor.fetchone()['total']
        cursor.execute("SELECT MIN(sale_date) as first, MAX(sale_date) as last FROM sales_transaction")
        dates = cursor.fetchone()
        cursor.execute("SELECT COUNT(DISTINCT product_category) as cats FROM sales_transaction")
        cats  = cursor.fetchone()['cats']
        cursor.execute("""
            SELECT b.branch_name, COUNT(*) as cnt
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            GROUP BY b.branch_name
        """)
        branches = cursor.fetchall()
        conn.close()
        profile = {
            'total_records': total,
            'date_from':     dates['first'] or 'N/A',
            'date_to':       dates['last']  or 'N/A',
            'categories':    cats,
            'branches':      [{'name': r['branch_name'], 'count': r['cnt']} for r in branches]
        }
    except Exception:
        profile = None

    past_uploads = []
    if os.path.exists(app.config['UPLOAD_FOLDER']):
        for filename in os.listdir(app.config['UPLOAD_FOLDER']):
            if filename.endswith('.csv'):
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                size_mb = os.path.getsize(filepath) / (1024 * 1024)
                mod_time = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%d %b %Y, %I:%M %p')
                past_uploads.append({
                    'filename': filename,
                    'size': round(size_mb, 2),
                    'date': mod_time,
                    'timestamp': os.path.getmtime(filepath)
                })
        past_uploads.sort(key=lambda x: x['timestamp'], reverse=True)

    return render_template('upload.html', profile=profile, past_uploads=past_uploads)


# ============================================================
#    API — DASHBOARD FILTERS
# ============================================================
@app.route('/api/dashboard_filters')
@login_required
def api_dashboard_filters():
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT strftime('%Y', sale_date) as yr "
            "FROM sales_transaction ORDER BY yr DESC"
        )
        years = [r['yr'] for r in cursor.fetchall() if r['yr']]
        cursor.execute(
            "SELECT DISTINCT strftime('%Y-%m', sale_date) as mo "
            "FROM sales_transaction ORDER BY mo DESC"
        )
        months = [r['mo'] for r in cursor.fetchall() if r['mo']]
        conn.close()
        return jsonify({"status": "success", "years": years, "months": months})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — KPI DATA
# ============================================================
@app.route('/api/kpis')
@login_required
def api_kpis():
    branch_filter = request.args.get('branch', 'all')
    time_filter   = request.args.get('time',   'all')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(sale_date) as max_d FROM sales_transaction")
        max_date = cursor.fetchone()['max_d'] or datetime.today().strftime('%Y-%m-%d')

        where, params = build_where(branch_filter, time_filter, max_date)
        join = "JOIN branch b ON s.branch_id = b.branch_id"

        cursor.execute(f"""
            SELECT COALESCE(SUM(s.total_revenue), 0) as rev,
                   COALESCE(SUM(s.quantity_sold), 0) as vol,
                   COUNT(s.transaction_id)           as txns
            FROM sales_transaction s {join} {where}
        """, params)
        metrics = cursor.fetchone()

        cursor.execute(f"""
            SELECT COUNT(DISTINCT s.sale_date) as active_days
            FROM sales_transaction s {join} {where}
        """, params)
        day_row     = cursor.fetchone()
        active_days = day_row['active_days'] or 1
        daily_avg   = round(metrics['rev'] / active_days, 2)

        cursor.execute(
            "SELECT strftime('%Y-%m', date(?, '-1 month')) as prev_mo",
            [max_date]
        )
        prev_mo = cursor.fetchone()['prev_mo']

        cursor.execute("""
            SELECT b.branch_name, SUM(s.total_revenue) as rev
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            WHERE strftime('%Y-%m', s.sale_date) = ?
            GROUP BY b.branch_name ORDER BY rev DESC LIMIT 1
        """, [prev_mo])
        tb         = cursor.fetchone()
        top_branch = tb['branch_name'] if tb else 'N/A'

        try:
            prev_dt    = datetime.strptime(prev_mo + '-01', '%Y-%m-%d')
            prev_label = prev_dt.strftime('%B %Y')
        except Exception:
            prev_label = prev_mo

        cursor.execute("""
            SELECT COALESCE(SUM(total_revenue), 0) as curr
            FROM sales_transaction
            WHERE strftime('%Y-%m', sale_date) = strftime('%Y-%m', ?)
        """, [max_date])
        curr_mo_rev = cursor.fetchone()['curr']

        cursor.execute("""
            SELECT COALESCE(SUM(total_revenue), 0) as prev
            FROM sales_transaction
            WHERE strftime('%Y-%m', sale_date) = ?
        """, [prev_mo])
        prev_mo_rev = cursor.fetchone()['prev']

        if prev_mo_rev > 0:
            pct_change  = ((curr_mo_rev - prev_mo_rev) / prev_mo_rev) * 100
            trend_label = f"+{pct_change:.1f}% vs {prev_label}" if pct_change >= 0 else f"{pct_change:.1f}% vs {prev_label}"
        else:
            trend_label = f"vs {prev_label}"

        cursor.execute(f"""
            SELECT s.sale_date, SUM(s.total_revenue) as rev
            FROM sales_transaction s {join} {where}
            GROUP BY s.sale_date
            ORDER BY s.sale_date DESC LIMIT 7
        """, params)
        spark_rows      = cursor.fetchall()
        sparkline_dates = [r['sale_date'] for r in reversed(spark_rows)]
        sparkline_revs  = [round(r['rev'], 2) for r in reversed(spark_rows)]

        conn.close()

        return jsonify({
            "status":              "success",
            "total_revenue":       round(metrics['rev'], 2),
            "volume_sold":         metrics['vol'],
            "total_transactions": metrics['txns'],
            "daily_average":      daily_avg,
            "top_branch":          top_branch,
            "trend_label":         trend_label,
            "txn_trend_label":    f"{metrics['txns']:,} total",
            "prev_month_label":   prev_label,
            "sparkline": {
                "labels": sparkline_dates,
                "data":   sparkline_revs
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — CHARTS DATA
# ============================================================
@app.route('/api/charts')
@login_required
def api_charts():
    branch_filter = request.args.get('branch', 'all')
    time_filter   = request.args.get('time',   'all')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(sale_date) as max_d FROM sales_transaction")
        max_date = cursor.fetchone()['max_d'] or datetime.today().strftime('%Y-%m-%d')

        where, params = build_where(branch_filter, time_filter, max_date)
        join = "JOIN branch b ON s.branch_id = b.branch_id"

        use_monthly = (time_filter == 'all' or time_filter.startswith('year_'))
        date_col    = "strftime('%Y-%m', s.sale_date)" if use_monthly else "s.sale_date"

        cursor.execute(f"""
            SELECT {date_col} as period, SUM(s.total_revenue) as rev
            FROM sales_transaction s {join} {where}
            GROUP BY period ORDER BY period ASC
        """, params)
        trend_rows = cursor.fetchall()

        trend_labels = []
        for r in trend_rows:
            p = r['period']
            if use_monthly and p:
                try:
                    dt = datetime.strptime(p + '-01', '%Y-%m-%d')
                    trend_labels.append(dt.strftime('%B %Y'))
                except Exception:
                    trend_labels.append(p)
            else:
                trend_labels.append(p)

        cursor.execute(f"""
            SELECT s.product_category as cat, SUM(s.total_revenue) as rev
            FROM sales_transaction s {join} {where}
            GROUP BY cat ORDER BY rev DESC
        """, params)
        cat_rows = cursor.fetchall()

        cursor.execute(f"""
            SELECT s.product_name, SUM(s.quantity_sold) as qty
            FROM sales_transaction s {join} {where}
            GROUP BY s.product_name ORDER BY qty DESC LIMIT 5
        """, params)
        top_prods = cursor.fetchall()

        cursor.execute(f"""
            SELECT s.product_name, SUM(s.quantity_sold) as qty
            FROM sales_transaction s {join} {where}
            GROUP BY s.product_name ORDER BY qty ASC LIMIT 3
        """, params)
        weak_prods = cursor.fetchall()

        cursor.execute(f"""
            SELECT s.payment_method, COUNT(*) as cnt
            FROM sales_transaction s {join} {where}
            GROUP BY s.payment_method ORDER BY cnt DESC
        """, params)
        pay_rows = cursor.fetchall()

        cursor.execute(f"""
            SELECT
                CASE strftime('%w', s.sale_date)
                    WHEN '0' THEN 'Sun' WHEN '1' THEN 'Mon'
                    WHEN '2' THEN 'Tue' WHEN '3' THEN 'Wed'
                    WHEN '4' THEN 'Thu' WHEN '5' THEN 'Fri'
                    WHEN '6' THEN 'Sat'
                END as day_name,
                strftime('%w', s.sale_date) as day_num,
                CAST(substr(s.transaction_time, 1, 2) AS INTEGER) as hour,
                COUNT(*) as txn_count
            FROM sales_transaction s {join} {where}
            GROUP BY day_num, hour ORDER BY day_num ASC, hour ASC
        """, params)
        heat_rows = cursor.fetchall()

        cursor.execute("""
            SELECT DISTINCT b.branch_name FROM branch b ORDER BY b.branch_name
        """)
        all_branches = [r['branch_name'] for r in cursor.fetchall()]

        cursor.execute("""
            SELECT DISTINCT strftime('%Y-%m', sale_date) as period
            FROM sales_transaction
            ORDER BY period ASC
        """)
        all_month_periods = [r['period'] for r in cursor.fetchall()]

        all_month_labels = []
        for p in all_month_periods:
            try:
                dt = datetime.strptime(p + '-01', '%Y-%m-%d')
                all_month_labels.append(dt.strftime('%B %Y'))
            except Exception:
                all_month_labels.append(p)

        monthly_by_branch = {b: [0] * len(all_month_periods) for b in all_branches}
        period_idx_map = {p: i for i, p in enumerate(all_month_periods)}

        cursor.execute("""
            SELECT strftime('%Y-%m', s.sale_date) as period,
                   b.branch_name,
                   SUM(s.total_revenue) as rev
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            GROUP BY period, b.branch_name
            ORDER BY period ASC
        """)
        for r in cursor.fetchall():
            if r['period'] in period_idx_map and r['branch_name'] in monthly_by_branch:
                monthly_by_branch[r['branch_name']][period_idx_map[r['period']]] = round(r['rev'], 2)

        conn.close()

        return jsonify({
            "status": "success",
            "trend": {
                "labels":     trend_labels,
                "raw":        [r['period'] for r in trend_rows],
                "data":       [round(r['rev'], 2) for r in trend_rows],
                "is_monthly": use_monthly
            },
            "monthly": {
                "labels":    all_month_labels,
                "raw":       all_month_periods,
                "branches":  all_branches,
                "by_branch": monthly_by_branch
            },
            "category": {
                "labels": [r['cat'] for r in cat_rows],
                "data":   [round(r['rev'], 2) for r in cat_rows]
            },
            "products": {
                "labels": [r['product_name'] for r in top_prods],
                "data":   [r['qty'] for r in top_prods]
            },
            "top_products": {
                "labels": [r['product_name'] for r in top_prods],
                "data":   [r['qty'] for r in top_prods]
            },
            "weak_products": {
                "labels": [r['product_name'] for r in weak_prods],
                "data":   [r['qty'] for r in weak_prods]
            },
            "payment": {
                "labels": [r['payment_method'] for r in pay_rows],
                "data":   [r['cnt'] for r in pay_rows]
            },
            "heatmap": [
                {'day': r['day_name'], 'hour': r['hour'], 'value': r['txn_count']}
                for r in heat_rows
            ]
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — AI CHATBOT WITH IN-MEMORY CONTEXT TTL CACHING
# ============================================================
GLOBAL_CHAT_CACHE = {
    "payload_dict": None,
    "expiry_timestamp": 0
}
CACHE_TTL_SECONDS = 300


def _fetch_db_context() -> dict:
    current_time = time.time()

    if GLOBAL_CHAT_CACHE["payload_dict"] and current_time < GLOBAL_CHAT_CACHE["expiry_timestamp"]:
        print("[CACHE ENGINE] Context served from memory cache.")
        return GLOBAL_CHAT_CACHE["payload_dict"]

    print("[CACHE ENGINE] Cache expired. Querying database...")

    conn   = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COALESCE(SUM(total_revenue),0) FROM sales_transaction")
    total_rev = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM sales_transaction")
    total_txns = cursor.fetchone()[0]

    cursor.execute("SELECT MIN(sale_date), MAX(sale_date) FROM sales_transaction")
    date_row   = cursor.fetchone()
    date_range = f"{date_row[0]} to {date_row[1]}" if date_row[0] else "N/A"
    max_date   = date_row[1] or datetime.today().strftime('%Y-%m-%d')

    cursor.execute("SELECT COUNT(DISTINCT sale_date) FROM sales_transaction")
    days_active = cursor.fetchone()[0] or 1
    daily_avg   = total_rev / days_active

    cursor.execute(
        "SELECT strftime('%Y-%m', date(?, '-1 month')) as prev_mo, "
        "       strftime('%Y-%m', ?) as curr_mo",
        [max_date, max_date]
    )
    mo_row   = cursor.fetchone()
    last_mo  = mo_row['prev_mo']
    curr_mo  = mo_row['curr_mo']

    try:
        last_mo_label = datetime.strptime(last_mo + '-01', '%Y-%m-%d').strftime('%B %Y')
        curr_mo_label = datetime.strptime(curr_mo + '-01', '%Y-%m-%d').strftime('%B %Y')
    except Exception:
        last_mo_label = last_mo
        curr_mo_label = curr_mo

    cursor.execute("""
        SELECT b.branch_name,
               SUM(s.total_revenue)  as rev,
               COUNT(*)              as txns,
               SUM(s.quantity_sold)  as qty,
               ROUND(SUM(s.total_revenue)/COUNT(DISTINCT s.sale_date),2) as daily_avg
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        WHERE strftime('%Y-%m', s.sale_date) = ?
        GROUP BY b.branch_name ORDER BY rev DESC
    """, [last_mo])
    last_mo_rows = cursor.fetchall()

    if last_mo_rows:
        last_mo_branch_lines = []
        for r in last_mo_rows:
            last_mo_branch_lines.append(
                f"  - {r['branch_name']}: RM {r['rev']:,.2f} revenue | "
                f"{r['txns']} transactions | {r['qty']} items sold | "
                f"RM {r['daily_avg']:,.2f} daily avg"
            )
        winner = last_mo_rows[0]
        loser  = last_mo_rows[-1] if len(last_mo_rows) > 1 else None
        diff   = winner['rev'] - (loser['rev'] if loser else 0)
        winner_note = (
            f"  → {winner['branch_name']} led by RM {diff:,.2f} "
            f"({((diff/loser['rev'])*100):.1f}% more)" if loser and loser['rev'] > 0 else ""
        )
        last_mo_branch_summary = "\n".join(last_mo_branch_lines)
        if winner_note:
            last_mo_branch_summary += "\n" + winner_note
    else:
        last_mo_branch_summary = f"  No data found for {last_mo_label}."

    cursor.execute("""
        SELECT b.branch_name,
               SUM(s.total_revenue) as rev,
               COUNT(*)              as txns,
               MAX(s.sale_date)     as last_day
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        WHERE strftime('%Y-%m', s.sale_date) = ?
        GROUP BY b.branch_name ORDER BY rev DESC
    """, [curr_mo])
    curr_mo_rows = cursor.fetchall()

    if curr_mo_rows:
        curr_mo_lines = [
            f"  - {r['branch_name']}: RM {r['rev']:,.2f} | {r['txns']} transactions (up to {r['last_day']})"
            for r in curr_mo_rows
        ]
        curr_mo_branch_summary = "\n".join(curr_mo_lines)
    else:
        curr_mo_branch_summary = f"  No data yet for {curr_mo_label}."

    trend_date_clause = "WHERE s.sale_date >= date(?, '-6 months')"
    trend_params = [max_date]

    cursor.execute(f"""
        SELECT b.branch_name,
               strftime('%Y-%m', s.sale_date) as month,
               SUM(s.total_revenue) as rev,
               COUNT(*) as txns
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {trend_date_clause}
        GROUP BY b.branch_name, month
        ORDER BY b.branch_name, month ASC
    """, trend_params)
    monthly_trend_rows = cursor.fetchall()

    monthly_trend_by_branch = {}
    for r in monthly_trend_rows:
        bn = r['branch_name']
        if bn not in monthly_trend_by_branch:
            monthly_trend_by_branch[bn] = []
        try:
            label = datetime.strptime(r['month'] + '-01', '%Y-%m-%d').strftime('%b %Y')
        except Exception:
            label = r['month']
        monthly_trend_by_branch[bn].append(f"{label}: RM {r['rev']:,.2f} ({r['txns']} txns)")

    monthly_trend_lines = []
    for branch, months in monthly_trend_by_branch.items():
        monthly_trend_lines.append(f"  {branch}:")
        monthly_trend_lines += [f"    {m}" for m in months]
    monthly_trend_summary = "\n".join(monthly_trend_lines) if monthly_trend_lines else "  No trend data."

    cursor.execute("""
        SELECT b.branch_name, SUM(s.total_revenue) as rev, COUNT(*) as txns
        FROM sales_transaction s JOIN branch b ON s.branch_id = b.branch_id
        GROUP BY b.branch_name ORDER BY rev DESC
    """)
    branch_rows    = cursor.fetchall()
    branch_summary = "\n".join([
        f"  - {r['branch_name']}: RM {r['rev']:,.2f} ({r['txns']} transactions) [all-time]"
        for r in branch_rows
    ])
    top_branch = branch_rows[0]['branch_name'] if branch_rows else 'N/A'

    cursor.execute("""
        SELECT CAST(substr(transaction_time,1,2) AS INTEGER) as hr, COUNT(*) as cnt
        FROM sales_transaction GROUP BY hr ORDER BY cnt DESC LIMIT 1
    """)
    peak_row  = cursor.fetchone()
    peak_hour = f"{peak_row['hr']:02d}:00–{peak_row['hr']+1:02d}:00" if peak_row else "N/A"

    cursor.execute("""
        SELECT product_name, SUM(quantity_sold) as qty, SUM(total_revenue) as rev
        FROM sales_transaction GROUP BY product_name ORDER BY qty DESC LIMIT 3
    """)
    top_products_rows = cursor.fetchall()
    top_products = "\n".join([
        f"  - {r['product_name']}: {r['qty']} units (RM {r['rev']:,.2f})"
        for r in top_products_rows
    ])

    cursor.execute("""
        SELECT product_category, SUM(total_revenue) as rev
        FROM sales_transaction GROUP BY product_category ORDER BY rev DESC
    """)
    categories = "\n".join([
        f"  - {r['product_category']}: RM {r['rev']:,.2f}"
        for r in cursor.fetchall()
    ])

    cursor.execute("""
        SELECT weather_condition,
               COUNT(DISTINCT sale_date) as days,
               ROUND(SUM(total_revenue)/COUNT(DISTINCT sale_date),2) as avg_rev
        FROM sales_transaction GROUP BY weather_condition ORDER BY avg_rev DESC
    """)
    weather_summary = "\n".join([
        f"  - {r['weather_condition']}: RM {r['avg_rev']:,.2f} avg/day ({r['days']} days)"
        for r in cursor.fetchall()
    ])

    cursor.execute("""
        SELECT b.branch_name, f.forecast_date, f.predicted_revenue
        FROM sales_forecast f JOIN branch b ON f.branch_id = b.branch_id
        WHERE f.forecast_date > (SELECT COALESCE(MAX(sale_date), '1970-01-01') FROM sales_transaction)
        ORDER BY f.forecast_date ASC
    """)
    forecast_rows    = cursor.fetchall()
    forecast_summary = "\n".join([
        f"  - {r['branch_name']} {r['forecast_date']}: RM {r['predicted_revenue']:,.2f}"
        for r in forecast_rows
    ]) if forecast_rows else "  No forecast generated yet."

    conn.close()

    top_products_list = [r['product_name'] for r in top_products_rows]
    top_products_rev  = [float(r['rev']) for r in top_products_rows]
    branch_names_list = [r['branch_name'] for r in branch_rows]
    branch_rev_list   = [float(r['rev']) for r in branch_rows]

    current_day = datetime.now().day
    is_payday_window = 25 <= current_day <= 28
    payday_status_text = (
        "Currently inside the active monthly PAYDAY window (Expect significantly higher customer purchasing volume)."
        if is_payday_window else "Standard operating period (Normal baseline consumer spending patterns)."
    )

    compiled_payload = {
        'date_range':              date_range,
        'total_rev':               total_rev,
        'total_txns':              total_txns,
        'daily_avg':               daily_avg,
        'peak_hour':               peak_hour,
        'top_branch':              top_branch,
        'branch_summary':          branch_summary,
        'payday_context':          payday_status_text,
        'last_mo_label':          last_mo_label,
        'curr_mo_label':          curr_mo_label,
        'last_mo_branch_summary': last_mo_branch_summary,
        'curr_mo_branch_summary': curr_mo_branch_summary,
        'monthly_trend_summary':  monthly_trend_summary,
        'top_products':            top_products,
        'categories':              categories,
        'weather_summary':         weather_summary,
        'forecast_summary':        forecast_summary,
        'arr_products':            top_products_list,
        'arr_product_revs':        top_products_rev,
        'arr_branches':            branch_names_list,
        'arr_branch_revs':         branch_rev_list
    }

    GLOBAL_CHAT_CACHE["payload_dict"] = compiled_payload
    GLOBAL_CHAT_CACHE["expiry_timestamp"] = current_time + CACHE_TTL_SECONDS
    return compiled_payload


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({"status": "error", "message": "No message provided."}), 400

    user_message = data['message'].strip()
    if not user_message:
        return jsonify({"status": "error", "message": "Empty message."}), 400

    if 'chat_history' not in session:
        session['chat_history'] = []

    try:
        db_data = _fetch_db_context()
        base_system_context = build_chat_system_context(db_data)

        temporal_context_extension = f"""
=== CURRENT MALAYSIAN TEMPORAL CONTEXT ===
- Current Date & Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}
- Payday Cycle Status: {db_data.get('payday_context', 'N/A')}
"""

        chart_rules_extension = f"""
📊 INTERACTIVE IN-CHAT CHART RULE:
If the user explicitly asks for a graph, visual breakdown, revenue trend chart, or sales comparison, you MUST generate and append a structured data block at the very end of your response text.
Format it strictly on its own new line (Do not wrap with markdown code blocks or add spaces inside tags):
[CHART_DATA={{"type":"bar","labels":["Putrajaya","Puncak Alam"],"values":{db_data['arr_branch_revs']},"title":"All-Time Branch Revenue Comparison"}}]

AVAILABLE CHART PAYLOAD MAPS:
1. Branch Comparison: type="bar", labels={db_data['arr_branches']}, values={db_data['arr_branch_revs']}, title="All-Time Branch Revenue Comparison"
2. Top Products: type="bar", labels={db_data['arr_products']}, values={db_data['arr_product_revs']}, title="Top Product Demand Mix"
3. Revenue Trends: Use "line" chart, populate labels with past months and values from monthly trend data.

Ensure values match context numbers exactly. If the user does not request a visual, do not append any [CHART_DATA] tag.
"""
        system_context = f"{base_system_context}\n{temporal_context_extension}\n{chart_rules_extension}"

    except Exception as e:
        print(f"[CHAT ERROR] Database context failure: {e}")
        system_context = (
            "You are the AI Business Advisor for 'Mini Coffee Shop'. "
            "Database connection is temporarily unavailable. Please retry shortly."
        )

    history_blocks = []
    for turn in session['chat_history']:
        history_blocks.append(f"User: {turn['user']}\nAI: {turn['bot']}")

    history_text = "\n\n".join(history_blocks)

    final_prompt = f"""{system_context}

=== CONVERSATION HISTORY (last {len(session['chat_history'])} turns) ===
{history_text if history_text else "(No prior conversation)"}

=== INCOMING MESSAGE ===
User: {user_message}
AI:"""

    from gemini_agent import get_ai_insight
    success, ai_response = get_ai_insight(final_prompt)

    if success:
        history_cache = session['chat_history']
        history_cache.append({"user": user_message, "bot": ai_response})
        if len(history_cache) > 10:
            history_cache.pop(0)
        session['chat_history'] = history_cache
        session.modified = True
        return jsonify({"status": "success", "response": ai_response})
    else:
        return jsonify({"status": "error", "response": ai_response}), 503


@app.route('/api/chat/clear', methods=['POST'])
@login_required
def api_clear_chat():
    session.pop('chat_history', None)
    session.modified = True
    return jsonify({"status": "success", "message": "Memory cleared."})


@app.route('/api/chat/restore', methods=['POST'])
@login_required
def api_restore_chat():
    data = request.get_json()
    if not data or 'history' not in data:
        return jsonify({"status": "error", "message": "No history provided."}), 400

    pairs = data['history']
    if not isinstance(pairs, list):
        return jsonify({"status": "error", "message": "History must be a list."}), 400

    sanitized = []
    for turn in pairs:
        if isinstance(turn, dict) and 'user' in turn and 'bot' in turn:
            sanitized.append({
                "user": str(turn['user'])[:2000],
                "bot":  str(turn['bot'])[:4000]
            })

    session['chat_history'] = sanitized[-10:]
    session.modified = True

    return jsonify({
        "status": "success",
        "restored_turns": len(session['chat_history']),
        "message": f"Restored {len(session['chat_history'])} conversation turns."
    })


# ============================================================
#    API — FORECAST ENGINE INTERFACE
# ============================================================
@app.route('/api/forecast')
@login_required
def api_forecast():
    branch_id   = request.args.get('branch_id',   1,            type=int)
    branch_name = request.args.get('branch_name', 'Putrajaya', type=str)
    try:
        engine          = ForecastEngine()
        success, result = engine.generate_7_day_forecast(branch_id, branch_name)
        if success:
            return jsonify({
                "status":              "success",
                "mape":                result['mape'],
                "rmse":                result['rmse'],
                "accuracy":            result.get('accuracy', 0),
                "persona":             result.get('persona', ''),
                "historical":          result['historical'],
                "forecast":            result['forecast'],
                "hourly":              result.get('hourly', []),
                "forecast_vs_actual": result.get('forecast_vs_actual', []),
                "insample_fit":        result.get('insample_fit', []),
                "weather_by_time":     result.get('weather_by_time', [])
            })
        else:
            return jsonify({"status": "error", "message": result}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — AVAILABLE REPORT MONTHS (for month picker disabling)
# ============================================================
@app.route('/api/report_months')
@login_required
def api_report_months():
    """Returns all YYYY-MM values that have sales transaction data."""
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT strftime('%Y-%m', sale_date) as mo "
            "FROM sales_transaction ORDER BY mo ASC"
        )
        months = [r['mo'] for r in cursor.fetchall() if r['mo']]
        conn.close()
        return jsonify({"status": "success", "months": months})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — REPORT DATA (shared by preview + PDF)
# ============================================================
def _build_report_data(branch_filter, date_from, date_to):
    """
    Builds the full Executive Report data payload.
    date_from / date_to must both be provided (YYYY-MM-DD).
    """
    conn   = get_db_connection()
    cursor = conn.cursor()

    branch_cond  = "b.branch_name = ?" if branch_filter != 'all' else None
    date_cond    = "s.sale_date BETWEEN ? AND ?" if date_from and date_to else None
    conditions   = [c for c in [branch_cond, date_cond] if c]
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    def base_params():
        p = []
        if branch_cond:
            p.append(branch_filter)
        if date_cond:
            p.extend([date_from, date_to])
        return p

    period_str = f"{date_from} to {date_to}" if date_from and date_to else "All data"

    # ── Daily data ────────────────────────────────────────────
    cursor.execute(f"""
        SELECT s.sale_date as date,
               ROUND(SUM(s.total_revenue), 2) as revenue,
               COUNT(*) as txns
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY s.sale_date
        ORDER BY s.sale_date ASC
    """, base_params())
    daily_data    = [dict(r) for r in cursor.fetchall()]
    period_revenue = sum(r['revenue'] for r in daily_data)
    period_txns    = sum(r['txns']    for r in daily_data)

    # Calendar days for daily average
    if date_from and date_to:
        try:
            calendar_days = max((
                datetime.strptime(date_to, '%Y-%m-%d') -
                datetime.strptime(date_from, '%Y-%m-%d')
            ).days + 1, 1)
        except Exception:
            calendar_days = len(daily_data) or 1
    else:
        calendar_days = len(daily_data) or 1

    daily_avg = round(period_revenue / calendar_days, 2)
    aov       = round(period_revenue / period_txns, 2) if period_txns > 0 else 0.0

    # ── Period-over-period trend ───────────────────────────────
    trend_label = "No prior period data"
    if daily_data:
        try:
            fd  = datetime.strptime(date_from, '%Y-%m-%d')
            ld  = datetime.strptime(date_to,   '%Y-%m-%d')
            span = (ld - fd).days + 1
            prev_from = (fd - timedelta(days=span)).strftime('%Y-%m-%d')
            prev_to   = (fd - timedelta(days=1)).strftime('%Y-%m-%d')

            prev_cond   = ("b.branch_name = ? AND " if branch_filter != 'all' else "") + \
                          "s.sale_date BETWEEN ? AND ?"
            prev_params = ([branch_filter] if branch_filter != 'all' else []) + [prev_from, prev_to]

            cursor.execute(f"""
                SELECT COALESCE(SUM(s.total_revenue), 0) as prev_rev
                FROM sales_transaction s
                JOIN branch b ON s.branch_id = b.branch_id
                WHERE {prev_cond}
            """, prev_params)
            prev_rev = cursor.fetchone()['prev_rev']

            if prev_rev > 0:
                pct  = ((period_revenue - prev_rev) / prev_rev) * 100
                sign = "+" if pct >= 0 else ""
                trend_label = f"{sign}{pct:.1f}% vs prior period"
        except Exception:
            trend_label = "Trend N/A"

    # ── Peak hour ──────────────────────────────────────────────
    cursor.execute(f"""
        SELECT CAST(substr(s.transaction_time, 1, 2) AS INTEGER) as hr, COUNT(*) as cnt
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY hr ORDER BY cnt DESC LIMIT 1
    """, base_params())
    ph        = cursor.fetchone()
    peak_hour = f"{int(ph['hr']):02d}:00 – {int(ph['hr'])+1:02d}:00" \
                if ph and ph['hr'] is not None else "N/A"

    # ── Hourly breakdown ───────────────────────────────────────
    cursor.execute(f"""
        SELECT CAST(substr(s.transaction_time, 1, 2) AS INTEGER) as hr,
               COUNT(*) as txn_count,
               ROUND(SUM(s.total_revenue), 2) as revenue
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY hr ORDER BY hr ASC
    """, base_params())
    hourly_breakdown = [
        {'hour': f"{r['hr']:02d}:00", 'txns': r['txn_count'], 'revenue': r['revenue']}
        for r in cursor.fetchall() if r['hr'] is not None
    ]

    # ── Peak day ───────────────────────────────────────────────
    cursor.execute(f"""
        SELECT CASE strftime('%w', s.sale_date)
            WHEN '0' THEN 'Sunday'   WHEN '1' THEN 'Monday'
            WHEN '2' THEN 'Tuesday'  WHEN '3' THEN 'Wednesday'
            WHEN '4' THEN 'Thursday' WHEN '5' THEN 'Friday'
            WHEN '6' THEN 'Saturday'
        END as day_name, COUNT(*) as cnt
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY day_name ORDER BY cnt DESC LIMIT 1
    """, base_params())
    pd_row   = cursor.fetchone()
    peak_day = pd_row['day_name'] if pd_row else "N/A"

    # ── Day-of-week breakdown ──────────────────────────────────
    cursor.execute(f"""
        SELECT
            CASE strftime('%w', s.sale_date)
                WHEN '0' THEN 'Sunday'   WHEN '1' THEN 'Monday'
                WHEN '2' THEN 'Tuesday'  WHEN '3' THEN 'Wednesday'
                WHEN '4' THEN 'Thursday' WHEN '5' THEN 'Friday'
                WHEN '6' THEN 'Saturday'
            END as day_name,
            strftime('%w', s.sale_date) as day_num,
            COUNT(*) as txn_count,
            ROUND(SUM(s.total_revenue), 2) as revenue
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY day_num ORDER BY day_num ASC
    """, base_params())
    dow_breakdown = [
        {'day': r['day_name'], 'txns': r['txn_count'], 'revenue': r['revenue']}
        for r in cursor.fetchall()
    ]

    # ── Top branch ─────────────────────────────────────────────
    cursor.execute(f"""
        SELECT b.branch_name, SUM(s.total_revenue) as rev
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY b.branch_name ORDER BY rev DESC LIMIT 1
    """, base_params())
    tb_row     = cursor.fetchone()
    top_branch = tb_row['branch_name'] if tb_row else 'N/A'

    # ── Regional breakdown (all branches, period-filtered) ─────
    cursor.execute(f"""
        SELECT b.branch_name,
               ROUND(SUM(s.total_revenue), 2) as rev,
               COUNT(*) as txns
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY b.branch_name ORDER BY rev DESC
    """, base_params())
    regional_breakdown = [dict(r) for r in cursor.fetchall()]
    branch_max_rev = max((r['rev'] for r in regional_breakdown), default=1)

    # ── Product performance ────────────────────────────────────
    cursor.execute(f"""
        SELECT s.product_name,
               SUM(s.quantity_sold) as qty,
               ROUND(SUM(s.total_revenue), 2) as revenue,
               ROUND(SUM(s.total_revenue) * 100.0 /
                     NULLIF((SELECT SUM(s2.total_revenue)
                             FROM sales_transaction s2
                             JOIN branch b2 ON s2.branch_id = b2.branch_id
                             {where_clause}), 0), 1) as pct
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY s.product_name ORDER BY qty DESC LIMIT 5
    """, base_params() + base_params())
    top_products = [dict(r) for r in cursor.fetchall()]

    cursor.execute(f"""
        SELECT s.product_name,
               SUM(s.quantity_sold) as qty,
               ROUND(SUM(s.total_revenue), 2) as revenue
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY s.product_name ORDER BY qty ASC LIMIT 3
    """, base_params())
    bottom_products = [dict(r) for r in cursor.fetchall()]

    # ── Category breakdown ─────────────────────────────────────
    cursor.execute(f"""
        SELECT s.product_category,
               ROUND(SUM(s.total_revenue), 2) as revenue,
               ROUND(SUM(s.total_revenue) * 100.0 /
                     NULLIF((SELECT SUM(s2.total_revenue)
                             FROM sales_transaction s2
                             JOIN branch b2 ON s2.branch_id = b2.branch_id
                             {where_clause}), 0), 1) as pct
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY s.product_category ORDER BY revenue DESC
    """, base_params() + base_params())
    category_breakdown = [dict(r) for r in cursor.fetchall()]

    # ── Payment method breakdown ───────────────────────────────
    cursor.execute(f"""
        SELECT s.payment_method,
               COUNT(*) as txn_count,
               ROUND(COUNT(*) * 100.0 /
                     NULLIF((SELECT COUNT(*)
                             FROM sales_transaction s2
                             JOIN branch b2 ON s2.branch_id = b2.branch_id
                             {where_clause}), 0), 1) as pct
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY s.payment_method ORDER BY txn_count DESC
    """, base_params() + base_params())
    payment_breakdown = [dict(r) for r in cursor.fetchall()]

    # ── Monthly trend — ALL months (unfiltered, for Section 2) ─
    cursor.execute("SELECT DISTINCT branch_name FROM branch ORDER BY branch_name")
    all_branches = [r[0] for r in cursor.fetchall()]

    cursor.execute("""
        SELECT DISTINCT strftime('%Y-%m', sale_date) as month
        FROM sales_transaction ORDER BY month ASC
    """)
    all_month_keys = [r['month'] for r in cursor.fetchall()]
    all_month_labels = []
    for m in all_month_keys:
        try:
            all_month_labels.append(datetime.strptime(m + '-01', '%Y-%m-%d').strftime('%b %Y'))
        except Exception:
            all_month_labels.append(m)

    by_branch_monthly = {b: [0]*len(all_month_keys) for b in all_branches}
    cursor.execute("""
        SELECT strftime('%Y-%m', s.sale_date) as month, b.branch_name,
               SUM(s.total_revenue) as rev
        FROM sales_transaction s JOIN branch b ON s.branch_id = b.branch_id
        GROUP BY month, b.branch_name
    """)
    month_idx = {m: i for i, m in enumerate(all_month_keys)}
    for r in cursor.fetchall():
        if r['month'] in month_idx and r['branch_name'] in by_branch_monthly:
            by_branch_monthly[r['branch_name']][month_idx[r['month']]] = round(r['rev'], 2)

    monthly_trend = {
        "labels":    all_month_labels,
        "keys":      all_month_keys,
        "branches":  all_branches,
        "by_branch": by_branch_monthly
    }

    # ── Forecast data (upcoming 7-day) ────────────────────────
    fc_conds  = ["b.branch_name = ?"] if branch_filter != 'all' else []
    fc_conds.append("f.forecast_date > (SELECT COALESCE(MAX(sale_date), '1970-01-01') FROM sales_transaction)")
    fc_where  = "WHERE " + " AND ".join(fc_conds)
    fc_params = [branch_filter] if branch_filter != 'all' else []

    try:
        cursor.execute(f"""
            SELECT f.forecast_date as ds,
                   f.predicted_revenue as yhat,
                   f.lower_bound_revenue as yhat_lower,
                   f.upper_bound_revenue as yhat_upper
            FROM sales_forecast f
            JOIN branch b ON f.branch_id = b.branch_id
            {fc_where} ORDER BY f.forecast_date ASC LIMIT 7
        """, fc_params)
        forecast_data = [dict(r) for r in cursor.fetchall()]
    except Exception:
        cursor.execute(f"""
            SELECT f.forecast_date as ds,
                   f.predicted_revenue as yhat,
                   f.predicted_revenue * 0.95 as yhat_lower,
                   f.predicted_revenue * 1.05 as yhat_upper
            FROM sales_forecast f
            JOIN branch b ON f.branch_id = b.branch_id
            {fc_where} ORDER BY f.forecast_date ASC LIMIT 7
        """, fc_params)
        forecast_data = [dict(r) for r in cursor.fetchall()]

    # ── Holiday tagging ────────────────────────────────────────
    holiday_set = {h[0] for h in MY_PUBLIC_HOLIDAYS}
    for row in forecast_data:
        dt_obj = datetime.strptime(row['ds'], '%Y-%m-%d')
        row['is_holiday'] = row['ds'] in holiday_set
        row['is_friday']  = (dt_obj.weekday() == 4)

    # ── Predicted vs Actual (Prophet in-sample fit for selected month) ──
    # Joins sales_forecast insample fitted values against actual daily revenue
    # for dates within the selected reporting period.
    pva_fc_conds  = ["f.forecast_date BETWEEN ? AND ?"]
    pva_fc_params = [date_from, date_to]
    pva_act_conds  = ["s.sale_date BETWEEN ? AND ?"]
    pva_act_params = [date_from, date_to]

    if branch_filter != 'all':
        pva_fc_conds.append("b.branch_name = ?")
        pva_fc_params.append(branch_filter)
        pva_act_conds.append("b.branch_name = ?")
        pva_act_params.append(branch_filter)

    pva_fc_where  = "WHERE " + " AND ".join(pva_fc_conds)
    pva_act_where = "WHERE " + " AND ".join(pva_act_conds)

    # Fetch forecast rows within the selected month range
    try:
        cursor.execute(f"""
            SELECT f.forecast_date                          AS ds,
                   COALESCE(SUM(f.predicted_revenue), 0)   AS yhat,
                   COALESCE(SUM(f.lower_bound_revenue), 0) AS yhat_lower,
                   COALESCE(SUM(f.upper_bound_revenue), 0) AS yhat_upper
            FROM sales_forecast f
            JOIN branch b ON f.branch_id = b.branch_id
            {pva_fc_where}
            GROUP BY f.forecast_date
            ORDER BY f.forecast_date ASC
        """, pva_fc_params)
        pva_forecast_rows = {r['ds']: dict(r) for r in cursor.fetchall()}
    except Exception:
        pva_forecast_rows = {}

    # Fetch actual daily revenue within the selected month range
    try:
        cursor.execute(f"""
            SELECT s.sale_date                         AS ds,
                   ROUND(SUM(s.total_revenue), 2)      AS actual_revenue,
                   COUNT(*)                            AS txn_count
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            {pva_act_where}
            GROUP BY s.sale_date
            ORDER BY s.sale_date ASC
        """, pva_act_params)
        pva_actual_rows = {r['ds']: dict(r) for r in cursor.fetchall()}
    except Exception:
        pva_actual_rows = {}

    # Merge: all dates that appear in either forecast or actual within the period
    all_pva_dates = sorted(set(list(pva_forecast_rows.keys()) + list(pva_actual_rows.keys())))
    predicted_vs_actual = []
    for ds in all_pva_dates:
        fc_row  = pva_forecast_rows.get(ds, {})
        act_row = pva_actual_rows.get(ds, {})
        yhat       = fc_row.get('yhat', 0) or 0
        actual_rev = act_row.get('actual_revenue', None)  # None = no recorded sales
        txn_count  = act_row.get('txn_count', 0) or 0
        lower      = fc_row.get('yhat_lower', 0) or 0
        upper      = fc_row.get('yhat_upper', 0) or 0
        variance     = None
        variance_pct = None
        if actual_rev is not None and yhat and yhat > 0:
            variance     = round(actual_rev - yhat, 2)
            variance_pct = round((variance / yhat) * 100, 1)
        dt_obj = datetime.strptime(ds, '%Y-%m-%d')
        predicted_vs_actual.append({
            'ds':           ds,
            'day_name':     ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][dt_obj.weekday()],
            'yhat':         round(yhat, 2),
            'yhat_lower':   round(lower, 2),
            'yhat_upper':   round(upper, 2),
            'actual':       round(actual_rev, 2) if actual_rev is not None else None,
            'txn_count':    txn_count,
            'variance':     variance,
            'variance_pct': variance_pct,
            'is_holiday':   ds in holiday_set,
            'in_range':     (lower <= (actual_rev or 0) <= upper)
                            if actual_rev is not None and lower and upper else None,
        })

    # Summary stats for predicted vs actual section
    pva_with_actual     = [r for r in predicted_vs_actual if r['actual'] is not None]
    pva_total_predicted = round(sum(r['yhat']   for r in pva_with_actual), 2)
    pva_total_actual    = round(sum(r['actual'] for r in pva_with_actual), 2)
    pva_mape = 0.0
    if pva_with_actual:
        mape_vals = [abs(r['variance_pct']) for r in pva_with_actual if r['variance_pct'] is not None]
        pva_mape  = round(sum(mape_vals) / len(mape_vals), 1) if mape_vals else 0.0
    pva_within_range = sum(1 for r in pva_with_actual if r.get('in_range'))

    conn.close()

    # ── AI Executive Summary prompt ────────────────────────────
    top_prod_ctx    = ", ".join([
        f"{p['product_name']} ({p['qty']} units, {p['pct']}% share)"
        for p in top_products
    ]) or "N/A"
    bottom_prod_ctx = ", ".join([p['product_name'] for p in bottom_products]) or "N/A"
    cat_ctx         = ", ".join([
        f"{c['product_category']}: RM {c['revenue']:,.2f} ({c['pct']}%)"
        for c in category_breakdown
    ]) or "N/A"
    regional_ctx    = ", ".join([
        f"{r['branch_name']}: RM {r['rev']:,.2f}"
        for r in regional_breakdown
    ]) or "N/A"

    ai_prompt = (
        f"Write a concise executive summary (3 sentences max) for a Malaysian coffee shop owner. "
        f"No jargon, no markdown, no bullet points. Use plain business English.\n"
        f"Branch: {branch_filter if branch_filter != 'all' else 'all branches'}. "
        f"Period: {period_str}. "
        f"Revenue: RM {period_revenue:,.2f} across {period_txns:,} transactions. "
        f"Average order value: RM {aov:,.2f}. Daily average: RM {daily_avg:,.2f}. "
        f"Trend vs prior period: {trend_label}. "
        f"Busiest hour: {peak_hour}. Busiest day: {peak_day}. "
        f"Top products: {top_prod_ctx}. Slow movers: {bottom_prod_ctx}. "
        f"Category revenue: {cat_ctx}. Regional performance: {regional_ctx}.\n\n"
        f"Then give exactly 3 numbered, actionable steps the owner can take this month to improve revenue."
    )
    success_ai, insight = get_ai_insight(ai_prompt)

    return {
        "status":               "success",
        "period":               period_str,
        "branch":               branch_filter,
        "daily":                daily_data,
        "peak_hour":            peak_hour,
        "peak_day":             peak_day,
        "hourly_breakdown":     hourly_breakdown,
        "dow_breakdown":        dow_breakdown,
        "top_branch":           top_branch,
        "period_revenue":       round(period_revenue, 2),
        "period_txns":          period_txns,
        "daily_average":        daily_avg,
        "aov":                  aov,
        "trend_label":          trend_label,
        "top_products":         top_products,
        "bottom_products":      bottom_products,
        "category_breakdown":   category_breakdown,
        "payment_breakdown":    payment_breakdown,
        "regional_breakdown":   regional_breakdown,
        "branch_max_rev":       branch_max_rev,
        "monthly_trend":        monthly_trend,
        "forecast":             forecast_data,
        "predicted_vs_actual":  predicted_vs_actual,
        "pva_total_predicted":  pva_total_predicted,
        "pva_total_actual":     pva_total_actual,
        "pva_mape":             pva_mape,
        "pva_within_range":     pva_within_range,
        "pva_days_with_data":   len(pva_with_actual),
        "ai_insight":           insight if success_ai else "AI insight temporarily unavailable."
    }


@app.route('/api/report_data')
@app.route('/api/report-data')
@login_required
def api_report_data():
    branch_filter = request.args.get('branch',    'all')
    date_from     = request.args.get('date_from', None)
    date_to       = request.args.get('date_to',   None)

    if not date_from or not date_to:
        return jsonify({"status": "error", "message": "date_from and date_to are required."}), 400

    try:
        result = _build_report_data(branch_filter, date_from, date_to)
        return jsonify(result)
    except Exception as e:
        print("Report API Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — PDF EXPORT (Executive Report Only, Month-Based)
# ============================================================
@app.route('/api/export-pdf')
@login_required
def api_export_pdf():
    branch_filter = request.args.get('branch',    'all')
    date_from     = request.args.get('date_from', None)
    date_to       = request.args.get('date_to',   None)

    if not date_from or not date_to:
        return jsonify({"status": "error", "message": "date_from and date_to are required."}), 400

    try:
        report_data = _build_report_data(branch_filter, date_from, date_to)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Data error: {str(e)}"}), 500

    branch_label = branch_filter if branch_filter != 'all' else 'All Branches'
    now_str      = datetime.now().strftime('%d %b %Y, %I:%M %p')
    today        = datetime.now().strftime('%A, %d %B %Y')

    try:
        month_label = datetime.strptime(date_from, '%Y-%m-%d').strftime('%B %Y')
        month_key   = datetime.strptime(date_from, '%Y-%m-%d').strftime('%Y-%m')
    except Exception:
        month_label = date_from
        month_key   = date_from[:7]

    # ─────────────────────────────────────────────────────────
    #   SECTION 2 — Monthly Revenue Trend Table
    # ─────────────────────────────────────────────────────────
    mt = report_data.get('monthly_trend', {})
    mt_labels   = mt.get('labels',   [])
    mt_keys     = mt.get('keys',     [])
    mt_branches = mt.get('branches', [])
    mt_by_b     = mt.get('by_branch', {})

    slice_n    = min(6, len(mt_labels))
    start_idx  = len(mt_labels) - slice_n
    rec_labels = mt_labels[start_idx:]
    rec_keys   = mt_keys[start_idx:]

    monthly_table_html = ""
    if rec_labels:
        header_cols = "".join([f"<th style='text-align:right;'>{b}</th>" for b in mt_branches])
        monthly_table_html = f"""
        <table>
            <thead>
                <tr>
                    <th>Month</th>
                    {header_cols}
                    <th style='text-align:right;'>Total</th>
                </tr>
            </thead>
            <tbody>"""
        for i, lbl in enumerate(rec_labels):
            real_idx  = start_idx + i
            total_row = sum(mt_by_b.get(b, [0]*len(mt_labels))[real_idx] for b in mt_branches)
            is_curr   = (rec_keys[i] == month_key)
            row_style = "background:#EFF6FF;font-weight:700;" if is_curr else ("background:#F8FAFC;" if i%2 else "")
            branch_cells = "".join([
                f"<td style='text-align:right;font-family:monospace;padding:5px 8px;'>"
                f"RM {mt_by_b.get(b,[0]*len(mt_labels))[real_idx]:,.2f}</td>"
                for b in mt_branches
            ])
            curr_marker = " ◀" if is_curr else ""
            monthly_table_html += f"""
                <tr style='{row_style}'>
                    <td style='font-family:monospace;padding:5px 8px;'>{lbl}{curr_marker}</td>
                    {branch_cells}
                    <td style='text-align:right;font-family:monospace;font-weight:700;color:#3B82F6;padding:5px 8px;'>RM {total_row:,.2f}</td>
                </tr>"""
        monthly_table_html += "</tbody></table>"
        monthly_table_html += "<div style='font-size:9px;color:#94A3B8;margin-top:3px;'>◀ Highlighted = selected reporting month.</div>"

    # ─────────────────────────────────────────────────────────
    #   SECTION 3 — KPI Grid
    # ─────────────────────────────────────────────────────────
    aov_val      = report_data.get('aov', 0)
    daily_avg    = report_data.get('daily_average', 0)
    period_txns  = report_data.get('period_txns', 0)
    days_in_p    = max(len(report_data.get('daily', [])), 1)
    avg_daily_t  = round(period_txns / days_in_p, 1)

    # ─────────────────────────────────────────────────────────
    #   SECTION 4 — Predicted vs Actual (Prophet in-sample)
    # ─────────────────────────────────────────────────────────
    pva_rows      = report_data.get('predicted_vs_actual', [])
    pva_total_p   = report_data.get('pva_total_predicted', 0)
    pva_total_a   = report_data.get('pva_total_actual',    0)
    pva_mape      = report_data.get('pva_mape',            0)
    pva_in_range  = report_data.get('pva_within_range',    0)
    pva_days      = report_data.get('pva_days_with_data',  0)
    pva_max_val   = max(
        (max(r.get('yhat', 0) or 0, r.get('actual', 0) or 0) for r in pva_rows),
        default=1
    )

    pva_table_rows_html = ""
    for i, r in enumerate(pva_rows):
        has_actual = r['actual'] is not None
        var        = r.get('variance')
        var_pct    = r.get('variance_pct')
        in_range   = r.get('in_range')
        hbadge     = ' 🎉' if r.get('is_holiday') else ''
        row_bg     = '#F8FAFC' if i % 2 else 'white'

        # Variance colour
        if var is not None:
            var_color = '#059669' if var >= 0 else '#DC2626'
            var_str   = f"{'+'if var>=0 else ''}RM {abs(var):,.2f} ({'+'if var_pct>=0 else ''}{var_pct}%)"
        else:
            var_color = '#94A3B8'
            var_str   = '—'

        actual_str = f"RM {r['actual']:,.2f}" if has_actual else '<span style="color:#94A3B8;font-size:9px;">No data</span>'
        range_badge = ''
        if has_actual and in_range is not None:
            range_badge = (
                '<span style="background:#ECFDF5;color:#065F46;border-radius:3px;font-size:8px;padding:1px 4px;">✓ On Target</span>'
                if in_range else
                '<span style="background:#FEF2F2;color:#991B1B;border-radius:3px;font-size:8px;padding:1px 4px;">⚠ Off Range</span>'
            )

        pct_of_max = round(((r.get('yhat', 0) or 0) / pva_max_val) * 100, 1)
        act_pct    = round(((r.get('actual', 0) or 0) / pva_max_val) * 100, 1) if has_actual else 0

        pva_table_rows_html += f"""
        <tr style="background:{row_bg};">
            <td style="font-family:monospace;padding:5px 8px;font-size:9px;">{r['ds']}</td>
            <td style="padding:5px 8px;font-size:9px;">{r['day_name']}{hbadge}</td>
            <td style="padding:5px 8px;width:80px;">
                <div style="background:#E2E8F0;border-radius:2px;height:5px;margin-bottom:2px;overflow:hidden;">
                    <div style="width:{pct_of_max}%;height:100%;background:#93C5FD;border-radius:2px;"></div>
                </div>
                <div style="background:#E2E8F0;border-radius:2px;height:5px;overflow:hidden;">
                    <div style="width:{act_pct}%;height:100%;background:#10B981;border-radius:2px;"></div>
                </div>
            </td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;color:#3B82F6;font-size:9px;">RM {r.get('yhat',0):,.2f}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;font-size:9px;">{actual_str}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;color:{var_color};font-size:9px;">{var_str}</td>
            <td style="padding:5px 8px;font-size:8px;">{range_badge}</td>
        </tr>"""

    if pva_table_rows_html:
        pva_section_html = f"""
        <div style="font-size:9px;color:#64748B;margin-bottom:6px;line-height:1.6;">
            <span style="display:inline-block;width:12px;height:6px;background:#93C5FD;border-radius:2px;margin-right:3px;"></span>Predicted &nbsp;
            <span style="display:inline-block;width:12px;height:6px;background:#10B981;border-radius:2px;margin-right:3px;"></span>Actual
        </div>
        <table>
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Day</th>
                    <th style="width:80px;">Volume</th>
                    <th style="text-align:right;">Predicted (RM)</th>
                    <th style="text-align:right;">Actual (RM)</th>
                    <th style="text-align:right;">Variance</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>{pva_table_rows_html}</tbody>
        </table>
        <div class="three-col" style="margin-top:8px;">
            <div class="insight-box">
                <strong>📊 Model Accuracy (MAPE):</strong><br>
                {pva_mape}% avg error across {pva_days} days
            </div>
            <div class="insight-box" style="background:#ECFDF5;border-color:#6EE7B7;color:#065F46;">
                <strong>🎯 Within Forecast Range:</strong><br>
                {pva_in_range} of {pva_days} days on target
            </div>
            <div class="insight-box" style="background:#FFFBEB;border-color:#FDE68A;color:#92400E;">
                <strong>📈 Predicted vs Actual:</strong><br>
                RM {pva_total_p:,.2f} vs RM {pva_total_a:,.2f}
            </div>
        </div>"""
    else:
        pva_section_html = """
        <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:6px;
             padding:14px;text-align:center;color:#94A3B8;font-size:10px;">
            No forecast data found for this period. Run the AI Forecast engine first,
            then select a past month to compare predictions against actual sales.
        </div>"""

    # ─────────────────────────────────────────────────────────
    #   SECTION 5 — Product / Regional / Category / Payment
    # ─────────────────────────────────────────────────────────
    top_prod_rows = ""
    for i, p in enumerate(report_data.get('top_products', [])):
        top_prod_rows += f"""
        <tr style="background:{'#F8FAFC' if i%2 else 'white'};">
            <td style="padding:5px 8px;">
                <span style="display:inline-block;width:18px;height:18px;border-radius:50%;
                      background:#3B82F6;color:white;font-size:9px;font-weight:700;
                      text-align:center;line-height:18px;">{i+1}</span> {p['product_name']}
            </td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">{int(p['qty']):,}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">RM {p['revenue']:,.2f}</td>
            <td style="text-align:right;padding:5px 8px;">
                <div style="background:#E2E8F0;border-radius:3px;height:5px;overflow:hidden;">
                    <div style="width:{p['pct']}%;height:100%;background:linear-gradient(90deg,#3B82F6,#60A5FA);border-radius:3px;"></div>
                </div>
            </td>
            <td style="text-align:right;padding:5px 8px;color:#64748B;">{p['pct']}%</td>
        </tr>"""

    bot_prod_rows = ""
    for i, p in enumerate(report_data.get('bottom_products', [])):
        bot_prod_rows += f"""
        <tr style="background:{'#F8FAFC' if i%2 else 'white'};">
            <td style="padding:5px 8px;color:#EF4444;">{p['product_name']}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">{int(p['qty']):,}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">RM {p['revenue']:,.2f}</td>
        </tr>"""

    regional_rows = ""
    br_max = report_data.get('branch_max_rev', 1)
    br_colors = ['#3B82F6', '#F59E0B', '#10B981', '#8B5CF6']
    for i, r in enumerate(report_data.get('regional_breakdown', [])):
        pct = round((r['rev'] / br_max) * 100, 1)
        is_top = r['branch_name'] == report_data.get('top_branch', '')
        regional_rows += f"""
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:9px;font-size:10px;">
            <div style="width:100px;flex-shrink:0;font-weight:{'700' if is_top else '400'};color:#334155;">
                {'⭐ ' if is_top else ''}{r['branch_name']}
            </div>
            <div style="flex:1;background:#E2E8F0;border-radius:4px;height:10px;overflow:hidden;">
                <div style="width:{pct}%;height:100%;background:{br_colors[i%4]};border-radius:4px;"></div>
            </div>
            <div style="width:75px;text-align:right;font-family:monospace;font-weight:600;color:#1E293B;">RM {r['rev']:,.2f}</div>
        </div>"""

    cat_total = sum(c['revenue'] for c in report_data.get('category_breakdown', [])) or 1
    cat_rows  = ""
    for i, c in enumerate(report_data.get('category_breakdown', [])):
        bw = round((c['revenue'] / cat_total) * 100, 1)
        cat_rows += f"""
        <tr style="background:{'#F8FAFC' if i%2 else 'white'};">
            <td style="padding:5px 8px;">{c['product_category']}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">RM {c['revenue']:,.2f}</td>
            <td style="text-align:right;padding:5px 8px;">{c['pct']}%</td>
            <td style="padding:5px 8px;width:70px;">
                <div style="background:#E2E8F0;border-radius:3px;height:6px;overflow:hidden;">
                    <div style="width:{bw}%;height:100%;background:linear-gradient(90deg,#10B981,#34D399);border-radius:3px;"></div>
                </div>
            </td>
        </tr>"""

    pay_total  = sum(p['txn_count'] for p in report_data.get('payment_breakdown', [])) or 1
    pay_colors = ['#3B82F6', '#F59E0B', '#10B981', '#8B5CF6']
    pay_rows   = ""
    for i, p in enumerate(report_data.get('payment_breakdown', [])):
        bw = round((p['txn_count'] / pay_total) * 100, 1)
        pay_rows += f"""
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:10px;">
            <div style="width:100px;flex-shrink:0;color:#334155;">{p['payment_method']}</div>
            <div style="flex:1;background:#E2E8F0;border-radius:4px;height:8px;overflow:hidden;">
                <div style="width:{bw}%;height:100%;background:{pay_colors[i%4]};border-radius:4px;"></div>
            </div>
            <div style="width:40px;text-align:right;font-family:monospace;font-weight:600;color:#1E293B;">{p['pct']}%</div>
        </div>"""

    # ─────────────────────────────────────────────────────────
    #   SECTION 6 — Pipeline: Hourly + Day-of-Week
    # ─────────────────────────────────────────────────────────
    hourly_data   = report_data.get('hourly_breakdown', [])
    hourly_max    = max((h['txns'] for h in hourly_data), default=1)
    peak_hour_val = report_data.get('peak_hour', 'N/A')

    hourly_rows = ""
    for h in hourly_data:
        bw     = round((h['txns'] / hourly_max) * 100, 1)
        isPeak = peak_hour_val.startswith(h['hour'][:2])
        hourly_rows += f"""
        <tr style="background:{'#EFF6FF' if isPeak else 'white'};">
            <td style="font-family:monospace;padding:4px 8px;font-size:9px;">{'⭐ ' if isPeak else ''}{h['hour']}</td>
            <td style="padding:4px 8px;">
                <div style="background:#E2E8F0;border-radius:3px;height:5px;overflow:hidden;">
                    <div style="width:{bw}%;height:100%;background:{'#1D4ED8' if isPeak else '#93C5FD'};border-radius:3px;"></div>
                </div>
            </td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;font-size:9px;">{h['txns']:,}</td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;font-size:9px;color:#64748B;">RM {h['revenue']:,.2f}</td>
        </tr>"""

    dow_data    = report_data.get('dow_breakdown', [])
    dow_max     = max((d['txns'] for d in dow_data), default=1)
    peak_day_v  = report_data.get('peak_day', 'N/A')
    dow_rows    = ""
    for d in dow_data:
        bw     = round((d['txns'] / dow_max) * 100, 1)
        isPeak = (d['day'] == peak_day_v)
        dow_rows += f"""
        <tr style="background:{'#ECFDF5' if isPeak else 'white'};">
            <td style="padding:4px 8px;font-size:9px;">{'⭐ ' if isPeak else ''}{d['day']}</td>
            <td style="padding:4px 8px;">
                <div style="background:#E2E8F0;border-radius:3px;height:5px;overflow:hidden;">
                    <div style="width:{bw}%;height:100%;background:{'#059669' if isPeak else '#6EE7B7'};border-radius:3px;"></div>
                </div>
            </td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;font-size:9px;">{d['txns']:,}</td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;font-size:9px;color:#64748B;">RM {d['revenue']:,.2f}</td>
        </tr>"""

    # ─────────────────────────────────────────────────────────
    #   SECTION 7 — AI Recommendations
    # ─────────────────────────────────────────────────────────
    ai_html = report_data.get('ai_insight', 'N/A').replace('\n', '<br>').replace('**', '')
    top_prods_list = report_data.get('top_products', [])

    # ─────────────────────────────────────────────────────────
    #   RENDER FULL PDF HTML
    # ─────────────────────────────────────────────────────────
    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>MCS Executive Report — {branch_label} — {month_label}</title>
<style>
  @page {{ size: A4 portrait; margin: 15mm 12mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; font-size: 10.5px; color: #1E293B; width: 100%; }}

  /* ── Page break control ───────────────────────────────── */
  .page-break {{ page-break-before: always; break-before: page; }}
  .no-break   {{ page-break-inside: avoid; break-inside: avoid; }}
  .keep-with-next {{ page-break-after: avoid; break-after: avoid; }}

  .header {{ background: linear-gradient(135deg,#1E2A3A,#2A3B52); padding: 28px 36px 22px; color: white; position:relative;overflow:hidden; }}
  .header::after {{ content:'';position:absolute;right:-40px;top:-40px;width:180px;height:180px;border-radius:50%;background:rgba(59,130,246,.12); }}
  .brand {{ display:flex;align-items:center;gap:10px;margin-bottom:14px; }}
  .brand-icon {{ width:36px;height:36px;background:linear-gradient(135deg,#F59E0B,#D97706);border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:18px; }}
  .accent {{ height:4px;background:linear-gradient(90deg,#F59E0B,#3B82F6,#10B981); }}
  .body {{ padding:24px 36px; }}

  .section-title {{
    font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;
    border-left:3px solid #3B82F6;padding-left:7px;margin:18px 0 10px;color:#1E2A3A;
  }}

  .kpi-5col {{ display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:4px; }}
  .kpi-4col {{ display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:4px; }}
  .kpi-box {{ border:1px solid #E2E8F0;border-radius:7px;padding:10px 12px;background:#F8FAFC;position:relative;overflow:hidden; }}
  .kpi-box::before {{ content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:7px 7px 0 0; }}
  .kpi-box.k-blue::before   {{ background:#3B82F6; }}
  .kpi-box.k-amber::before  {{ background:#F59E0B; }}
  .kpi-box.k-teal::before   {{ background:#10B981; }}
  .kpi-box.k-cyan::before   {{ background:#06B6D4; }}
  .kpi-box.k-purple::before {{ background:#8B5CF6; }}
  .kpi-label {{ font-size:8px;color:#64748B;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px; }}
  .kpi-value {{ font-size:14px;font-weight:700;color:#1E293B;font-family:monospace; }}
  .kpi-sub   {{ font-size:8px;color:#94A3B8;margin-top:2px; }}

  table {{ width:100%;border-collapse:collapse;font-size:10px; }}
  thead th {{ background:#1E2A3A;color:white;padding:6px 8px;text-align:left;font-size:9px;letter-spacing:.4px; }}
  tbody td {{ padding:5px 8px;border-bottom:1px solid #E2E8F0;color:#334155; }}

  .two-col   {{ display:grid;grid-template-columns:1fr 1fr;gap:14px; }}
  .three-col {{ display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px; }}

  .insight-box {{
    background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;
    padding:9px 11px;font-size:10px;color:#1E40AF;line-height:1.6;margin-bottom:6px;
  }}
  .insight-box.teal  {{ background:#ECFDF5;border-color:#6EE7B7;color:#065F46; }}
  .insight-box.amber {{ background:#FFFBEB;border-color:#FDE68A;color:#92400E; }}
  .insight-box.red   {{ background:#FEF2F2;border-color:#FECACA;color:#991B1B; }}
  .insight-box.slate {{ background:#F8FAFC;border-color:#CBD5E1;color:#475569; }}

  .ai-box {{ background:linear-gradient(135deg,#F8FAFC,#EFF6FF);border:1px solid #BFDBFE;border-radius:7px;padding:12px 14px; }}

  .footer {{ padding:10px 36px;background:#F8FAFC;border-top:1px solid #E2E8F0;display:flex;justify-content:space-between;font-size:8px;color:#94A3B8; }}
  .data-note {{ background:#FFFBEB;border:1px solid #FDE68A;border-radius:5px;padding:7px 10px;font-size:9px;color:#92400E;margin-top:6px; }}

  /* ── Print media overrides ────────────────────────────── */
  @media print {{
    @page {{ size: A4 portrait; margin: 12mm 10mm; }}
    body {{ width: 100%; font-size: 10px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .header {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .page-break {{ page-break-before: always; break-before: page; }}
    .no-break   {{ page-break-inside: avoid; break-inside: avoid; }}
    .section-title {{ page-break-after: avoid; break-after: avoid; }}
    table {{ page-break-inside: auto; }}
    tr {{ page-break-inside: avoid; break-inside: avoid; }}
    .footer {{ position: running(footer); }}
  }}
</style>
</head>
<body>

<!-- ═══════════════════════════════════════════════════════
     PAGE 1
═══════════════════════════════════════════════════════ -->

<!-- HEADER -->
<div class="header no-break">
  <div class="brand">
    <div class="brand-icon">☕</div>
    <div>
      <div style="font-size:14px;font-weight:700;">Mini Coffee Shop</div>
      <div style="font-size:9px;opacity:.6;">AI-Powered Analytics System</div>
    </div>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:flex-end;position:relative;z-index:1;">
    <div>
      <div style="font-size:20px;font-weight:700;">Executive Sales Report</div>
      <div style="font-size:10px;opacity:.7;margin-top:3px;">{branch_label} · {month_label} · {today}</div>
    </div>
    <div style="text-align:right;font-size:9px;opacity:.7;line-height:1.7;">
      Generated: {now_str}<br>
      Reporting Period: {date_from} – {date_to}<br>
      Confidential — Internal Use Only
    </div>
  </div>
</div>
<div class="accent"></div>

<div class="body">

<!-- ══ SECTION 1: EXECUTIVE SUMMARY ══════════════════════════ -->
<div class="section-title keep-with-next">1. Executive Summary</div>
<div class="kpi-5col no-break">
  <div class="kpi-box k-blue">
    <div class="kpi-label">Total Revenue</div>
    <div class="kpi-value">RM {report_data['period_revenue']:,.2f}</div>
    <div class="kpi-sub">{report_data['trend_label']}</div>
  </div>
  <div class="kpi-box k-amber">
    <div class="kpi-label">Sales Volume</div>
    <div class="kpi-value">{report_data['period_txns']:,}</div>
    <div class="kpi-sub">Orders placed</div>
  </div>
  <div class="kpi-box k-teal">
    <div class="kpi-label">Daily Average</div>
    <div class="kpi-value">RM {daily_avg:,.2f}</div>
    <div class="kpi-sub">Revenue per day</div>
  </div>
  <div class="kpi-box k-cyan">
    <div class="kpi-label">Avg Order Value</div>
    <div class="kpi-value">RM {aov_val:,.2f}</div>
    <div class="kpi-sub">Per transaction</div>
  </div>
  <div class="kpi-box k-purple">
    <div class="kpi-label">Top Branch</div>
    <div class="kpi-value" style="font-size:11px;margin-top:3px;">{report_data['top_branch']}</div>
    <div class="kpi-sub">Highest revenue</div>
  </div>
</div>
<div class="data-note no-break">
  ⚠ Profit &amp; COGS data is not available from POS transaction logs. Revenue figures represent gross sales only.
</div>

<!-- ══ SECTION 2: REVENUE & SALES VOLUME TREND ═══════════════ -->
<div class="section-title keep-with-next">2. Revenue &amp; Sales Volume — Last {slice_n} Months</div>
<div class="no-break">
{monthly_table_html if monthly_table_html else '<div style="color:#94A3B8;font-size:9px;padding:8px 0;">Insufficient monthly history.</div>'}
</div>

<!-- ══ SECTION 3: KEY PERFORMANCE INDICATORS ═════════════════ -->
<div class="section-title keep-with-next">3. Key Performance Indicators</div>
<div class="kpi-4col no-break">
  <div class="kpi-box k-blue">
    <div class="kpi-label">Avg Deal Size (AOV)</div>
    <div class="kpi-value">RM {aov_val:,.2f}</div>
    <div class="kpi-sub">Revenue ÷ Transactions</div>
  </div>
  <div class="kpi-box k-teal">
    <div class="kpi-label">Avg Daily Orders</div>
    <div class="kpi-value">{avg_daily_t}</div>
    <div class="kpi-sub">Transactions per day</div>
  </div>
  <div class="kpi-box k-amber">
    <div class="kpi-label">Peak Hour</div>
    <div class="kpi-value" style="font-size:11px;">{report_data['peak_hour']}</div>
    <div class="kpi-sub">Highest transaction volume</div>
  </div>
  <div class="kpi-box k-purple">
    <div class="kpi-label">Peak Day</div>
    <div class="kpi-value" style="font-size:11px;">{report_data['peak_day']}</div>
    <div class="kpi-sub">Most active weekday</div>
  </div>
</div>
<div class="insight-box slate no-break" style="margin-top:6px;">
  <strong>Period-over-Period:</strong> {report_data['trend_label']} &nbsp;|&nbsp;
  <strong>Active Days:</strong> {days_in_p} &nbsp;|&nbsp;
  <strong>Avg Revenue/Day:</strong> RM {daily_avg:,.2f}
</div>

</div><!-- /.body page 1 -->

<!-- ═══════════════════════════════════════════════════════
     PAGE 2 — PREDICTED VS ACTUAL + SEGMENT BREAKDOWN
═══════════════════════════════════════════════════════ -->
<div class="page-break"></div>

<div class="body" style="padding-top:20px;">

<!-- ══ SECTION 4: PREDICTED VS ACTUAL (PROPHET IN-SAMPLE) ════ -->
<div class="section-title keep-with-next">4. Daily Sales Trend ({month_label})</div>
<div class="no-break">
{pva_section_html}
</div>

</div><!-- /.body page 2 -->

<!-- ═══════════════════════════════════════════════════════
     PAGE 3 — SEGMENTS, PIPELINE & AI
═══════════════════════════════════════════════════════ -->
<div class="page-break"></div>
<div class="body">

<!-- ══ SECTION 5: SEGMENT BREAKDOWN ══════════════════════════ -->
<div class="section-title keep-with-next">5. Segment Breakdown — Product, Region &amp; Category</div>

<div class="two-col no-break" style="margin-bottom:12px;">
  <div>
    <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">🏆 Top-Selling Products</div>
    {'<table><thead><tr><th>Product</th><th style="text-align:right;">Units</th><th style="text-align:right;">Revenue</th><th style="width:60px;">Bar</th><th style="text-align:right;">Share</th></tr></thead><tbody>' + top_prod_rows + '</tbody></table>' if top_prod_rows else '<div style="color:#94A3B8;font-size:9px;">No product data</div>'}
  </div>
  <div>
    <div style="font-size:9px;font-weight:700;color:#EF4444;margin-bottom:6px;">⚠ Underperforming Products (Bottom 3)</div>
    {'<table><thead><tr><th>Product</th><th style="text-align:right;">Units</th><th style="text-align:right;">Revenue</th></tr></thead><tbody>' + bot_prod_rows + '</tbody></table>' if bot_prod_rows else '<div style="color:#94A3B8;font-size:9px;">No data</div>'}
    <div class="insight-box red" style="margin-top:8px;font-size:9px;">
      <strong>Action:</strong> Bundle underperformers with top sellers or run targeted promos.
    </div>
  </div>
</div>

<div class="two-col no-break">
  <div>
    <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:8px;">📍 Regional Performance (By Branch)</div>
    {regional_rows if regional_rows else '<div style="color:#94A3B8;font-size:9px;">No branch data</div>'}
    <div style="margin-top:10px;font-size:9px;font-weight:700;color:#1E293B;margin-bottom:8px;">💳 Payment Method Breakdown</div>
    {pay_rows if pay_rows else '<div style="color:#94A3B8;font-size:9px;">No payment data</div>'}
  </div>
  <div>
    <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">🗂 Revenue by Product Category</div>
    {'<table><thead><tr><th>Category</th><th style="text-align:right;">Revenue</th><th style="text-align:right;">Share</th><th style="width:60px;">Bar</th></tr></thead><tbody>' + cat_rows + '</tbody></table>' if cat_rows else '<div style="color:#94A3B8;font-size:9px;">No category data</div>'}
  </div>
</div>

<!-- ══ SECTION 6: SALES PIPELINE HEALTH ══════════════════════ -->
<div class="section-title keep-with-next">6. Sales Pipeline Health — Transaction Flow by Time</div>
<div class="two-col no-break">
  <div>
    <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">⏰ Hourly Transaction Volume</div>
    {'<table><thead><tr><th>Hour</th><th>Activity</th><th style="text-align:right;">Orders</th><th style="text-align:right;">Revenue</th></tr></thead><tbody>' + hourly_rows + '</tbody></table>' if hourly_rows else '<div style="color:#94A3B8;font-size:9px;padding:8px 0;">No hourly data</div>'}
  </div>
  <div>
    <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">📅 Day-of-Week Revenue Pattern</div>
    {'<table><thead><tr><th>Day</th><th>Activity</th><th style="text-align:right;">Orders</th><th style="text-align:right;">Revenue</th></tr></thead><tbody>' + dow_rows + '</tbody></table>' if dow_rows else '<div style="color:#94A3B8;font-size:9px;padding:8px 0;">No data</div>'}
    <div class="insight-box teal" style="margin-top:8px;font-size:9px;">
      ⭐ <strong>Peak Hour:</strong> {report_data['peak_hour']}
      &nbsp;|&nbsp;
      ⭐ <strong>Peak Day:</strong> {report_data['peak_day']}<br>
      Maximise staff coverage at these windows to capture full revenue potential.
    </div>
  </div>
</div>

<!-- ══ SECTION 7: ACTIONABLE RECOMMENDATIONS ════════════════ -->
<div class="section-title keep-with-next" style="margin-top:20px;">7. AI-Powered Actionable Recommendations</div>
<div class="ai-box no-break">
  <div style="display:flex;align-items:center;gap:7px;margin-bottom:7px;font-weight:700;font-size:11px;">
    <span style="background:linear-gradient(135deg,#3B82F6,#8B5CF6);color:white;padding:2px 7px;border-radius:3px;font-size:8px;">GEMINI AI</span>
    Strategic Recommendations for {month_label}
  </div>
  <div style="font-size:10px;color:#334155;line-height:1.7;">{ai_html}</div>
</div>

<div class="two-col no-break" style="margin-top:12px;">
  <div>
    <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">📌 Key Findings</div>
    <div style="font-size:10px;line-height:1.9;color:#334155;">
      ● Revenue trend: <strong>{report_data['trend_label']}</strong><br>
      ● Top branch: <strong>{report_data['top_branch']}</strong><br>
      ● Avg order value: <strong>RM {aov_val:,.2f}</strong><br>
      ● Busiest window: <strong>{report_data['peak_hour']} · {report_data['peak_day']}</strong><br>
      ● Best product: <strong>{top_prods_list[0]['product_name'] if top_prods_list else 'N/A'}</strong> ({top_prods_list[0]['qty'] if top_prods_list else 0} units)
    </div>
  </div>
  <div>
    <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">✅ Recommended Actions</div>
    <div style="font-size:10px;line-height:1.9;color:#334155;">
      ✓ Deploy extra baristas during <strong>{report_data['peak_hour']}</strong><br>
      ✓ Pre-stock inventory before <strong>{report_data['peak_day']}</strong> each week<br>
      ✓ Bundle slow movers with <strong>{top_prods_list[0]['product_name'] if top_prods_list else 'top sellers'}</strong><br>
      ✓ Run 2PM–4PM afternoon deals to lift off-peak volume<br>
      ✓ Incentivise cashless payments to cut cash-handling overhead
    </div>
  </div>
</div>

</div><!-- /.body page 3 -->

<div class="footer">
  <div>Mini Coffee Shop · MCS Analytics v1.0 · {month_label}</div>
  <div>Generated {now_str} · Confidential — Internal Use Only</div>
  <div>Page 3 of 3</div>
</div>
</body>
</html>"""

    try:
        from weasyprint import HTML as WP_HTML
        pdf_bytes = WP_HTML(string=html_doc).write_pdf()
        response  = make_response(pdf_bytes)
        response.headers['Content-Type']        = 'application/pdf'
        filename_str = f"MCS_Executive_Report_{branch_filter.replace(' ','_')}_{month_key}.pdf"
        response.headers['Content-Disposition'] = f'attachment; filename="{filename_str}"'
        return response

    except ImportError:
        response = make_response(html_doc)
        response.headers['Content-Type']        = 'text/html; charset=utf-8'
        filename_str = f"MCS_Executive_Report_{branch_filter.replace(' ','_')}_{month_key}.html"
        response.headers['Content-Disposition'] = f'attachment; filename="{filename_str}"'
        return response

    except Exception as e:
        return jsonify({"status": "error", "message": f"PDF render error: {str(e)}"}), 500


# ============================================================
#    RUNTIME ENGINE EXECUTION ENTRYPOINT
# ============================================================
if __name__ == '__main__':
    app.run(debug=True)