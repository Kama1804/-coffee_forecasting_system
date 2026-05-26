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
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether
)
from io import BytesIO

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
REPORT_CACHE = {}
FORECAST_CACHE = {}


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
        # Check global cache
        cache_key = f"{branch_id}_{branch_name}"
        if cache_key in FORECAST_CACHE:
            result = FORECAST_CACHE[cache_key]
        else:
            engine          = ForecastEngine()
            success, result = engine.generate_7_day_forecast(branch_id, branch_name)
            if not success:
                return jsonify({"status": "error", "message": result}), 500
            
            # Save to global cache
            FORECAST_CACHE[cache_key] = result

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
        # Check global cache first
        cache_key = f"{branch_filter}_{date_from}_{date_to}"
        if cache_key in REPORT_CACHE:
            return jsonify(REPORT_CACHE[cache_key])

        result = _build_report_data(branch_filter, date_from, date_to)
        
        # Save to global cache
        REPORT_CACHE[cache_key] = result
        
        return jsonify(result)
    except Exception as e:
        print("Report API Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — PDF EXPORT (Forecast Report)
# ============================================================
@app.route('/api/export-forecast-pdf')
@login_required
def api_export_forecast_pdf():
    branch_id   = request.args.get('branch_id',   1,            type=int)
    branch_name = request.args.get('branch_name', 'Putrajaya', type=str)
    month_filter = request.args.get('month',       None,         type=str)

    try:
        # Check global forecast cache
        cache_key = f"{branch_id}_{branch_name}"
        if cache_key in FORECAST_CACHE:
             result = FORECAST_CACHE[cache_key]
        else:
            engine          = ForecastEngine()
            success, result = engine.generate_7_day_forecast(branch_id, branch_name)
            if not success:
                return jsonify({"status": "error", "message": result}), 500
            
            # Save to global cache
            FORECAST_CACHE[cache_key] = result

        # Build data components
        forecast_rows = ""
        for row in result['forecast']:
            promos = " ".join([f"<span style='background:#FEF3C7;color:#92400E;padding:1px 4px;border-radius:3px;font-size:8px;font-weight:600;margin-right:3px;'>{p}</span>" for p in row.get('promotions', [])])
            if row.get('is_holiday'):
                promos += "<span style='background:#FEE2E2;color:#991B1B;padding:1px 4px;border-radius:3px;font-size:8px;font-weight:600;margin-right:3px;'>🏖️ Holiday</span>"
            
            day_name = datetime.strptime(row['ds'], '%Y-%m-%d').strftime('%A')
            row_style = "background:#F8FAFC;" if row.get('is_holiday') else ("background:#FEF3C7;" if day_name in ['Saturday','Sunday'] else "")
            
            forecast_rows += f"""
            <tr style="{row_style}">
                <td style="font-family:monospace;padding:5px 8px;">{row['ds']}</td>
                <td style="padding:5px 8px;">{day_name}</td>
                <td style="padding:5px 8px;">{row['weather']}</td>
                <td style="padding:5px 8px;">{promos}</td>
                <td style="text-align:right;font-family:monospace;padding:5px 8px;font-weight:700;">RM {row['yhat']:,.2f}</td>
                <td style="text-align:right;font-family:monospace;padding:5px 8px;color:#64748B;">RM {row['yhat_lower']:,.2f}</td>
                <td style="text-align:right;font-family:monospace;padding:5px 8px;color:#64748B;">RM {row['yhat_upper']:,.2f}</td>
            </tr>"""

        # Predicted vs Actual Section
        pva_section_html = ""
        if month_filter:
            pva_data = [r for r in result.get('forecast_vs_actual', []) if r['ds'].startswith(month_filter)]
            if pva_data:
                pva_rows = ""
                total_p = 0
                total_a = 0
                mape_sum = 0
                count = 0
                
                for r in pva_data:
                    var = r['actual'] - r['predicted']
                    var_pct = (var / r['predicted'] * 100) if r['predicted'] > 0 else 0
                    var_color = "#059669" if var >= 0 else "#DC2626"
                    
                    total_p += r['predicted']
                    total_a += r['actual']
                    mape_sum += abs(var_pct)
                    count += 1
                    
                    pva_rows += f"""
                    <tr>
                        <td style="font-family:monospace;padding:4px 8px;">{r['ds']}</td>
                        <td style="text-align:right;font-family:monospace;padding:4px 8px;">RM {r['predicted']:,.2f}</td>
                        <td style="text-align:right;font-family:monospace;padding:4px 8px;">RM {r['actual']:,.2f}</td>
                        <td style="text-align:right;font-family:monospace;padding:4px 8px;color:{var_color};">
                            {'+' if var>=0 else ''}RM {var:,.2f} ({'+' if var_pct>=0 else ''}{var_pct:.1f}%)
                        </td>
                    </tr>"""
                
                avg_mape = mape_sum / count if count > 0 else 0
                month_label = datetime.strptime(month_filter + "-01", "%Y-%m-%d").strftime("%B %Y")
                
                pva_section_html = f"""
                <div style="page-break-before: always;"></div>
                <div class="section-title">Historical Performance: {month_label}</div>
                <div class="kpi-grid">
                    <div class="kpi-box"><div class="kpi-label">Month Total Predicted</div><div class="kpi-value">RM {total_p:,.2f}</div></div>
                    <div class="kpi-box"><div class="kpi-label">Month Total Actual</div><div class="kpi-value">RM {total_a:,.2f}</div></div>
                    <div class="kpi-box"><div class="kpi-label">Avg. Month Accuracy</div><div class="kpi-value">{max(0, round(100 - avg_mape, 1))}%</div></div>
                    <div class="kpi-box"><div class="kpi-label">Variance</div><div class="kpi-value" style="color:{'#059669' if total_a >= total_p else '#DC2626'};">RM {total_a - total_p:,.2f}</div></div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Date</th>
                            <th style="text-align:right;">Predicted (RM)</th>
                            <th style="text-align:right;">Actual (RM)</th>
                            <th style="text-align:right;">Variance</th>
                        </tr>
                    </thead>
                    <tbody>
                        {pva_rows}
                    </tbody>
                </table>
                """

        now_str = datetime.now().strftime('%d %b %Y, %I:%M %p')
        
        html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4 portrait; margin: 15mm 12mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; font-size: 10px; color: #1E293B; }}
  .header {{ background: linear-gradient(135deg,#1E2A3A,#2A3B52); padding: 25px 30px; color: white; }}
  .brand {{ display:flex;align-items:center;gap:10px;margin-bottom:10px; }}
  .accent {{ height:4px;background:linear-gradient(90deg,#F59E0B,#3B82F6,#10B981); }}
  .body {{ padding:20px 30px; }}
  .section-title {{ font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;border-left:3px solid #3B82F6;padding-left:7px;margin:15px 0 10px;color:#1E2A3A; }}
  .kpi-grid {{ display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:15px; }}
  .kpi-box {{ border:1px solid #E2E8F0;border-radius:6px;padding:10px;background:#F8FAFC; }}
  .kpi-label {{ font-size:8px;color:#64748B;text-transform:uppercase;margin-bottom:3px; }}
  .kpi-value {{ font-size:14px;font-weight:700;font-family:monospace; }}
  table {{ width:100%;border-collapse:collapse;margin-top:5px; }}
  thead th {{ background:#1E2A3A;color:white;padding:6px 8px;text-align:left;font-size:9px; }}
  tbody td {{ padding:5px 8px;border-bottom:1px solid #E2E8F0; }}
  .footer {{ padding:10px 30px;background:#F8FAFC;border-top:1px solid #E2E8F0;display:flex;justify-content:space-between;font-size:8px;color:#94A3B8; }}
</style>
</head>
<body>
<div class="header">
    <div class="brand"><div style="background:#F59E0B;padding:5px;border-radius:5px;">☕</div> <strong>Mini Coffee Shop</strong></div>
    <div style="font-size:18px;font-weight:700;">AI Sales Forecast Report</div>
    <div style="font-size:10px;opacity:.8;">Branch: {branch_name} | Generated: {now_str}</div>
</div>
<div class="accent"></div>
<div class="body">
    <div class="section-title">Model Performance & Persona</div>
    <div class="kpi-grid">
        <div class="kpi-box"><div class="kpi-label">MAPE</div><div class="kpi-value">{result['mape']}%</div></div>
        <div class="kpi-box"><div class="kpi-label">RMSE</div><div class="kpi-value">RM {result['rmse']:,.2f}</div></div>
        <div class="kpi-box"><div class="kpi-label">Accuracy</div><div class="kpi-value">{result['accuracy']}%</div></div>
        <div class="kpi-box"><div class="kpi-label">7-Day Total</div><div class="kpi-value">RM {sum(r['yhat'] for r in result['forecast']):,.2f}</div></div>
    </div>
    <div style="background:#EFF6FF;padding:10px;border-radius:6px;margin-bottom:15px;font-size:10px;">
        <strong>Branch Persona:</strong> {result['persona']}
    </div>

    <div class="section-title">7-Day Prediction Breakdown</div>
    <table>
        <thead>
            <tr>
                <th>Date</th>
                <th>Day</th>
                <th>Weather</th>
                <th>Promotions/Flags</th>
                <th style="text-align:right;">Predicted (RM)</th>
                <th style="text-align:right;">Lower</th>
                <th style="text-align:right;">Upper</th>
            </tr>
        </thead>
        <tbody>
            {forecast_rows}
        </tbody>
    </table>

    {pva_section_html}
    
    <div style="margin-top:20px;font-size:9px;color:#64748B;line-height:1.6;">
        <strong>Note:</strong> This forecast is generated using Prophet with multiplicative seasonality, 
        incorporating Malaysian public holidays, weather regressors, and branch-specific demand patterns.
        95% confidence intervals (Lower/Upper) are provided for risk assessment.
    </div>
</div>
<div class="footer">
    <div>MCS Analytics v1.0</div>
    <div>Internal Use Only</div>
</div>
</body>
</html>"""

        from weasyprint import HTML as WP_HTML
        pdf_bytes = WP_HTML(string=html_doc).write_pdf()
        response  = make_response(pdf_bytes)
        response.headers['Content-Type']        = 'application/pdf'
        filename_str = f"MCS_Forecast_{branch_name.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.pdf"
        response.headers['Content-Disposition'] = f'attachment; filename="{filename_str}"'
        return response

    except ImportError:
        # Fallback to HTML if WeasyPrint is missing
        response = make_response(html_doc)
        response.headers['Content-Type']        = 'text/html; charset=utf-8'
        filename_str = f"MCS_Forecast_{branch_name.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.html"
        response.headers['Content-Disposition'] = f'attachment; filename="{filename_str}"'
        return response

    except Exception as e:
        return jsonify({"status": "error", "message": f"PDF render error: {str(e)}"}), 500


# ============================================================
#    API — PDF EXPORT (Executive Report Only, Month-Based)
# ============================================================
# ── Colour palette (flat, business) ───────────────────────────
C_DARK    = colors.HexColor('#1E293B')   # headings
C_MID     = colors.HexColor('#334155')   # body text
C_MUTED   = colors.HexColor('#64748B')   # sub-labels
C_LIGHT   = colors.HexColor('#F1F5F9')   # row alt / header bg
C_BLUE    = colors.HexColor('#2563EB')   # accent / KPI
C_TEAL    = colors.HexColor('#0D9488')   # positive
C_RED     = colors.HexColor('#DC2626')   # negative
C_AMBER   = colors.HexColor('#D97706')   # warning
C_WHITE   = colors.white
C_BORDER  = colors.HexColor('#CBD5E1')   # table borders
C_HDRROW  = colors.HexColor('#1E293B')   # table header bg
 
 
def _styles():
    base = getSampleStyleSheet()
 
    def add(name, **kw):
        if name not in base:
            base.add(ParagraphStyle(name=name, **kw))
        return base[name]
 
    add('RPT_Title',
        fontName='Helvetica-Bold', fontSize=16,
        textColor=C_DARK, spaceAfter=2, leading=20)
    add('RPT_Sub',
        fontName='Helvetica', fontSize=9,
        textColor=C_MUTED, spaceAfter=0, leading=12)
    add('RPT_SectionH',
        fontName='Helvetica-Bold', fontSize=10,
        textColor=C_DARK, spaceBefore=12, spaceAfter=4, leading=13)
    add('RPT_Body',
        fontName='Helvetica', fontSize=9,
        textColor=C_MID, spaceAfter=3, leading=13)
    add('RPT_BodySm',
        fontName='Helvetica', fontSize=8,
        textColor=C_MUTED, spaceAfter=2, leading=11)
    add('RPT_Mono',
        fontName='Courier', fontSize=8.5,
        textColor=C_MID, spaceAfter=2, leading=12)
    add('RPT_Bold',
        fontName='Helvetica-Bold', fontSize=9,
        textColor=C_DARK, spaceAfter=3, leading=13)
    add('RPT_AI',
        fontName='Helvetica', fontSize=9,
        textColor=C_MID, spaceAfter=4, leading=14,
        leftIndent=10)
    add('RPT_Bullet',
        fontName='Helvetica', fontSize=9,
        textColor=C_MID, spaceAfter=3, leading=13,
        leftIndent=16, bulletIndent=6)
    return base
 
 
def _table_style(has_header=True, stripe=True):
    cmds = [
        ('FONTNAME',  (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE',  (0, 0), (-1, -1), 8.5),
        ('TEXTCOLOR', (0, 0), (-1, -1), C_MID),
        ('VALIGN',    (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('GRID',      (0, 0), (-1, -1), 0.4, C_BORDER),
    ]
    if has_header:
        cmds += [
            ('BACKGROUND', (0, 0), (-1, 0), C_HDRROW),
            ('TEXTCOLOR',  (0, 0), (-1, 0), C_WHITE),
            ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, 0), 8.5),
        ]
    if stripe:
        # stripe every even data row
        cmds.append(('ROWBACKGROUNDS', (0, 1 if has_header else 0), (-1, -1),
                     [C_WHITE, C_LIGHT]))
    return TableStyle(cmds)
 
 
def _section(title, story, styles):
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width='100%', thickness=0.5, color=C_BORDER, spaceAfter=3))
    story.append(Paragraph(title, styles['RPT_SectionH']))
 
 
def _bar_cell(pct, bar_color=C_BLUE, width_pts=80):
    """Return a mini text-based bar (ASCII-style using underscores)."""
    filled = int(pct / 100 * 12)
    bar = '█' * filled + '░' * (12 - filled)
    return f'<font color="#{bar_color.hexval()[1:] if hasattr(bar_color,"hexval") else "2563EB"}">{bar}</font>'
 
 
# ── PAGE TEMPLATE (header + footer) ───────────────────────────
def _make_on_page(branch_label, month_label, now_str):
    def on_page(canvas, doc):
        canvas.saveState()
        w, h = A4
        # Header bar
        canvas.setFillColor(C_DARK)
        canvas.rect(0, h - 28*mm, w, 28*mm, fill=1, stroke=0)
        # Accent line
        canvas.setFillColor(C_BLUE)
        canvas.rect(0, h - 29.5*mm, w, 1.5*mm, fill=1, stroke=0)
 
        canvas.setFillColor(C_WHITE)
        canvas.setFont('Helvetica-Bold', 13)
        canvas.drawString(14*mm, h - 14*mm, '☕  Mini Coffee Shop — Executive Sales Report')
        canvas.setFont('Helvetica', 8)
        canvas.drawString(14*mm, h - 20*mm, f'{branch_label}  ·  {month_label}  ·  Confidential — Internal Use Only')
        canvas.drawRightString(w - 14*mm, h - 20*mm, f'Generated: {now_str}')
 
        # Footer
        canvas.setFillColor(C_MUTED)
        canvas.setFont('Helvetica', 7.5)
        canvas.drawString(14*mm, 8*mm, 'MCS Analytics v1.0 — Internal Use Only')
        canvas.drawCentredString(w / 2, 8*mm, f'Page {doc.page}')
        canvas.drawRightString(w - 14*mm, 8*mm, f'{branch_label}  ·  {month_label}')
        canvas.restoreState()
    return on_page
 
 
@app.route('/api/export-pdf')
@login_required
def api_export_pdf():
    branch_filter = request.args.get('branch',    'all')
    date_from     = request.args.get('date_from', None)
    date_to       = request.args.get('date_to',   None)
 
    if not date_from or not date_to:
        return jsonify({"status": "error", "message": "date_from and date_to are required."}), 400
 
    # ── Fetch / global cache report data ──────────────────────
    try:
        cache_key = f"{branch_filter}_{date_from}_{date_to}"
        if cache_key in REPORT_CACHE:
            report_data = REPORT_CACHE[cache_key]
        else:
            report_data = _build_report_data(branch_filter, date_from, date_to)
            REPORT_CACHE[cache_key] = report_data
    except Exception as e:
        return jsonify({"status": "error", "message": f"Data error: {str(e)}"}), 500
 
    # ── Derived labels ─────────────────────────────────────────
    branch_label = branch_filter if branch_filter != 'all' else 'All Branches'
    now_str      = datetime.now().strftime('%d %b %Y, %I:%M %p')
    try:
        month_label = datetime.strptime(date_from, '%Y-%m-%d').strftime('%B %Y')
        month_key   = datetime.strptime(date_from, '%Y-%m-%d').strftime('%Y-%m')
    except Exception:
        month_label = date_from
        month_key   = date_from[:7]
 
    # ── Build PDF with ReportLab ───────────────────────────────
    buf    = BytesIO()
    styles = _styles()
    M      = 14 * mm
 
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=M, rightMargin=M,
        topMargin=34*mm, bottomMargin=18*mm,
    )
 
    story = []
 
    # ── Helpers ────────────────────────────────────────────────
    def P(text, style='RPT_Body'):
        return Paragraph(text, styles[style])
 
    def num(n, dp=2):
        return f'{float(n or 0):,.{dp}f}'
 
    def pct_bar(pct, width=60):
        """Tiny visual bar using filled chars."""
        filled = max(0, min(12, int((pct or 0) / 100 * 12)))
        return '█' * filled + '░' * (12 - filled)
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 1 — EXECUTIVE SUMMARY
    # ─────────────────────────────────────────────────────────
    _section('1.  Executive Summary', story, styles)
 
    rd            = report_data
    period_rev    = rd.get('period_revenue', 0)
    period_txns   = rd.get('period_txns', 0)
    daily_avg     = rd.get('daily_average', 0)
    aov           = rd.get('aov', 0)
    trend_label   = rd.get('trend_label', 'N/A')
    top_branch    = rd.get('top_branch', 'N/A')
    peak_hour     = rd.get('peak_hour', 'N/A')
    peak_day      = rd.get('peak_day', 'N/A')
    days_in_p     = max(len(rd.get('daily', [])), 1)
    avg_daily_t   = round(period_txns / days_in_p, 1)
 
    kpi_data = [
        ['Metric', 'Value', 'Metric', 'Value'],
        ['Total Revenue',      f'RM {num(period_rev)}',  'Sales Volume',      f'{int(period_txns):,} orders'],
        ['Daily Average',      f'RM {num(daily_avg)}',   'Avg Order Value',   f'RM {num(aov)}'],
        ['Period vs Prior',    trend_label,               'Top Branch',        top_branch],
        ['Peak Hour',          peak_hour,                 'Peak Day',          peak_day],
        ['Reporting Period',   f'{date_from} to {date_to}', 'Avg Daily Orders', str(avg_daily_t)],
    ]
 
    kpi_tbl = Table(kpi_data, colWidths=[45*mm, 45*mm, 45*mm, 45*mm])
    kpi_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), C_HDRROW),
        ('TEXTCOLOR',     (0, 0), (-1, 0), C_WHITE),
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME',      (0, 1), (-1, -1), 'Helvetica'),
        ('FONTNAME',      (0, 1), (0, -1), 'Helvetica-Bold'),  # left label col bold
        ('FONTNAME',      (2, 1), (2, -1), 'Helvetica-Bold'),  # right label col bold
        ('FONTSIZE',      (0, 0), (-1, -1), 8.5),
        ('TEXTCOLOR',     (0, 1), (-1, -1), C_MID),
        ('TEXTCOLOR',     (1, 1), (1, -1), C_BLUE),
        ('TEXTCOLOR',     (3, 1), (3, -1), C_BLUE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(KeepTogether([kpi_tbl]))
    story.append(Spacer(1, 4))
    story.append(P('⚠  Profit & COGS data not available from POS logs. Revenue figures represent gross sales only.',
                   'RPT_BodySm'))
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 2 — MONTHLY REVENUE TREND
    # ─────────────────────────────────────────────────────────
    mt         = rd.get('monthly_trend', {})
    mt_labels  = mt.get('labels',   [])
    mt_keys    = mt.get('keys',     [])
    mt_branches= mt.get('branches', [])
    mt_by_b    = mt.get('by_branch', {})
 
    slice_n   = min(6, len(mt_labels))
    start_idx = len(mt_labels) - slice_n
    rec_labels= mt_labels[start_idx:]
    rec_keys  = mt_keys[start_idx:]
 
    _section(f'2.  Revenue Trend — Last {slice_n} Months', story, styles)
 
    if rec_labels:
        hdr = ['Month'] + mt_branches + ['Total']
        rows = [hdr]
        for i, lbl in enumerate(rec_labels):
            ri       = start_idx + i
            is_curr  = (rec_keys[i] == month_key)
            total_r  = sum(mt_by_b.get(b, [0]*len(mt_labels))[ri] for b in mt_branches)
            row      = [f'{"► " if is_curr else ""}{lbl}']
            row     += [f'RM {num(mt_by_b.get(b,[0]*len(mt_labels))[ri])}' for b in mt_branches]
            row     += [f'RM {num(total_r)}']
            rows.append(row)
 
        n_cols   = len(hdr)
        col_w    = [30*mm] + [None]*(n_cols-2) + [30*mm]
        avail    = 180*mm - 30*mm - 30*mm
        mid_w    = avail / max(n_cols - 2, 1)
        col_w[1:-1] = [mid_w] * (n_cols - 2)
 
        mt_tbl = Table(rows, colWidths=col_w)
        mt_style = _table_style()
        # highlight current month rows
        for i, k in enumerate(rec_keys):
            if k == month_key:
                mt_style.add('BACKGROUND', (0, i+1), (-1, i+1), colors.HexColor('#EFF6FF'))
                mt_style.add('FONTNAME',   (0, i+1), (-1, i+1), 'Helvetica-Bold')
                mt_style.add('TEXTCOLOR',  (-1, i+1), (-1, i+1), C_BLUE)
        # right-align numeric cols
        for c in range(1, n_cols):
            mt_style.add('ALIGN', (c, 0), (c, -1), 'RIGHT')
        mt_tbl.setStyle(mt_style)
        story.append(KeepTogether([mt_tbl]))
        story.append(P('► Highlighted row = selected reporting month.', 'RPT_BodySm'))
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 3 — DAILY SALES vs PREDICTED (PvA)
    # ─────────────────────────────────────────────────────────
    _section(f'3.  Daily Sales — Predicted vs Actual ({month_label})', story, styles)
 
    pva_rows     = rd.get('predicted_vs_actual', [])
    pva_total_p  = rd.get('pva_total_predicted', 0)
    pva_total_a  = rd.get('pva_total_actual', 0)
    pva_mape     = rd.get('pva_mape', 0)
    pva_in_range = rd.get('pva_within_range', 0)
    pva_days     = rd.get('pva_days_with_data', 0)
 
    # PvA summary row
    pva_sum = [
        ['Total Predicted', f'RM {num(pva_total_p)}',
         'Total Actual', f'RM {num(pva_total_a)}',
         'MAPE', f'{pva_mape}%',
         'On Target', f'{pva_in_range} / {pva_days} days']
    ]
    pva_sum_tbl = Table(pva_sum, colWidths=[30*mm, 30*mm, 25*mm, 30*mm, 18*mm, 20*mm, 20*mm, 28*mm])
    pva_sum_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), C_LIGHT),
        ('FONTNAME',      (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, -1), 8.5),
        ('TEXTCOLOR',     (0, 0), (-1, -1), C_DARK),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
        ('ALIGN',         (1, 0), (1, -1), 'RIGHT'),
        ('ALIGN',         (3, 0), (3, -1), 'RIGHT'),
        ('ALIGN',         (5, 0), (5, -1), 'RIGHT'),
        ('ALIGN',         (7, 0), (7, -1), 'RIGHT'),
    ]))
    story.append(pva_sum_tbl)
    story.append(Spacer(1, 4))
 
    if pva_rows:
        pva_hdr = ['Date', 'Day', 'Predicted (RM)', 'Actual (RM)', 'Variance', 'Var %', 'Status']
        pva_body = [pva_hdr]
        for r in pva_rows:
            has_act = r['actual'] is not None
            var     = r.get('variance')
            var_pct = r.get('variance_pct')
            status  = ''
            if has_act and r.get('in_range') is not None:
                status = 'On Target' if r['in_range'] else 'Off Range'
            holiday_flag = ' 🎉' if r.get('is_holiday') else ''
            pva_body.append([
                r['ds'],
                r['day_name'] + holiday_flag,
                num(r.get('yhat', 0)),
                num(r['actual']) if has_act else '—',
                (f"+{num(var)}" if var and var >= 0 else num(var)) if var is not None else '—',
                (f"+{var_pct}%" if var_pct and var_pct >= 0 else f"{var_pct}%") if var_pct is not None else '—',
                status,
            ])
 
        pva_tbl = Table(pva_body, colWidths=[22*mm, 16*mm, 30*mm, 28*mm, 28*mm, 18*mm, 22*mm])
        pva_style = _table_style()
        for i, r in enumerate(pva_rows, start=1):
            var = r.get('variance')
            if var is not None:
                col = C_TEAL if var >= 0 else C_RED
                pva_style.add('TEXTCOLOR', (4, i), (5, i), col)
        for c in [2, 3, 4, 5]:
            pva_style.add('ALIGN', (c, 0), (c, -1), 'RIGHT')
        pva_tbl.setStyle(pva_style)
        story.append(KeepTogether([pva_tbl]))
    else:
        story.append(P('No forecast data found for this period. Run the AI Forecast engine first.', 'RPT_BodySm'))
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 4 — PRODUCT PERFORMANCE
    # ─────────────────────────────────────────────────────────
    story.append(PageBreak())
    _section('4.  Product Performance', story, styles)
 
    top_prods = rd.get('top_products', [])
    bot_prods = rd.get('bottom_products', [])
 
    if top_prods:
        story.append(P('Top-Selling Products (by quantity)', 'RPT_Bold'))
        top_hdr = ['#', 'Product', 'Units Sold', 'Revenue (RM)', 'Share %', 'Bar']
        top_body = [top_hdr]
        for i, p in enumerate(top_prods, 1):
            top_body.append([
                str(i),
                p['product_name'],
                f"{int(p['qty']):,}",
                num(p['revenue']),
                f"{p['pct']}%",
                pct_bar(float(p['pct'] or 0)),
            ])
        top_tbl = Table(top_body, colWidths=[8*mm, 55*mm, 25*mm, 32*mm, 20*mm, 42*mm])
        ts = _table_style()
        ts.add('ALIGN', (0, 0), (0, -1), 'CENTER')
        for c in [2, 3, 4]:
            ts.add('ALIGN', (c, 0), (c, -1), 'RIGHT')
        ts.add('FONTNAME', (5, 1), (5, -1), 'Courier')
        ts.add('FONTSIZE', (5, 1), (5, -1), 7)
        ts.add('TEXTCOLOR', (5, 1), (5, -1), C_BLUE)
        top_tbl.setStyle(ts)
        story.append(KeepTogether([top_tbl]))
        story.append(Spacer(1, 6))
 
    if bot_prods:
        story.append(P('Underperforming Products — Bottom 3 (by quantity)', 'RPT_Bold'))
        bot_hdr = ['Product', 'Units Sold', 'Revenue (RM)']
        bot_body = [bot_hdr] + [
            [p['product_name'], f"{int(p['qty']):,}", num(p['revenue'])]
            for p in bot_prods
        ]
        bot_tbl = Table(bot_body, colWidths=[100*mm, 35*mm, 45*mm])
        bs = _table_style()
        bs.add('TEXTCOLOR', (0, 1), (0, -1), C_RED)
        for c in [1, 2]:
            bs.add('ALIGN', (c, 0), (c, -1), 'RIGHT')
        bot_tbl.setStyle(bs)
        story.append(KeepTogether([bot_tbl]))
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 5 — REGIONAL & CATEGORY BREAKDOWN
    # ─────────────────────────────────────────────────────────
    _section('5.  Regional & Category Breakdown', story, styles)
 
    reg_data = rd.get('regional_breakdown', [])
    cat_data = rd.get('category_breakdown', [])
    pay_data = rd.get('payment_breakdown', [])
 
    if reg_data:
        story.append(P('Branch Performance', 'RPT_Bold'))
        reg_hdr  = ['Branch', 'Revenue (RM)', 'Transactions', 'Bar']
        reg_body = [reg_hdr]
        br_max   = rd.get('branch_max_rev', 1) or 1
        for r in reg_data:
            is_top = r['branch_name'] == top_branch
            reg_body.append([
                ('★ ' if is_top else '') + r['branch_name'],
                num(r['rev']),
                f"{int(r['txns']):,}",
                pct_bar(r['rev'] / br_max * 100),
            ])
        reg_tbl = Table(reg_body, colWidths=[55*mm, 40*mm, 35*mm, 52*mm])
        rs = _table_style()
        rs.add('ALIGN', (1, 0), (2, -1), 'RIGHT')
        rs.add('FONTNAME', (3, 1), (3, -1), 'Courier')
        rs.add('FONTSIZE', (3, 1), (3, -1), 7)
        rs.add('TEXTCOLOR', (3, 1), (3, -1), C_BLUE)
        reg_tbl.setStyle(rs)
        story.append(KeepTogether([reg_tbl]))
        story.append(Spacer(1, 6))
 
    if cat_data:
        story.append(P('Revenue by Product Category', 'RPT_Bold'))
        cat_hdr  = ['Category', 'Revenue (RM)', 'Share %', 'Bar']
        cat_body = [cat_hdr] + [
            [c['product_category'], num(c['revenue']), f"{c['pct']}%", pct_bar(float(c['pct'] or 0))]
            for c in cat_data
        ]
        cat_tbl = Table(cat_body, colWidths=[60*mm, 40*mm, 22*mm, 60*mm])
        cs = _table_style()
        cs.add('ALIGN', (1, 0), (2, -1), 'RIGHT')
        cs.add('FONTNAME', (3, 1), (3, -1), 'Courier')
        cs.add('FONTSIZE', (3, 1), (3, -1), 7)
        cs.add('TEXTCOLOR', (3, 1), (3, -1), C_TEAL)
        cat_tbl.setStyle(cs)
        story.append(KeepTogether([cat_tbl]))
        story.append(Spacer(1, 6))
 
    if pay_data:
        story.append(P('Payment Method Breakdown', 'RPT_Bold'))
        pay_hdr  = ['Payment Method', 'Transactions', 'Share %', 'Bar']
        pay_body = [pay_hdr] + [
            [p['payment_method'], f"{int(p['txn_count']):,}", f"{p['pct']}%", pct_bar(float(p['pct'] or 0))]
            for p in pay_data
        ]
        pay_tbl = Table(pay_body, colWidths=[60*mm, 35*mm, 22*mm, 65*mm])
        ps = _table_style()
        ps.add('ALIGN', (1, 0), (2, -1), 'RIGHT')
        ps.add('FONTNAME', (3, 1), (3, -1), 'Courier')
        ps.add('FONTSIZE', (3, 1), (3, -1), 7)
        ps.add('TEXTCOLOR', (3, 1), (3, -1), colors.HexColor('#D97706'))
        pay_tbl.setStyle(ps)
        story.append(KeepTogether([pay_tbl]))
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 6 — TRANSACTION FLOW (Hourly + Day-of-Week)
    # ─────────────────────────────────────────────────────────
    _section('6.  Transaction Flow by Time', story, styles)
 
    hourly_data = rd.get('hourly_breakdown', [])
    dow_data    = rd.get('dow_breakdown', [])
    hourly_max  = max((h['txns'] for h in hourly_data), default=1) or 1
    dow_max     = max((d['txns'] for d in dow_data),    default=1) or 1
 
    if hourly_data:
        story.append(P('Hourly Transaction Volume', 'RPT_Bold'))
        hr_hdr  = ['Hour', 'Transactions', 'Revenue (RM)', 'Activity']
        hr_body = [hr_hdr]
        for h in hourly_data:
            is_peak = peak_hour.startswith(h['hour'][:2])
            hr_body.append([
                ('★ ' if is_peak else '') + h['hour'],
                f"{h['txns']:,}",
                num(h['revenue']),
                pct_bar(h['txns'] / hourly_max * 100),
            ])
        hr_tbl = Table(hr_body, colWidths=[28*mm, 30*mm, 36*mm, 88*mm])
        hs = _table_style()
        hs.add('ALIGN', (1, 0), (2, -1), 'RIGHT')
        hs.add('FONTNAME', (3, 1), (3, -1), 'Courier')
        hs.add('FONTSIZE', (3, 1), (3, -1), 7)
        hs.add('TEXTCOLOR', (3, 1), (3, -1), C_BLUE)
        hr_tbl.setStyle(hs)
        story.append(KeepTogether([hr_tbl]))
        story.append(Spacer(1, 6))
 
    if dow_data:
        story.append(P('Day-of-Week Revenue Pattern', 'RPT_Bold'))
        dow_hdr  = ['Day', 'Transactions', 'Revenue (RM)', 'Activity']
        dow_body = [dow_hdr]
        for d in dow_data:
            is_peak = (d['day'] == peak_day)
            dow_body.append([
                ('★ ' if is_peak else '') + d['day'],
                f"{d['txns']:,}",
                num(d['revenue']),
                pct_bar(d['txns'] / dow_max * 100),
            ])
        dow_tbl = Table(dow_body, colWidths=[30*mm, 30*mm, 36*mm, 86*mm])
        ds = _table_style()
        ds.add('ALIGN', (1, 0), (2, -1), 'RIGHT')
        ds.add('FONTNAME', (3, 1), (3, -1), 'Courier')
        ds.add('FONTSIZE', (3, 1), (3, -1), 7)
        ds.add('TEXTCOLOR', (3, 1), (3, -1), C_TEAL)
        dow_tbl.setStyle(ds)
        story.append(KeepTogether([dow_tbl]))
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 7 — AI RECOMMENDATIONS
    # ─────────────────────────────────────────────────────────
    story.append(PageBreak())
    _section('7.  AI-Powered Actionable Recommendations', story, styles)
 
    ai_text = rd.get('ai_insight', 'AI insight temporarily unavailable.')
 
    story.append(P(f'<b>Branch:</b> {branch_label}  |  <b>Period:</b> {month_label}  |  <b>Revenue:</b> RM {num(period_rev)}', 'RPT_Body'))
    story.append(Spacer(1, 6))
 
    # Parse AI text into lines and render cleanly
    for line in ai_text.replace('\r\n', '\n').split('\n'):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 3))
            continue
        # Numbered action items → bullet style
        if line and line[0].isdigit() and len(line) > 2 and line[1] in '.):':
            story.append(Paragraph(f'• {line}', styles['RPT_Bullet']))
        elif line.startswith('•') or line.startswith('-'):
            story.append(Paragraph(line, styles['RPT_Bullet']))
        else:
            story.append(Paragraph(line, styles['RPT_AI']))
 
    story.append(Spacer(1, 8))
 
    # Key findings summary table
    top_prods_list = rd.get('top_products', [])
    findings = [
        ['Finding', 'Detail'],
        ['Revenue Trend',      trend_label],
        ['Top Branch',         top_branch],
        ['Avg Order Value',    f'RM {num(aov)}'],
        ['Busiest Window',     f'{peak_hour}  ·  {peak_day}'],
        ['Best Product',       f"{top_prods_list[0]['product_name']} ({int(top_prods_list[0]['qty']):,} units)" if top_prods_list else 'N/A'],
        ['Period Revenue',     f'RM {num(period_rev)} across {int(period_txns):,} transactions'],
    ]
    fin_tbl = Table(findings, colWidths=[55*mm, 127*mm])
    fin_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), C_HDRROW),
        ('TEXTCOLOR',     (0, 0), (-1, 0), C_WHITE),
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME',      (0, 1), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME',      (1, 1), (1, -1), 'Helvetica'),
        ('FONTSIZE',      (0, 0), (-1, -1), 8.5),
        ('TEXTCOLOR',     (0, 1), (0, -1), C_DARK),
        ('TEXTCOLOR',     (1, 1), (1, -1), C_MID),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
        ('GRID',          (0, 0), (-1, -1), 0.4, C_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(KeepTogether([fin_tbl]))
 
    # ── Build & stream PDF ─────────────────────────────────────
    on_page = _make_on_page(branch_label, month_label, now_str)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
 
    pdf_bytes = buf.getvalue()
    response  = make_response(pdf_bytes)
    response.headers['Content-Type']        = 'application/pdf'
    filename_str = f"MCS_Executive_Report_{branch_filter.replace(' ','_')}_{month_key}.pdf"
    response.headers['Content-Disposition'] = f'attachment; filename="{filename_str}"'
    return response


# ============================================================
#    RUNTIME ENGINE EXECUTION ENTRYPOINT
# ============================================================
if __name__ == '__main__':
    app.run(debug=True)