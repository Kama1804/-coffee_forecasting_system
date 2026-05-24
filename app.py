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
#   CONFIGURATION
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
#   DATABASE INIT
# ============================================================
DB_PATH = os.path.join('database', 'coffee_shop.db')
if not os.path.exists(DB_PATH):
    print("Database not found. Running initialization script...")
    initialize_database()
else:
    print("Database found. System ready.")

# ============================================================
#   HELPERS
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
#   AUTH ROUTES
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
#   PAGE ROUTES
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
#   API — DASHBOARD FILTERS
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
#   API — KPI DATA
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
            "status":             "success",
            "total_revenue":      round(metrics['rev'], 2),
            "volume_sold":        metrics['vol'],
            "total_transactions": metrics['txns'],
            "daily_average":      daily_avg,
            "top_branch":         top_branch,
            "trend_label":        trend_label,
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
#   API — CHARTS DATA
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

        monthly_by_branch = {b: [] for b in all_branches}
        for r in trend_rows:
            for b in all_branches:
                monthly_by_branch[b].append(0)

        cursor.execute(f"""
            SELECT {date_col} as period, b.branch_name, SUM(s.total_revenue) as rev
            FROM sales_transaction s {join} {where}
            GROUP BY period, b.branch_name ORDER BY period ASC
        """, params)
        branch_monthly = cursor.fetchall()
        period_index   = {p: i for i, p in enumerate([r['period'] for r in trend_rows])}

        for r in branch_monthly:
            if r['period'] in period_index and r['branch_name'] in monthly_by_branch:
                monthly_by_branch[r['branch_name']][period_index[r['period']]] = round(r['rev'], 2)

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
                "labels":    trend_labels,
                "raw":       [r['period'] for r in trend_rows],
                "data":      [round(r['rev'], 2) for r in trend_rows],
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
#   API — AI CHATBOT WITH IN-MEMORY CONTEXT TTL CACHING
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
               COUNT(*)             as txns,
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

    cursor.execute("""
        SELECT b.branch_name,
               strftime('%Y-%m', s.sale_date) as month,
               SUM(s.total_revenue) as rev,
               COUNT(*) as txns
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        WHERE s.sale_date >= date(?, '-6 months')
        GROUP BY b.branch_name, month
        ORDER BY b.branch_name, month ASC
    """, [max_date])
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

    compiled_payload = {
        'date_range':             date_range,
        'total_rev':              total_rev,
        'total_txns':             total_txns,
        'daily_avg':              daily_avg,
        'peak_hour':              peak_hour,
        'top_branch':             top_branch,
        'branch_summary':         branch_summary,
        'last_mo_label':          last_mo_label,
        'curr_mo_label':          curr_mo_label,
        'last_mo_branch_summary': last_mo_branch_summary,
        'curr_mo_branch_summary': curr_mo_branch_summary,
        'monthly_trend_summary':  monthly_trend_summary,
        'top_products':           top_products,
        'categories':             categories,
        'weather_summary':        weather_summary,
        'forecast_summary':       forecast_summary,
        'arr_products':           top_products_list,
        'arr_product_revs':       top_products_rev,
        'arr_branches':           branch_names_list,
        'arr_branch_revs':        branch_rev_list
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
        system_context = f"{base_system_context}\n{chart_rules_extension}"

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
#   API — FORECAST ENGINE INTERFACE
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
                "status":             "success",
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
#   API — REPORT DATA AND PDF BUILDER SERVICE ENGINE
# ============================================================
def _build_report_data(branch_filter, rpt_type, date_from, date_to):
    """Core report data builder — shared by both the preview and PDF endpoints."""
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
 
    # ── Date range label ──────────────────────────────────────
    if date_from and date_to:
        period_str = f"{date_from} to {date_to}"
    elif rpt_type == 'weekly':
        period_str = "Last 7 Days"
    else:
        period_str = "Last 30 Days"
 
    # ── Daily data ────────────────────────────────────────────
    # FIX 1: when a date range is given, always fetch ALL days in that range
    # (no LIMIT). When no range is given, fall back to last N days.
    if date_from and date_to:
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
    else:
        limit_days = 7 if rpt_type == 'weekly' else 30
        # Fetch from the most recent date backwards so LIMIT gives latest days
        cursor.execute(f"""
            SELECT s.sale_date as date,
                   ROUND(SUM(s.total_revenue), 2) as revenue,
                   COUNT(*) as txns
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            {where_clause}
            GROUP BY s.sale_date
            ORDER BY s.sale_date DESC
            LIMIT ?
        """, base_params() + [limit_days])
 
    raw_daily = cursor.fetchall()
    # Always present chronologically
    daily_data = list(reversed([dict(r) for r in raw_daily])) if not (date_from and date_to) \
                 else [dict(r) for r in raw_daily]
 
    period_revenue = sum(r['revenue'] for r in daily_data)
    period_txns    = sum(r['txns']    for r in daily_data)
 
    # FIX 2: daily_average uses the actual calendar span (not just days with transactions)
    # so the number makes sense to a business owner
    if date_from and date_to:
        try:
            fd_dt = datetime.strptime(date_from, '%Y-%m-%d')
            td_dt = datetime.strptime(date_to,   '%Y-%m-%d')
            calendar_days = max((td_dt - fd_dt).days + 1, 1)
        except Exception:
            calendar_days = len(daily_data) or 1
    else:
        calendar_days = 7 if rpt_type == 'weekly' else 30
 
    daily_avg = round(period_revenue / calendar_days, 2)
    aov       = round(period_revenue / period_txns, 2) if period_txns > 0 else 0.0
 
    # ── Period-over-period trend ──────────────────────────────
    # FIX 3: compare the same number of calendar days, not transaction-days
    if daily_data:
        first_date = daily_data[0]['date']
        last_date  = daily_data[-1]['date']
        try:
            fd  = datetime.strptime(first_date, '%Y-%m-%d')
            ld  = datetime.strptime(last_date,  '%Y-%m-%d')
            # Use calendar span for the prior period
            span_days = (ld - fd).days + 1
            prev_from = (fd - timedelta(days=span_days)).strftime('%Y-%m-%d')
            prev_to   = (fd - timedelta(days=1)).strftime('%Y-%m-%d')
 
            prev_cond = ("b.branch_name = ? AND " if branch_filter != 'all' else "") + \
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
                pct = ((period_revenue - prev_rev) / prev_rev) * 100
                sign = "+" if pct >= 0 else ""
                trend_label = f"{sign}{pct:.1f}% vs prior period"
            else:
                trend_label = "No prior period data"
        except Exception:
            trend_label = "Trend N/A"
    else:
        trend_label = "No data"
 
    # ── Peak hour ─────────────────────────────────────────────
    cursor.execute(f"""
        SELECT CAST(substr(s.transaction_time, 1, 2) AS INTEGER) as hr, COUNT(*) as cnt
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY hr ORDER BY cnt DESC LIMIT 1
    """, base_params())
    ph        = cursor.fetchone()
    peak_hour = f"{int(ph['hr']):02d}:00 – {int(ph['hr'])+1:02d}:00" if ph and ph['hr'] is not None else "N/A"
 
    # ── Hourly breakdown (all hours) ──────────────────────────
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
 
    # ── Peak day ──────────────────────────────────────────────
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
 
    # ── Day-of-week breakdown ─────────────────────────────────
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
 
    # ── Top branch ────────────────────────────────────────────
    cursor.execute(f"""
        SELECT b.branch_name, SUM(s.total_revenue) as rev
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY b.branch_name ORDER BY rev DESC LIMIT 1
    """, base_params())
    tb_row     = cursor.fetchone()
    top_branch = tb_row['branch_name'] if tb_row else 'N/A'
 
    # ── Weather breakdown ─────────────────────────────────────
    cursor.execute(f"""
        SELECT s.weather_condition,
               ROUND(SUM(s.total_revenue) / COUNT(DISTINCT s.sale_date), 2) as avg_rev
        FROM sales_transaction s
        JOIN branch b ON s.branch_id = b.branch_id
        {where_clause}
        GROUP BY s.weather_condition
    """, base_params())
    weather_data = {r['weather_condition']: r['avg_rev'] for r in cursor.fetchall()}
 
    # ── Product performance ───────────────────────────────────
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
    """, base_params() + base_params())   # params duplicated for subquery
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
 
    # ── Category revenue breakdown ────────────────────────────
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
 
    # ── Payment method breakdown ──────────────────────────────
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
 
    # ── Forecast data (not filtered by date — always next 7 days) ─
    fc_conds  = ["b.branch_name = ?"] if branch_filter != 'all' else []
    fc_where  = ("WHERE " + " AND ".join(fc_conds)) if fc_conds else ""
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
    except Exception as e:
        print(f"[Forecast fallback] {e}")
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
 
    # ── Holiday tagging ───────────────────────────────────────
    holiday_set = {h[0] for h in MY_PUBLIC_HOLIDAYS}
    for row in forecast_data:
        dt_obj = datetime.strptime(row['ds'], '%Y-%m-%d')
        row['is_holiday'] = row['ds'] in holiday_set
        row['is_friday']  = (dt_obj.weekday() == 4)
 
    conn.close()
 
    # ── AI insight prompt (simple, no MAPE/RMSE) ─────────────
    top_prod_ctx    = ", ".join([
        f"{p['product_name']} ({p['qty']} units, {p['pct']}% of sales)"
        for p in top_products
    ]) or "N/A"
    bottom_prod_ctx = ", ".join([p['product_name'] for p in bottom_products]) or "N/A"
    cat_ctx         = ", ".join([
        f"{c['product_category']}: RM {c['revenue']:,.2f} ({c['pct']}%)"
        for c in category_breakdown
    ]) or "N/A"
    pay_ctx         = ", ".join([
        f"{p['payment_method']}: {p['pct']}%"
        for p in payment_breakdown
    ]) or "N/A"
 
    ai_prompt = (
        f"Write a brief, plain-English executive summary (3 sentences max) for a Malaysian "
        f"coffee shop owner. No jargon, no markdown, no bullet points.\n"
        f"Branch: {branch_filter if branch_filter != 'all' else 'all branches'}. "
        f"Period: {period_str}. "
        f"Revenue: RM {period_revenue:,.2f} across {period_txns:,} transactions. "
        f"Average order: RM {aov:,.2f}. Daily average: RM {daily_avg:,.2f}. "
        f"Trend vs prior period: {trend_label}. "
        f"Busiest hour: {peak_hour}. Busiest day: {peak_day}. "
        f"Top products: {top_prod_ctx}. Slow products: {bottom_prod_ctx}. "
        f"Then give 3 short, specific action items the owner can act on this week."
    )
    success_ai, insight = get_ai_insight(ai_prompt)
 
    return {
        "status":             "success",
        "period":             period_str,
        "branch":             branch_filter,
        "daily":              daily_data,
        "weather":            weather_data,
        "peak_hour":          peak_hour,
        "peak_day":           peak_day,
        "hourly_breakdown":   hourly_breakdown,
        "dow_breakdown":      dow_breakdown,
        "top_branch":         top_branch,
        "period_revenue":     round(period_revenue, 2),
        "period_txns":        period_txns,
        "daily_average":      daily_avg,
        "aov":                aov,
        "trend_label":        trend_label,
        "top_products":       top_products,
        "bottom_products":    bottom_products,
        "category_breakdown": category_breakdown,
        "payment_breakdown":  payment_breakdown,
        "forecast":           forecast_data,
        "ai_insight":         insight if success_ai else "AI insight temporarily unavailable."
        # MAPE and RMSE intentionally removed — not meaningful to the business owner
    }


@app.route('/api/report_data')
@app.route('/api/report-data')
@login_required
def api_report_data():
    branch_filter = request.args.get('branch', 'all')
    rpt_type      = request.args.get('type',   'weekly')
    date_from     = request.args.get('date_from', None)
    date_to       = request.args.get('date_to',   None)

    try:
        result = _build_report_data(branch_filter, rpt_type, date_from, date_to)
        return jsonify(result)
    except Exception as e:
        print("Report API Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/export-pdf')
@login_required
def api_export_pdf():
    branch_filter = request.args.get('branch', 'all')
    rpt_type      = request.args.get('type',   'executive')
    date_from     = request.args.get('date_from', None)
    date_to       = request.args.get('date_to',   None)

    try:
        report_data = _build_report_data(branch_filter, rpt_type, date_from, date_to)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Data error: {str(e)}"}), 500

    branch_label = branch_filter if branch_filter != 'all' else 'All Branches'
    now_str      = datetime.now().strftime('%d %b %Y, %I:%M %p')
    today        = datetime.now().strftime('%A, %d %B %Y')

    # ── Forecast table rows ───────────────────────────────────
    forecast_rows_html = ""
    fore     = report_data.get('forecast', [])
    fore_max = max((r['yhat'] for r in fore), default=1)
    for i, r in enumerate(fore):
        d             = datetime.strptime(r['ds'], '%Y-%m-%d')
        day           = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.weekday() % 7]
        pct           = round((r['yhat'] / fore_max) * 100, 1)
        holiday_badge = ' 🎉' if r.get('is_holiday') else ''
        forecast_rows_html += f"""
        <tr style="background:{'#F8FAFC' if i%2==1 else 'white'};">
            <td style="font-family:monospace;padding:5px 8px;">{r['ds']}</td>
            <td style="padding:5px 8px;">{day}{holiday_badge}</td>
            <td style="padding:5px 8px;">
                <div style="background:#E2E8F0;border-radius:3px;height:6px;overflow:hidden;">
                    <div style="width:{pct}%;height:100%;background:#3B82F6;border-radius:3px;"></div>
                </div>
            </td>
            <td style="text-align:right;font-family:monospace;font-weight:700;color:#3B82F6;padding:5px 8px;">RM {r['yhat']:,.2f}</td>
            <td style="text-align:right;font-family:monospace;color:#94A3B8;font-size:10px;padding:5px 8px;">{r.get('yhat_lower', 0):,.2f}</td>
            <td style="text-align:right;font-family:monospace;color:#94A3B8;font-size:10px;padding:5px 8px;">{r.get('yhat_upper', 0):,.2f}</td>
        </tr>"""

    total_7day = sum(r['yhat'] for r in fore)

    # ── Product performance rows ──────────────────────────────
    top_prod_rows_html = ""
    for i, p in enumerate(report_data.get('top_products', [])):
        top_prod_rows_html += f"""
        <tr style="background:{'#F8FAFC' if i%2==1 else 'white'};">
            <td style="padding:5px 8px;">
                <span style="display:inline-block;width:18px;height:18px;border-radius:50%;
                      background:#3B82F6;color:white;font-size:9px;font-weight:700;
                      text-align:center;line-height:18px;">{i+1}</span> {p['product_name']}
            </td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">{int(p['qty']):,}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">RM {p['revenue']:,.2f}</td>
            <td style="text-align:right;padding:5px 8px;color:#64748B;">{p['pct']}%</td>
        </tr>"""

    bottom_prod_rows_html = ""
    for i, p in enumerate(report_data.get('bottom_products', [])):
        bottom_prod_rows_html += f"""
        <tr style="background:{'#F8FAFC' if i%2==1 else 'white'};">
            <td style="padding:5px 8px;color:#EF4444;">{p['product_name']}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">{int(p['qty']):,}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">RM {p['revenue']:,.2f}</td>
        </tr>"""

    # ── Category rows ─────────────────────────────────────────
    cat_rows_html = ""
    cat_total_rev = sum(c['revenue'] for c in report_data.get('category_breakdown', [])) or 1
    for i, c in enumerate(report_data.get('category_breakdown', [])):
        bar_w = round((c['revenue'] / cat_total_rev) * 100, 1)
        cat_rows_html += f"""
        <tr style="background:{'#F8FAFC' if i%2==1 else 'white'};">
            <td style="padding:5px 8px;">{c['product_category']}</td>
            <td style="text-align:right;font-family:monospace;padding:5px 8px;">RM {c['revenue']:,.2f}</td>
            <td style="text-align:right;padding:5px 8px;">{c['pct']}%</td>
            <td style="padding:5px 8px;width:80px;">
                <div style="background:#E2E8F0;border-radius:3px;height:6px;overflow:hidden;">
                    <div style="width:{bar_w}%;height:100%;
                         background:linear-gradient(90deg,#3B82F6,#60A5FA);border-radius:3px;"></div>
                </div>
            </td>
        </tr>"""

    # ── Payment method rows ───────────────────────────────────
    pay_rows_html = ""
    pay_total  = sum(p['txn_count'] for p in report_data.get('payment_breakdown', [])) or 1
    pay_colors = ['#3B82F6', '#F59E0B', '#10B981', '#8B5CF6']
    for i, p in enumerate(report_data.get('payment_breakdown', [])):
        bar_w = round((p['txn_count'] / pay_total) * 100, 1)
        pay_rows_html += f"""
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:10px;">
            <div style="width:100px;flex-shrink:0;color:#334155;">{p['payment_method']}</div>
            <div style="flex:1;background:#E2E8F0;border-radius:4px;height:8px;overflow:hidden;">
                <div style="width:{bar_w}%;height:100%;
                     background:{pay_colors[i % 4]};border-radius:4px;"></div>
            </div>
            <div style="width:40px;text-align:right;font-family:monospace;
                 font-weight:600;color:#1E293B;">{p['pct']}%</div>
        </div>"""

    # ── Hourly breakdown rows ─────────────────────────────────
    hourly_rows_html = ""
    hourly_data   = report_data.get('hourly_breakdown', [])
    hourly_max    = max((h['txns'] for h in hourly_data), default=1)
    peak_hour_val = report_data.get('peak_hour', 'N/A')
    for h in hourly_data:
        bar_w   = round((h['txns'] / hourly_max) * 100, 1)
        is_peak = peak_hour_val.startswith(h['hour'][:2])
        bg      = '#EFF6FF' if is_peak else 'white'
        bar_col = '#1D4ED8' if is_peak else '#93C5FD'
        hourly_rows_html += f"""
        <tr style="background:{bg};">
            <td style="font-family:monospace;padding:4px 8px;font-size:9px;">
                {'⭐ ' if is_peak else ''}{h['hour']}
            </td>
            <td style="padding:4px 8px;">
                <div style="background:#E2E8F0;border-radius:3px;height:5px;overflow:hidden;">
                    <div style="width:{bar_w}%;height:100%;background:{bar_col};border-radius:3px;"></div>
                </div>
            </td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;font-size:9px;">{h['txns']:,}</td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;
                font-size:9px;color:#64748B;">RM {h['revenue']:,.2f}</td>
        </tr>"""

    # ── Day-of-week breakdown rows ────────────────────────────
    dow_rows_html = ""
    dow_data      = report_data.get('dow_breakdown', [])
    dow_max       = max((d['txns'] for d in dow_data), default=1)
    peak_day_val  = report_data.get('peak_day', 'N/A')
    for d in dow_data:
        bar_w   = round((d['txns'] / dow_max) * 100, 1)
        is_peak = (d['day'] == peak_day_val)
        bg      = '#ECFDF5' if is_peak else 'white'
        bar_col = '#059669' if is_peak else '#6EE7B7'
        dow_rows_html += f"""
        <tr style="background:{bg};">
            <td style="padding:4px 8px;font-size:9px;">{'⭐ ' if is_peak else ''}{d['day']}</td>
            <td style="padding:4px 8px;">
                <div style="background:#E2E8F0;border-radius:3px;height:5px;overflow:hidden;">
                    <div style="width:{bar_w}%;height:100%;background:{bar_col};border-radius:3px;"></div>
                </div>
            </td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;font-size:9px;">{d['txns']:,}</td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;
                font-size:9px;color:#64748B;">RM {d['revenue']:,.2f}</td>
        </tr>"""

    # ── Daily breakdown rows (for weekly report PDF) ──────────
    daily_rows_html = ""
    for i, row in enumerate(report_data.get('daily', [])):
        d   = datetime.strptime(row['date'], '%Y-%m-%d')
        day = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.weekday() % 7]
        row_aov = round(row['revenue'] / row['txns'], 2) if row['txns'] > 0 else 0
        daily_rows_html += f"""
        <tr style="background:{'#F8FAFC' if i%2==1 else 'white'};">
            <td style="font-family:monospace;padding:4px 8px;font-size:9px;">{row['date']}</td>
            <td style="padding:4px 8px;font-size:9px;">{day}</td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;">RM {row['revenue']:,.2f}</td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;">{row['txns']:,}</td>
            <td style="text-align:right;font-family:monospace;padding:4px 8px;
                color:#64748B;">RM {row_aov:,.2f}</td>
        </tr>"""

    ai_insight            = report_data.get('ai_insight', 'No AI insight available.')
    ai_insight_clean_html = ai_insight.replace('\n', '<br>').replace('**', '')
    aov_val               = report_data.get('aov', 0)

    # ── Forecast section HTML (shared between report types) ───
    if fore:
        forecast_section_html = f"""
        <table>
            <thead>
                <tr>
                    <th>Date</th><th>Day</th>
                    <th style="width:120px;">Trend</th>
                    <th style="text-align:right;">Forecast (RM)</th>
                    <th style="text-align:right;">Lower</th>
                    <th style="text-align:right;">Upper</th>
                </tr>
            </thead>
            <tbody>{forecast_rows_html}</tbody>
        </table>
        <div class="three-col" style="margin-top:8px;">
            <div class="insight-box">
                <strong>7-Day Total:</strong> RM {total_7day:,.2f}
            </div>
            <div class="insight-box" style="background:#ECFDF5;border-color:#6EE7B7;color:#065F46;">
                <strong>Peak Hour:</strong> {report_data['peak_hour']}
            </div>
            <div class="insight-box" style="background:#FFFBEB;border-color:#FDE68A;color:#92400E;">
                <strong>Peak Day:</strong> {report_data['peak_day']}
            </div>
        </div>"""
    else:
        forecast_section_html = """
        <div style="color:#94A3B8;text-align:center;padding:12px;background:#F8FAFC;
             border-radius:6px;font-size:10px;">
            No forecast generated. Run the AI Forecast engine first.
        </div>"""

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>MCS Report - {branch_label}</title>
<style>
  @page {{ size: A4; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Arial', sans-serif; font-size: 11px; color: #1E293B; width: 794px; }}
  .header {{ background: #1E2A3A; padding: 28px 36px 22px; color: white; }}
  .brand {{ display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }}
  .brand-icon {{ width:36px;height:36px;background:#F59E0B;border-radius:7px;
                 display:flex;align-items:center;justify-content:center;font-size:18px; }}
  .accent {{ height:4px;background:linear-gradient(90deg,#F59E0B,#3B82F6,#10B981); }}
  .body {{ padding: 24px 36px; }}
  .section-title {{ font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;
                    border-left:3px solid #3B82F6;padding-left:7px;margin:18px 0 10px;color:#1E2A3A; }}
  .kpi-5col {{ display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:4px; }}
  .kpi-box {{ border:1px solid #E2E8F0;border-radius:7px;padding:10px 12px;background:#F8FAFC; }}
  .kpi-label {{ font-size:8px;color:#64748B;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px; }}
  .kpi-value {{ font-size:15px;font-weight:700;color:#1E293B;font-family:monospace; }}
  .kpi-sub {{ font-size:8px;color:#94A3B8;margin-top:2px; }}
  table {{ width:100%;border-collapse:collapse;font-size:10px; }}
  thead th {{ background:#1E2A3A;color:white;padding:6px 8px;text-align:left;
              font-size:9px;letter-spacing:.4px; }}
  tbody td {{ padding:5px 8px;border-bottom:1px solid #E2E8F0;color:#334155; }}
  .two-col {{ display:grid;grid-template-columns:1fr 1fr;gap:14px; }}
  .three-col {{ display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px; }}
  .insight-box {{ background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;
                  padding:9px 11px;font-size:10px;color:#1E40AF;line-height:1.6;margin-bottom:6px; }}
  .ai-box {{ background:linear-gradient(135deg,#F8FAFC,#EFF6FF);
             border:1px solid #BFDBFE;border-radius:7px;padding:12px 14px; }}
  .footer {{ padding:10px 36px;background:#F8FAFC;border-top:1px solid #E2E8F0;
             display:flex;justify-content:space-between;font-size:8px;color:#94A3B8; }}
  .no-data {{ color:#94A3B8;text-align:center;padding:10px;font-size:9px; }}
  .data-note {{ background:#FFFBEB;border:1px solid #FDE68A;border-radius:5px;
                padding:7px 10px;font-size:9px;color:#92400E;margin-top:6px; }}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="brand">
    <div class="brand-icon">☕</div>
    <div>
      <div style="font-size:14px;font-weight:700;">Mini Coffee Shop</div>
      <div style="font-size:9px;opacity:.6;">AI-Powered Analytics System</div>
    </div>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:flex-end;">
    <div>
      <div style="font-size:20px;font-weight:700;">
        {'Executive Summary' if rpt_type == 'executive' else 'Weekly Sales'} Report
      </div>
      <div style="font-size:10px;opacity:.7;margin-top:3px;">{branch_label} · {today}</div>
    </div>
    <div style="text-align:right;font-size:9px;opacity:.7;line-height:1.7;">
      Generated: {now_str}<br>
      Period: {report_data['period']}<br>
      Confidential — Internal Use Only
    </div>
  </div>
</div>
<div class="accent"></div>

<div class="body">

  <!-- SECTION 1: EXECUTIVE SUMMARY KPIs -->
  <div class="section-title">1. Executive Summary</div>
  <div class="kpi-5col">
    <div class="kpi-box" style="border-top:3px solid #3B82F6;">
      <div class="kpi-label">Total Revenue</div>
      <div class="kpi-value">RM {report_data['period_revenue']:,.2f}</div>
      <div class="kpi-sub">{report_data['trend_label']}</div>
    </div>
    <div class="kpi-box" style="border-top:3px solid #F59E0B;">
      <div class="kpi-label">Transactions</div>
      <div class="kpi-value">{report_data['period_txns']:,}</div>
      <div class="kpi-sub">Total recorded</div>
    </div>
    <div class="kpi-box" style="border-top:3px solid #10B981;">
      <div class="kpi-label">Avg Order Value</div>
      <div class="kpi-value">RM {aov_val:,.2f}</div>
      <div class="kpi-sub">Revenue ÷ transactions</div>
    </div>
    <div class="kpi-box" style="border-top:3px solid #06B6D4;">
      <div class="kpi-label">Daily Average</div>
      <div class="kpi-value">RM {report_data['daily_average']:,.2f}</div>
      <div class="kpi-sub">Per active day</div>
    </div>
    <div class="kpi-box" style="border-top:3px solid #8B5CF6;">
      <div class="kpi-label">Top Branch</div>
      <div class="kpi-value" style="font-size:11px;margin-top:3px;">{report_data['top_branch']}</div>
      <div class="kpi-sub">Period leader</div>
    </div>
  </div>
  <div class="data-note">
    ⚠ Profit &amp; COGS data is not available from POS transaction logs.
    Revenue figures represent gross sales only.
  </div>

  <!-- SECTION 2: PRODUCT PERFORMANCE -->
  <div class="section-title">2. Product Performance</div>
  <div class="two-col">
    <div>
      <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">
        🏆 Top Selling Items
      </div>
      {'<table><thead><tr><th>Product</th><th style="text-align:right;">Units Sold</th><th style="text-align:right;">Revenue</th><th style="text-align:right;">Share</th></tr></thead><tbody>' + top_prod_rows_html + '</tbody></table>' if top_prod_rows_html else '<div class="no-data">No product data available</div>'}
    </div>
    <div>
      <div style="font-size:9px;font-weight:700;color:#EF4444;margin-bottom:6px;">
        ⚠ Slow-Moving Items (Bottom 3)
      </div>
      {'<table><thead><tr><th>Product</th><th style="text-align:right;">Units Sold</th><th style="text-align:right;">Revenue</th></tr></thead><tbody>' + bottom_prod_rows_html + '</tbody></table>' if bottom_prod_rows_html else '<div class="no-data">No data available</div>'}
    </div>
  </div>

  <!-- SECTION 3: CATEGORY & PAYMENT BREAKDOWN -->
  <div class="section-title">3. Category Revenue &amp; Payment Methods</div>
  <div class="two-col">
    <div>
      <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">
        Revenue by Category
      </div>
      {'<table><thead><tr><th>Category</th><th style="text-align:right;">Revenue</th><th style="text-align:right;">Share</th><th style="width:80px;">Bar</th></tr></thead><tbody>' + cat_rows_html + '</tbody></table>' if cat_rows_html else '<div class="no-data">No category data</div>'}
    </div>
    <div>
      <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:8px;">
        Payment Method Breakdown
      </div>
      {pay_rows_html if pay_rows_html else '<div class="no-data">No payment data</div>'}
    </div>
  </div>

  <!-- SECTION 4: TEMPORAL & BEHAVIORAL ANALYTICS -->
  <div class="section-title">4. Temporal &amp; Behavioral Analytics</div>
  <div class="two-col">
    <div>
      <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">
        ⏰ Hourly Transaction Volume
      </div>
      {'<table><thead><tr><th>Hour</th><th>Volume</th><th style="text-align:right;">Txns</th><th style="text-align:right;">Revenue</th></tr></thead><tbody>' + hourly_rows_html + '</tbody></table>' if hourly_rows_html else '<div class="no-data">No hourly data</div>'}
    </div>
    <div>
      <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">
        📅 Day-of-Week Breakdown
      </div>
      {'<table><thead><tr><th>Day</th><th>Volume</th><th style="text-align:right;">Txns</th><th style="text-align:right;">Revenue</th></tr></thead><tbody>' + dow_rows_html + '</tbody></table>' if dow_rows_html else '<div class="no-data">No day data</div>'}
      <div style="margin-top:10px;background:#ECFDF5;border:1px solid #6EE7B7;
           border-radius:6px;padding:9px 11px;font-size:10px;color:#065F46;">
        <strong>Peak Hour:</strong> {report_data['peak_hour']}
        &nbsp;|&nbsp;
        <strong>Peak Day:</strong> {report_data['peak_day']}
      </div>
    </div>
  </div>

  <!-- SECTION 5: DAILY SALES DETAIL (weekly report only) -->
  {'<div class="section-title">5. Daily Sales Detail</div><table><thead><tr><th>Date</th><th>Day</th><th style="text-align:right;">Revenue</th><th style="text-align:right;">Transactions</th><th style="text-align:right;">AOV</th></tr></thead><tbody>' + daily_rows_html + '</tbody></table>' if rpt_type == 'weekly' and daily_rows_html else ''}

  <!-- SECTION 5/6: 7-DAY FORECAST -->
  <div class="section-title">
    {'6' if rpt_type == 'weekly' else '5'}. 7-Day Prophet Revenue Forecast
  </div>
  {forecast_section_html}

  <!-- SECTION 6/7: AI RECOMMENDATIONS -->
  <div class="section-title">
    {'7' if rpt_type == 'weekly' else '6'}. AI-Generated Strategic Recommendations
  </div>
  <div class="ai-box">
    <div style="display:flex;align-items:center;gap:7px;margin-bottom:7px;
         font-weight:700;font-size:11px;">
      <span style="background:linear-gradient(135deg,#3B82F6,#8B5CF6);color:white;
            padding:2px 7px;border-radius:3px;font-size:8px;">GEMINI AI</span>
      Operational Insights &amp; Action Plan
    </div>
    <div style="font-size:10px;color:#334155;line-height:1.7;">{ai_insight_clean_html}</div>
  </div>

  <!-- SECTION 7/8: OPERATIONAL NOTES -->
  <div class="section-title" style="margin-top:14px;">
    {'8' if rpt_type == 'weekly' else '7'}. Operational Notes &amp; Recommended Actions
  </div>
  <div class="two-col">
    <div>
      <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">
        📌 Key Observations
      </div>
      <div style="font-size:10px;line-height:1.9;color:#334155;">
        ● Revenue trend: <strong>{report_data['trend_label']}</strong><br>
        ● Top branch: <strong>{report_data['top_branch']}</strong><br>
        ● Avg order value (AOV): <strong>RM {aov_val:,.2f}</strong><br>
        ● Busiest hour: <strong>{report_data['peak_hour']}</strong><br>
        ● Busiest day: <strong>{report_data['peak_day']}</strong>
      </div>
    </div>
    <div>
      <div style="font-size:9px;font-weight:700;color:#1E293B;margin-bottom:6px;">
        ✅ Recommended Actions
      </div>
      <div style="font-size:10px;line-height:1.9;color:#334155;">
        ✓ Schedule extra baristas during <strong>{report_data['peak_hour']}</strong><br>
        ✓ Pre-stock top products before <strong>{report_data['peak_day']}</strong><br>
        ✓ Bundle slow-moving items with top sellers for promotions<br>
        ✓ Run afternoon deals (2PM–4PM) to lift off-peak revenue<br>
        ✓ Monitor cashless payment uptake for loyalty integration
      </div>
    </div>
  </div>

</div>

<div class="footer">
  <div>Mini Coffee Shop · MCS Analytics v1.0</div>
  <div>Generated {now_str} · Confidential</div>
  <div>Page 1 of 1</div>
</div>
</body>
</html>"""

    try:
        from weasyprint import HTML as WP_HTML
        pdf_bytes = WP_HTML(string=html_doc).write_pdf()
        response  = make_response(pdf_bytes)
        response.headers['Content-Type']        = 'application/pdf'
        response.headers['Content-Disposition'] = (
            f'attachment; filename="MCS_Report_{branch_filter.replace(" ","_")}'
            f'_{datetime.now().strftime("%Y-%m-%d")}.pdf"'
        )
        return response

    except ImportError:
        response = make_response(html_doc)
        response.headers['Content-Type']        = 'text/html; charset=utf-8'
        response.headers['Content-Disposition'] = (
            f'attachment; filename="MCS_Report_{branch_filter.replace(" ","_")}'
            f'_{datetime.now().strftime("%Y-%m-%d")}.html"'
        )
        return response

    except Exception as e:
        return jsonify({"status": "error", "message": f"PDF render error: {str(e)}"}), 500


# ============================================================
#   RUNTIME ENGINE EXECUTION ENTRYPOINT
# ============================================================
if __name__ == '__main__':
    app.run(debug=True)