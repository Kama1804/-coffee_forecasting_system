from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify, make_response, Response
import os
import sqlite3
import time
import io
import json
from functools import wraps
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Core Module Dependencies
from init_db import initialize_database
from etl_pipeline import ETLPipeline
from gemini_agent import get_ai_insight, build_chat_system_context, stream_ai_insight, build_slim_context, fast_kpi_bypass
from forecast_engine import ForecastEngine, MY_PUBLIC_HOLIDAYS, MY_SEASONS
from analytics import (get_dashboard_metrics, calculate_ingredient_demand, 
                       revenue_decline_and_product_mix_profiler, weather_payday_cross_tabulation, SKU_MAPPING)
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

# Helper tool to convert database SKU codes back to friendly menu names for frontend charts
REVERSE_SKU_LOOKUP = {v: k.title() for k, v in SKU_MAPPING.items()}

# ============================================================
#    HELPERS
# ============================================================
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def build_where(branch_filter, time_filter, max_date, temp_filter='all', alias='s'):
    conds, params = [], []
    if branch_filter != 'all':
        conds.append(f"{alias}.store_location = ?")
        params.append(branch_filter)
    if time_filter == 'current_week':
        conds.append(f"{alias}.transaction_date >= date(?, '-7 days')")
        params.append(max_date)
    elif time_filter.startswith('year_'):
        conds.append(f"strftime('%Y', {alias}.transaction_date) = ?")
        params.append(time_filter.split('_')[1])
    elif time_filter.startswith('month_'):
        conds.append(f"strftime('%Y-%m', {alias}.transaction_date) = ?")
        params.append(time_filter.split('_')[1])
    
    if temp_filter == 'ICED':
        conds.append(f"{alias}.product_detail LIKE ?")
        params.append("%ICED%")
    elif temp_filter == 'HOT':
        conds.append(f"{alias}.product_detail NOT LIKE ?")
        params.append("%ICED%")

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
        cursor.execute("SELECT MIN(transaction_date) as first, MAX(transaction_date) as last FROM sales_transaction")
        dates = cursor.fetchone()
        cursor.execute("SELECT COUNT(DISTINCT product_category) as cats FROM sales_transaction")
        cats  = cursor.fetchone()['cats']
        
        cursor.execute("""
            SELECT store_location, COUNT(*) as cnt
            FROM sales_transaction
            GROUP BY store_location
        """)
        branches = cursor.fetchall()
        conn.close()
        profile = {
            'total_records': total,
            'date_from':     dates['first'] or 'N/A',
            'date_to':       dates['last']  or 'N/A',
            'categories':    cats,
            'branches':      [{'name': r['store_location'], 'count': r['cnt']} for r in branches]
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
            "SELECT DISTINCT strftime('%Y', transaction_date) as yr "
            "FROM sales_transaction ORDER BY yr DESC"
        )
        years = [r['yr'] for r in cursor.fetchall() if r['yr']]
        cursor.execute(
            "SELECT DISTINCT strftime('%Y-%m', transaction_date) as mo "
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
    temp_filter   = request.args.get('temp',   'all')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(transaction_date) as max_d FROM sales_transaction")
        max_date = cursor.fetchone()['max_d'] or datetime.today().strftime('%Y-%m-%d')

        where, params = build_where(branch_filter, time_filter, max_date, temp_filter)

        cursor.execute(f"""
            SELECT COALESCE(SUM(s.Total_Bill_MYR), 0) as rev,
                   COALESCE(SUM(s.transaction_qty), 0) as vol,
                   COUNT(s.transaction_id)           as txns
            FROM sales_transaction s {where}
        """, params)
        metrics = cursor.fetchone()

        cursor.execute(f"""
            SELECT COUNT(DISTINCT s.transaction_date) as active_days
            FROM sales_transaction s {where}
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
            SELECT store_location, SUM(Total_Bill_MYR) as rev
            FROM sales_transaction
            WHERE strftime('%Y-%m', transaction_date) = ?
            GROUP BY store_location ORDER BY rev DESC LIMIT 1
        """, [prev_mo])
        tb         = cursor.fetchone()
        top_branch = tb['store_location'] if tb else 'N/A'

        try:
            prev_dt    = datetime.strptime(prev_mo + '-01', '%Y-%m-%d')
            prev_label = prev_dt.strftime('%B %Y')
        except Exception:
            prev_label = prev_mo

        cursor.execute("""
            SELECT COALESCE(SUM(Total_Bill_MYR), 0) as curr
            FROM sales_transaction
            WHERE strftime('%Y-%m', transaction_date) = strftime('%Y-%m', ?)
        """, [max_date])
        curr_mo_rev = cursor.fetchone()['curr']

        cursor.execute("""
            SELECT COALESCE(SUM(Total_Bill_MYR), 0) as prev
            FROM sales_transaction
            WHERE strftime('%Y-%m', transaction_date) = ?
        """, [prev_mo])
        prev_mo_rev = cursor.fetchone()['prev']

        if prev_mo_rev > 0:
            pct_change  = ((curr_mo_rev - prev_mo_rev) / prev_mo_rev) * 100
            trend_label = f"+{pct_change:.1f}% vs {prev_label}" if pct_change >= 0 else f"{pct_change:.1f}% vs {prev_label}"
        else:
            trend_label = f"vs {prev_label}"

        cursor.execute(f"""
            SELECT s.transaction_date, SUM(s.Total_Bill_MYR) as rev
            FROM sales_transaction s {where}
            GROUP BY s.transaction_date
            ORDER BY s.transaction_date DESC LIMIT 7
        """, params)
        spark_rows      = cursor.fetchall()
        sparkline_dates = [r['transaction_date'] for r in reversed(spark_rows)]
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
#    API — CHARTS DATA (REFACTORED LABEL TRANSLATIONS)
# ============================================================
@app.route('/api/charts')
@login_required
def api_charts():
    branch_filter = request.args.get('branch', 'all')
    time_filter   = request.args.get('time',   'all')
    temp_filter   = request.args.get('temp',   'all')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(transaction_date) as max_d FROM sales_transaction")
        max_date = cursor.fetchone()['max_d'] or datetime.today().strftime('%Y-%m-%d')

        where, params = build_where(branch_filter, time_filter, max_date, temp_filter)

        use_monthly = (time_filter == 'all' or time_filter.startswith('year_'))
        date_col    = "strftime('%Y-%m', s.transaction_date)" if use_monthly else "s.transaction_date"

        cursor.execute(f"""
            SELECT {date_col} as period, SUM(s.Total_Bill_MYR) as rev
            FROM sales_transaction s {where}
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
            SELECT s.product_category as cat, SUM(s.Total_Bill_MYR) as rev
            FROM sales_transaction s {where}
            GROUP BY cat ORDER BY rev DESC
        """, params)
        cat_rows = cursor.fetchall()

        # 🟢 CHANGED: Clean SKU-to-Menu-Name resolution mapping loop to keep charts clear
        cursor.execute(f"""
            SELECT s.product_id, SUM(s.transaction_qty) as qty
            FROM sales_transaction s {where}
            GROUP BY s.product_id ORDER BY qty DESC LIMIT 5
        """, params)
        top_prods = cursor.fetchall()
        top_prod_labels = [REVERSE_SKU_LOOKUP.get(r['product_id'], r['product_id']) for r in top_prods]

        cursor.execute(f"""
            SELECT s.product_id, SUM(s.transaction_qty) as qty
            FROM sales_transaction s {where}
            GROUP BY s.product_id ORDER BY qty ASC LIMIT 3
        """, params)
        weak_prods = cursor.fetchall()
        weak_prod_labels = [REVERSE_SKU_LOOKUP.get(r['product_id'], r['product_id']) for r in weak_prods]

        cursor.execute(f"""
            SELECT s.payment_method, COUNT(*) as cnt
            FROM sales_transaction s {where}
            GROUP BY s.payment_method ORDER BY cnt DESC
        """, params)
        pay_rows = cursor.fetchall()

        cursor.execute(f"""
            SELECT
                CASE strftime('%w', s.transaction_date)
                    WHEN '0' THEN 'Sun' WHEN '1' THEN 'Mon'
                    WHEN '2' THEN 'Tue' WHEN '3' THEN 'Wed'
                    WHEN '4' THEN 'Thu' WHEN '5' THEN 'Fri'
                    WHEN '6' THEN 'Sat'
                END as d_name,
                strftime('%w', s.transaction_date) as d_num,
                CAST(s.Hour AS INTEGER) as hr,
                COUNT(*) as txn_count
            FROM sales_transaction s {where}
            GROUP BY d_num, hr ORDER BY d_num ASC, hr ASC
        """, params)
        heat_rows = cursor.fetchall()

        cursor.execute("""
            SELECT DISTINCT store_location FROM sales_transaction ORDER BY store_location
        """)
        all_branches = [r['store_location'] for r in cursor.fetchall() if r['store_location']]

        cursor.execute(f"""
            SELECT DISTINCT strftime('%Y-%m', s.transaction_date) as period
            FROM sales_transaction s {where}
            ORDER BY period ASC
        """, params)
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

        cursor.execute(f"""
            SELECT strftime('%Y-%m', s.transaction_date) as period,
                   s.store_location,
                   SUM(s.Total_Bill_MYR) as rev
            FROM sales_transaction s {where}
            GROUP BY period, s.store_location
            ORDER BY period ASC
        """, params)
        for r in cursor.fetchall():
            if r['period'] in period_idx_map and r['store_location'] in monthly_by_branch:
                monthly_by_branch[r['store_location']][period_idx_map[r['period']]] = round(r['rev'], 2)

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
                "labels": top_prod_labels,
                "data":   [r['qty'] for r in top_prods]
            },
            "top_products": {
                "labels": top_prod_labels,
                "data":   [r['qty'] for r in top_prods]
            },
            "weak_products": {
                "labels": weak_prod_labels,
                "data":   [r['qty'] for r in weak_prods]
            },
            "payment": {
                "labels": [str(r['payment_method']).upper() for r in pay_rows],
                "data":   [r['cnt'] for r in pay_rows]
            },
            "heatmap": [
                {'day': r['d_name'], 'hour': r['hr'], 'value': r['txn_count']}
                for r in heat_rows
            ]
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — DIAGNOSTICS DATA
# ============================================================
@app.route('/api/diagnostics')
@login_required
def api_diagnostics():
    branch_filter = request.args.get('branch', 'all')
    time_filter   = request.args.get('time',   'all')
    temp_filter   = request.args.get('temp',   'all')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(transaction_date) as max_d FROM sales_transaction")
        max_date = cursor.fetchone()['max_d'] or datetime.today().strftime('%Y-%m-%d')
        conn.close()

        where, params = build_where(branch_filter, time_filter, max_date, temp_filter)
        
        cross_tab = weather_payday_cross_tabulation(where, params)
        return jsonify({
            "status": "success",
            "payday": cross_tab['payday_spend_analysis'],
            "weather": cross_tab['weather_temperature_impact']
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
    start_all = time.time()
    current_time = time.time()

    if GLOBAL_CHAT_CACHE["payload_dict"] and current_time < GLOBAL_CHAT_CACHE["expiry_timestamp"]:
        return GLOBAL_CHAT_CACHE["payload_dict"]

    conn   = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COALESCE(SUM(Total_Bill_MYR),0), COUNT(*) FROM sales_transaction")
    row1 = cursor.fetchone()
    total_rev = row1[0]
    total_txns = row1[1]

    cursor.execute("SELECT MIN(transaction_date), MAX(transaction_date) FROM sales_transaction")
    date_row   = cursor.fetchone()
    date_range = f"{date_row[0]} to {date_row[1]}" if date_row[0] else "N/A"
    max_date   = date_row[1] or datetime.today().strftime('%Y-%m-%d')

    cursor.execute("SELECT COUNT(DISTINCT transaction_date) FROM sales_transaction")
    days_active = cursor.fetchone()[0] or 1
    daily_avg   = total_rev / days_active

    cursor.execute("""
        SELECT store_location, CAST(Hour AS INTEGER) as hr, COUNT(*) as cnt
        FROM sales_transaction GROUP BY store_location, hr ORDER BY store_location, cnt DESC
    """)
    peak_rows = cursor.fetchall()
    branch_peaks = {}
    for r in peak_rows:
        bid = r['store_location']
        if bid not in branch_peaks: branch_peaks[bid] = []
        if len(branch_peaks[bid]) < 3:
            branch_peaks[bid].append(f"{r['hr']:02d}:00 ({r['cnt']} txns)")

    cursor.execute("""
        SELECT store_location, strftime('%Y-%m', transaction_date) as month, SUM(Total_Bill_MYR) as rev
        FROM sales_transaction
        WHERE transaction_date >= date(?, '-6 months')
        GROUP BY store_location, month ORDER BY month ASC
    """, [max_date])
    trend_rows = cursor.fetchall()
    
    cursor.execute("SELECT product_category, SUM(Total_Bill_MYR) as rev FROM sales_transaction GROUP BY product_category ORDER BY rev DESC")
    cat_summary = "\n".join([f"  - {r['product_category']}: RM {r['rev']:,.2f}" for r in cursor.fetchall()])

    conn.close()
    print(f"[PERF] Super-Fetcher took {time.time() - start_all:.4f}s")

    compiled_payload = {
        'date_range': date_range,
        'total_rev': total_rev,
        'total_txns': total_txns,
        'daily_avg': daily_avg,
        'max_date': max_date,
        'branch_peaks': branch_peaks,
        'trend_rows': [dict(r) for r in trend_rows],
        'categories': cat_summary,
    }
    
    compiled_payload['monthly_trend_summary'] = "\n".join([f"{r['store_location']} {r['month']}: RM {r['rev']:,.2f}" for r in compiled_payload['trend_rows']])

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
        bypass_response = fast_kpi_bypass(user_message, db_data)
        if bypass_response:
            history_cache = session['chat_history']
            history_cache.append({"user": user_message, "bot": bypass_response})
            if len(history_cache) > 4:
                history_cache.pop(0)
            session['chat_history'] = history_cache
            session.modified = True
            return jsonify({"status": "success", "response": bypass_response})
            
        system_context = build_slim_context(db_data, user_message)
    except Exception as e:
        print(f"[CHAT ERROR] Database context failure: {e}")
        system_context = "You are the AI Business Advisor for 'Mini Coffee Shop'. Database connection is temporarily unavailable."

    history_blocks = []
    for turn in session['chat_history'][-3:]:
        history_blocks.append(f"User: {turn['user']}\nAI: {turn['bot']}")

    history_text = "\n\n".join(history_blocks)

    final_prompt = f"""{system_context}

=== CONVERSATION HISTORY (last {len(history_blocks)} turns) ===
{history_text if history_text else "(No prior conversation)"}

=== INCOMING MESSAGE ===
User: {user_message}
AI:"""

    success, ai_response = get_ai_insight(final_prompt)

    if success:
        history_cache = session['chat_history']
        history_cache.append({"user": user_message, "bot": ai_response})
        if len(history_cache) > 4:
            history_cache.pop(0)
        session['chat_history'] = history_cache
        session.modified = True
        return jsonify({"status": "success", "response": ai_response})
    else:
        return jsonify({"status": "error", "response": ai_response}), 503


@app.route('/api/chat/stream', methods=['POST'])
@login_required
def api_chat_stream():
    data = request.get_json()
    if not data or not data.get('message', '').strip():
        return jsonify({"status": "error", "message": "Empty message."}), 400

    user_message = data['message'].strip()
    if 'chat_history' not in session:
        session['chat_history'] = []

    try:
        db_data = _fetch_db_context()
        bypass_response = fast_kpi_bypass(user_message, db_data)
        
        if bypass_response:
            session['_pending_user_msg'] = user_message
            session.modified = True
            
            def bypass_stream():
                try:
                    yield f"data: {json.dumps({'chunk': bypass_response})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                except GeneratorExit:
                    print("[BYPASS LOG] Stream killed early by user client drop.")
                
            return Response(
                bypass_stream(),
                mimetype='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                }
            )

        system_context = build_slim_context(db_data, user_message)
    except Exception:
        system_context = "You are the AI Business Advisor for 'Mini Coffee Shop'. Database temporarily unavailable."

    history_blocks = []
    for turn in session['chat_history'][-3:]:
        history_blocks.append(f"User: {turn['user']}\nAI: {turn['bot']}")

    final_prompt = f"""{system_context}

=== CONVERSATION HISTORY ===
{chr(10).join(history_blocks) if history_blocks else "(No prior conversation)"}

=== INCOMING MESSAGE ===
User: {user_message}
AI:"""

    session['_pending_user_msg'] = user_message
    session.modified = True

    return Response(
        stream_ai_insight(final_prompt),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/api/chat/save', methods=['POST'])
@login_required
def api_chat_save():
    data = request.get_json()
    user_msg = session.pop('_pending_user_msg', data.get('user', ''))
    bot_msg  = data.get('bot', '')

    if user_msg and bot_msg:
        history = session.get('chat_history', [])
        history.append({'user': user_msg, 'bot': bot_msg})
        if len(history) > 4:
            history.pop(0)
        session['chat_history'] = history
        session.modified = True

    return jsonify({"status": "success"})


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
    branch_id   = request.args.get('branch_id',   'STB-PJ1',    type=str)
    branch_name = request.args.get('branch_name', 'Putrajaya', type=str)
    try:
        cache_key = f"{branch_id}_{branch_name}"
        if cache_key in FORECAST_CACHE:
            result = FORECAST_CACHE[cache_key]
        else:
            engine          = ForecastEngine()
            success, result = engine.generate_7_day_forecast(branch_id, branch_name)
            if not success:
                return jsonify({"status": "error", "message": result}), 500
            
            FORECAST_CACHE[cache_key] = result

        return jsonify({
            "status":               "success",
            "mape":                result['mape'],
            "rmse":                result['rmse'],
            "accuracy":            result.get('accuracy', 0),
            "persona":             result.get('persona', ''),
            "historical":          result['historical'],
            "forecast":            result['forecast'],
            "hourly":              result.get('hourly', []),
            "forecast_vs_actual": result.get('forecast_vs_actual', []),
            "insample_fit":        result.get('insample_fit', []),
            "weather_by_time":     result.get('weather_by_time', []),
            "ingredient_demand":   result.get('ingredient_demand', {})
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    
# ============================================================
#    API — AVAILABLE REPORT MONTHS
# ============================================================
@app.route('/api/report_months')
@login_required
def api_report_months():
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT strftime('%Y-%m', transaction_date) as mo "
            "FROM sales_transaction ORDER BY mo ASC"
        )
        months = [r['mo'] for r in cursor.fetchall() if r['mo']]
        conn.close()
        return jsonify({"status": "success", "months": months})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — REPORT DATA (REFACTORED RELATIONAL STRIPPING)
# ============================================================
def _build_report_data(branch_filter, date_from, date_to):
    """
    Builds the full Executive Report data payload from the denormalized warehouse columns.
    """
    conn   = get_db_connection()
    cursor = conn.cursor()

    branch_cond  = "s.store_location = ?" if branch_filter != 'all' else None
    date_cond    = "s.transaction_date BETWEEN ? AND ?" if date_from and date_to else None
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
        SELECT s.transaction_date as date,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue,
               COUNT(*) as txns
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.transaction_date
        ORDER BY s.transaction_date ASC
    """, base_params())
    daily_data     = [dict(r) for r in cursor.fetchall()]
    period_revenue = sum(r['revenue'] for r in daily_data)
    period_txns    = sum(r['txns']    for r in daily_data)

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

            prev_cond   = ("s.store_location = ? AND " if branch_filter != 'all' else "") + \
                          "s.transaction_date BETWEEN ? AND ?"
            prev_params = ([branch_filter] if branch_filter != 'all' else []) + [prev_from, prev_to]

            cursor.execute(f"""
                SELECT COALESCE(SUM(s.Total_Bill_MYR), 0) as prev_rev
                FROM sales_transaction s
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
        SELECT CAST(s.Hour AS INTEGER) as hr, COUNT(*) as cnt
        FROM sales_transaction s
        {where_clause}
        GROUP BY hr ORDER BY cnt DESC LIMIT 1
    """, base_params())
    ph        = cursor.fetchone()
    peak_hour = f"{int(ph['hr']):02d}:00 – {int(ph['hr'])+1:02d}:00" \
                if ph and ph['hr'] is not None else "N/A"

    # ── Hourly breakdown ───────────────────────────────────────
    cursor.execute(f"""
        SELECT CAST(s.Hour AS INTEGER) as hr,
               COUNT(*) as txn_count,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue
        FROM sales_transaction s
        {where_clause}
        GROUP BY hr ORDER BY hr ASC
    """, base_params())
    hourly_breakdown = [
        {'hour': f"{r['hr']:02d}:00", 'txns': r['txn_count'], 'revenue': r['revenue']}
        for r in cursor.fetchall() if r['hr'] is not None
    ]

    # ── Peak day ───────────────────────────────────────────────
    cursor.execute(f"""
        SELECT CASE strftime('%w', s.transaction_date)
            WHEN '0' THEN 'Sunday'   WHEN '1' THEN 'Monday'
            WHEN '2' THEN 'Tuesday'  WHEN '3' THEN 'Wednesday'
            WHEN '4' THEN 'Thursday' WHEN '5' THEN 'Friday'
            WHEN '6' THEN 'Saturday'
        END as day_name, COUNT(*) as cnt
        FROM sales_transaction s
        {where_clause}
        GROUP BY day_name ORDER BY cnt DESC LIMIT 1
    """, base_params())
    pd_row   = cursor.fetchone()
    peak_day = pd_row['day_name'] if pd_row else "N/A"

    # ── Day-of-week breakdown ──────────────────────────────────
    cursor.execute(f"""
        SELECT
            CASE strftime('%w', s.transaction_date)
                WHEN '0' THEN 'Sunday'   WHEN '1' THEN 'Monday'
                WHEN '2' THEN 'Tuesday'  WHEN '3' THEN 'Wednesday'
                WHEN '4' THEN 'Thursday' WHEN '5' THEN 'Friday'
                WHEN '6' THEN 'Saturday'
            END as day_name,
            strftime('%w', s.transaction_date) as day_num,
            COUNT(*) as txn_count,
            ROUND(SUM(s.Total_Bill_MYR), 2) as revenue
        FROM sales_transaction s
        {where_clause}
        GROUP BY day_num ORDER BY day_num ASC
    """, base_params())
    dow_breakdown = [
        {'day': r['day_name'], 'txns': r['txn_count'], 'revenue': r['revenue']}
        for r in cursor.fetchall()
    ]

    # ── Top branch ─────────────────────────────────────────────
    cursor.execute(f"""
        SELECT s.store_location, SUM(s.Total_Bill_MYR) as rev
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.store_location ORDER BY rev DESC LIMIT 1
    """, base_params())
    tb_row     = cursor.fetchone()
    top_branch = tb_row['store_location'] if tb_row else 'N/A'

    # ── Regional breakdown ─────────────────────────────────────
    cursor.execute(f"""
        SELECT s.store_location as branch_name,
               ROUND(SUM(s.Total_Bill_MYR), 2) as rev,
               COUNT(*) as txns
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.store_location ORDER BY rev DESC
    """, base_params())
    regional_breakdown = [dict(r) for r in cursor.fetchall()]
    branch_max_rev = max((r['rev'] for r in regional_breakdown), default=1)

    # ── Product performance ────────────────────────────────────
    # 🟢 CHANGED: Rewritten to directly translate SKU IDs to menu string properties cleanly
    cursor.execute(f"""
        SELECT s.product_id as product_id,
               SUM(s.transaction_qty) as qty,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue,
               ROUND(SUM(s.Total_Bill_MYR) * 100.0 /
                     NULLIF((SELECT SUM(s2.Total_Bill_MYR)
                             FROM sales_transaction s2
                             {where_clause}), 0), 1) as pct
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.product_id ORDER BY qty DESC LIMIT 5
    """, base_params() + base_params())
    top_products_raw = [dict(r) for r in cursor.fetchall()]
    
    top_products = []
    for p in top_products_raw:
        p['product_id'] = REVERSE_SKU_LOOKUP.get(p['product_id'], p['product_id'])
        top_products.append(p)

    cursor.execute(f"""
        SELECT s.product_id as product_id,
               SUM(s.transaction_qty) as qty,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.product_id ORDER BY qty ASC LIMIT 3
    """, base_params())
    bottom_products_raw = [dict(r) for r in cursor.fetchall()]
    
    bottom_products = []
    for p in bottom_products_raw:
        p['product_id'] = REVERSE_SKU_LOOKUP.get(p['product_id'], p['product_id'])
        bottom_products.append(p)

    # ── Category breakdown ─────────────────────────────────────
    cursor.execute(f"""
        SELECT s.product_category,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue,
               ROUND(SUM(s.Total_Bill_MYR) * 100.0 /
                     NULLIF((SELECT SUM(s2.Total_Bill_MYR)
                             FROM sales_transaction s2
                             {where_clause}), 0), 1) as pct
        FROM sales_transaction s
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
                             {where_clause}), 0), 1) as pct
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.payment_method ORDER BY txn_count DESC
    """, base_params() + base_params())
    payment_breakdown = [dict(r) for r in cursor.fetchall()]

    # ── Monthly trend — ALL months ─────────────────────────────
    cursor.execute("SELECT DISTINCT store_location FROM sales_transaction ORDER BY store_location")
    all_branches = [r[0] for r in cursor.fetchall() if r[0]]

    cursor.execute("""
        SELECT DISTINCT strftime('%Y-%m', transaction_date) as month
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
        SELECT strftime('%Y-%m', transaction_date) as month, store_location,
               SUM(Total_Bill_MYR) as rev
        FROM sales_transaction
        GROUP BY month, store_location
    """)
    month_idx = {m: i for i, m in enumerate(all_month_keys)}
    for r in cursor.fetchall():
        if r['month'] in month_idx and r['store_location'] in by_branch_monthly:
            by_branch_monthly[r['store_location']][month_idx[r['month']]] = round(r['rev'], 2)

    monthly_trend = {
        "labels":    all_month_labels,
        "keys":      all_month_keys,
        "branches":  all_branches,
        "by_branch": by_branch_monthly
    }

    # ── Forecast data (upcoming 7-day) ────────────────────────
    fc_conds  = ["s.store_location = ?"] if branch_filter != 'all' else []
    fc_conds.append("f.forecast_date > (SELECT COALESCE(MAX(transaction_date), '1970-01-01') FROM sales_transaction)")
    fc_where  = "WHERE " + " AND ".join(fc_conds)
    fc_params = [branch_filter] if branch_filter != 'all' else []

    try:
        cursor.execute(f"""
            SELECT f.forecast_date as ds,
                   f.predicted_revenue as yhat,
                   f.lower_bound_revenue as yhat_lower,
                   f.upper_bound_revenue as yhat_upper
            FROM sales_forecast f
            JOIN sales_transaction s ON f.branch_id = s.branch_id
            {fc_where} GROUP BY f.forecast_date ORDER BY f.forecast_date ASC LIMIT 7
        """, fc_params)
        forecast_data = [dict(r) for r in cursor.fetchall()]
    except Exception:
        forecast_data = []

    holiday_set = {h[0] for h in MY_PUBLIC_HOLIDAYS}
    for row in forecast_data:
        dt_obj = datetime.strptime(row['ds'], '%Y-%m-%d')
        row['is_holiday'] = row['ds'] in holiday_set
        row['is_friday']  = (dt_obj.weekday() == 4)

    # ── Predicted vs Actual ──
    pva_fc_conds  = ["f.forecast_date BETWEEN ? AND ?"]
    pva_fc_params = [date_from, date_to]
    pva_act_conds  = ["s.transaction_date BETWEEN ? AND ?"]
    pva_act_params = [date_from, date_to]

    if branch_filter != 'all':
        b_id = 'STB-PJ1' if branch_filter == 'Putrajaya' else 'FT-PA1'
        pva_fc_conds.append("f.branch_id = ?")
        pva_fc_params.append(b_id)
        pva_act_conds.append("s.store_location = ?")
        pva_act_params.append(branch_filter)

    pva_fc_where  = "WHERE " + " AND ".join(pva_fc_conds)
    pva_act_where = "WHERE " + " AND ".join(pva_act_conds)

    try:
        cursor.execute(f"""
            SELECT f.forecast_date                                  AS ds,
                   COALESCE(SUM(f.predicted_revenue), 0)   AS yhat,
                   COALESCE(SUM(f.lower_bound_revenue), 0) AS yhat_lower,
                   COALESCE(SUM(f.upper_bound_revenue), 0) AS yhat_upper
            FROM sales_forecast f
            {pva_fc_where}
            GROUP BY f.forecast_date
            ORDER BY f.forecast_date ASC
        """, pva_fc_params)
        pva_forecast_rows = {r['ds']: dict(r) for r in cursor.fetchall()}
    except Exception:
        pva_forecast_rows = {}

    try:
        cursor.execute(f"""
            SELECT s.transaction_date                         AS ds,
                   ROUND(SUM(s.Total_Bill_MYR), 2)            AS actual_revenue,
                   COUNT(*)                                    AS txn_count
            FROM sales_transaction s
            {pva_act_where}
            GROUP BY s.transaction_date
            ORDER BY s.transaction_date ASC
        """, pva_act_params)
        pva_actual_rows = {r['ds']: dict(r) for r in cursor.fetchall()}
    except Exception:
        pva_actual_rows = {}

    all_pva_dates = sorted(set(list(pva_forecast_rows.keys()) + list(pva_actual_rows.keys())))
    predicted_vs_actual = []
    for ds in all_pva_dates:
        fc_row  = pva_forecast_rows.get(ds, {})
        act_row = pva_actual_rows.get(ds, {})
        yhat       = fc_row.get('yhat', 0) or 0
        actual_rev = act_row.get('actual_revenue', None)
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

    pva_with_actual     = [r for r in predicted_vs_actual if r['actual'] is not None]
    pva_total_predicted = round(sum(r['yhat']   for r in pva_with_actual), 2)
    pva_total_actual    = round(sum(r['actual'] for r in pva_with_actual), 2)
    pva_mape = 0.0
    if pva_with_actual:
        mape_vals = [abs(r['variance_pct']) for r in pva_with_actual if r['variance_pct'] is not None]
        pva_mape  = round(sum(mape_vals) / len(mape_vals), 1) if mape_vals else 0.0
    pva_within_range = sum(1 for r in pva_with_actual if r.get('in_range'))

    try:
        branch_id = None
        if branch_filter == 'Putrajaya': branch_id = 'STB-PJ1'
        if branch_filter == 'Puncak Alam': branch_id = 'FT-PA1'
        diagnostics = revenue_decline_and_product_mix_profiler(branch_id)
    except Exception:
        diagnostics = {}

    conn.close()

    # AI Prompt Formatting Context
    top_prod_ctx    = ", ".join([f"{p['product_id']} ({p['qty']} units)" for p in top_products]) or "N/A"
    bottom_prod_ctx = ", ".join([p['product_id'] for p in bottom_products]) or "N/A"
    cat_ctx         = ", ".join([f"{c['product_category']}: RM {c['revenue']:,.2f}" for c in category_breakdown]) or "N/A"
    regional_ctx    = ", ".join([f"{r['branch_name']}: RM {r['rev']:,.2f}" for r in regional_breakdown]) or "N/A"

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
        "diagnostics":          diagnostics,
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
        cache_key = f"{branch_filter}_{date_from}_{date_to}"
        if cache_key in REPORT_CACHE:
            return jsonify(REPORT_CACHE[cache_key])

        result = _build_report_data(branch_filter, date_from, date_to)
        REPORT_CACHE[cache_key] = result
        return jsonify(result)
    except Exception as e:
        print("Report API Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#    API — PDF EXPORT (Forecast Report — Full Data, Plain English)
# ============================================================
@app.route('/api/export-forecast-pdf', methods=['GET', 'POST'])
@login_required
def api_export_forecast_pdf():
    # ── Accept both GET (legacy) and POST (new full-data path) ──────────
    if request.method == 'POST':
        body        = request.get_json() or {}
        branch_id   = body.get('branch_id',   'STB-PJ1')
        branch_name = body.get('branch_name', 'Putrajaya')
        month_filter = body.get('month_filter', None)
        result      = body.get('forecast_data', None)    # full frontend payload
    else:
        branch_id    = request.args.get('branch_id',   'STB-PJ1', type=str)
        branch_name  = request.args.get('branch_name', 'Putrajaya', type=str)
        month_filter = request.args.get('month', None,  type=str)
        result       = None

    # ── If no data was posted, regenerate from the engine ───────────────
    if result is None:
        try:
            cache_key = f"{branch_id}_{branch_name}"
            if cache_key in FORECAST_CACHE:
                result = FORECAST_CACHE[cache_key]
            else:
                engine          = ForecastEngine()
                success, result = engine.generate_7_day_forecast(branch_id, branch_name)
                if not success:
                    return jsonify({"status": "error", "message": result}), 500
                FORECAST_CACHE[cache_key] = result
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    try:
        now_str  = datetime.now().strftime('%d %b %Y, %I:%M %p')
        forecast = result.get('forecast', [])
        fva_all  = result.get('forecast_vs_actual', [])
        hourly   = result.get('hourly', [])
        wbt      = result.get('weather_by_time', [])
        ingr     = result.get('ingredient_demand', {})
        mape     = result.get('mape', 0)
        rmse     = result.get('rmse', 0)
        accuracy = result.get('accuracy', 0)
        persona  = result.get('persona', '')

        total_7day = sum(r.get('yhat', 0) for r in forecast)

        # ── Helpers ─────────────────────────────────────────────────────
        def fnum(n, d=2):
            return f"{float(n or 0):,.{d}f}"

        def row_bg(is_holiday, day_of_week):
            if is_holiday:       return "background:#FEF2F2;"
            if day_of_week in (5, 6): return "background:#FFFBEB;"
            return ""

        WEATHER_ICON_MAP = {'Sunny': '☀️', 'Cloudy': '⛅', 'Raining': '🌧️'}
        DAYS_LIST        = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

        # ── Section 1: 7-Day forecast table ─────────────────────────────
        forecast_rows_html = ""
        for row in forecast:
            dt       = datetime.strptime(row['ds'], '%Y-%m-%d')
            day_name = DAYS_LIST[dt.weekday() + 1 if dt.weekday() < 6 else 0]
            # use Python weekday: Mon=0…Sun=6
            py_wd    = dt.weekday()        # 0=Mon … 6=Sun
            js_day   = (py_wd + 1) % 7    # 0=Sun … 6=Sat
            is_sun   = (js_day == 0)
            is_hol   = row.get('is_holiday', False)
            flags    = []
            if is_hol:                flags.append("🏖️ Public Holiday")
            if row.get('is_friday'):  flags.append("🎉 Friday Promo — 20% off Lattes")
            if row.get('season'):     flags.append(f"🎁 {row['season']}")
            if is_sun:                flags.append("🔒 Closed")
            bg_style = "background:#FEF2F2;" if is_hol else ("background:#FFFBEB;" if js_day in (0,6) else "")
            weather_cell = "—" if is_sun else f"{WEATHER_ICON_MAP.get(row.get('weather','Cloudy'),'⛅')} {row.get('weather','Cloudy')}"
            yhat_disp  = "0.00" if is_sun else fnum(row.get('yhat', 0))
            lower_disp = "0.00" if is_sun else fnum(row.get('yhat_lower', 0))
            upper_disp = "0.00" if is_sun else fnum(row.get('yhat_upper', 0))
            forecast_rows_html += f"""
            <tr style="{bg_style}">
                <td style="font-family:monospace;padding:5px 8px;">{row['ds']}</td>
                <td style="padding:5px 8px;font-weight:600;">{DAYS_LIST[dt.isoweekday() % 7]}</td>
                <td style="padding:5px 8px;">{weather_cell}</td>
                <td style="padding:5px 8px;font-size:8px;">{" · ".join(flags) if flags else "—"}</td>
                <td style="text-align:right;font-family:monospace;padding:5px 8px;font-weight:700;">RM {yhat_disp}</td>
                <td style="text-align:right;font-family:monospace;padding:5px 8px;color:#64748B;">RM {lower_disp}</td>
                <td style="text-align:right;font-family:monospace;padding:5px 8px;color:#64748B;">RM {upper_disp}</td>
            </tr>"""

        # ── Section 2: Ingredients ───────────────────────────────────────
        ingr_html = ""
        if ingr:
            beans_kg  = (ingr.get('beans_g',  0) or 0) / 1000
            milk_l    = (ingr.get('milk_ml',  0) or 0) / 1000
            choco_kg  = (ingr.get('choco_g',  0) or 0) / 1000
            ice_kg    = (ingr.get('ice_g',    0) or 0) / 1000
            hot_cups  = ingr.get('cup_hot',   0) or 0
            cold_cups = ingr.get('cup_cold',  0) or 0
            ingr_html = f"""
            <div class="section-title">What to Buy This Week — Ingredient Shopping Guide</div>
            <p style="font-size:9px;color:#64748B;margin-bottom:10px;">
                Based on your predicted sales, here is how much of each ingredient you will need over the next 7 days.
            </p>
            <div class="kpi-grid">
                <div class="kpi-box"><div class="kpi-label">Coffee Beans</div><div class="kpi-value">{fnum(beans_kg)} kg</div></div>
                <div class="kpi-box"><div class="kpi-label">Fresh Milk</div><div class="kpi-value">{fnum(milk_l, 1)} L</div></div>
                <div class="kpi-box"><div class="kpi-label">Cocoa Powder</div><div class="kpi-value">{fnum(choco_kg)} kg</div></div>
                <div class="kpi-box"><div class="kpi-label">Crushed Ice</div><div class="kpi-value">{fnum(ice_kg, 1)} kg</div></div>
                <div class="kpi-box"><div class="kpi-label">Hot Paper Cups</div><div class="kpi-value">{hot_cups} pcs</div></div>
                <div class="kpi-box"><div class="kpi-label">Cold Plastic Cups</div><div class="kpi-value">{cold_cups} pcs</div></div>
            </div>"""

        # ── Section 3: How accurate has the forecast been ────────────────
        fva_section_html = ""
        fva_rows_to_show = []
        if fva_all:
            sorted_fva = sorted(fva_all, key=lambda r: r['ds'])
            # 🟢 REQUIREMENT: Constrain accuracy check to only the most recent 7 days
            fva_rows_to_show = sorted_fva[-7:]

            if fva_rows_to_show:
                total_p = sum(r.get('predicted', 0) for r in fva_rows_to_show)
                total_a = sum(r.get('actual', 0)    for r in fva_rows_to_show)
                mape_vals = []
                fva_body = ""
                for r in fva_rows_to_show:
                    pred = r.get('predicted', 0) or 0
                    act  = r.get('actual', 0) or 0
                    var  = act - pred
                    var_pct = (var / pred * 100) if pred > 0 else 0
                    mape_vals.append(abs(var_pct))
                    var_color = "#059669" if var >= 0 else "#DC2626"
                    sign = "+" if var >= 0 else ""
                    fva_body += f"""
                    <tr>
                        <td style="font-family:monospace;padding:4px 8px;">{r['ds']}</td>
                        <td style="text-align:right;font-family:monospace;padding:4px 8px;">RM {fnum(pred)}</td>
                        <td style="text-align:right;font-family:monospace;padding:4px 8px;">RM {fnum(act)}</td>
                        <td style="text-align:right;font-family:monospace;padding:4px 8px;color:{var_color};">
                            {sign}RM {fnum(abs(var))} ({sign}{fnum(var_pct, 1)}%)
                        </td>
                    </tr>"""
                avg_mape    = sum(mape_vals) / len(mape_vals) if mape_vals else 0
                avg_acc     = max(0, round(100 - avg_mape, 1))
                
                fva_section_html = f"""
                <div style="page-break-before:always;"></div>
                <div class="section-title">How Accurate Has the Forecast Been? — Recent 7 Days</div>
                <div class="kpi-grid">
                    <div class="kpi-box"><div class="kpi-label">Total Predicted</div><div class="kpi-value">RM {fnum(total_p)}</div></div>
                    <div class="kpi-box"><div class="kpi-label">Total Actual Sales</div><div class="kpi-value">RM {fnum(total_a)}</div></div>
                    <div class="kpi-box"><div class="kpi-label">Forecast Accuracy</div><div class="kpi-value">{avg_acc}%</div></div>
                    <div class="kpi-box"><div class="kpi-label">Total Difference</div>
                        <div class="kpi-value" style="color:{'#059669' if total_a >= total_p else '#DC2626'};">
                            RM {fnum(abs(total_a - total_p))} {'over' if total_a >= total_p else 'under'}
                        </div>
                    </div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Date</th>
                            <th style="text-align:right;">Predicted (RM)</th>
                            <th style="text-align:right;">Actual Sales (RM)</th>
                            <th style="text-align:right;">Difference</th>
                        </tr>
                    </thead>
                    <tbody>{fva_body}</tbody>
                </table>"""

        # ── Section 4: Busiest hours ─────────────────────────────────────
        hourly_html = ""
        if hourly:
            sorted_hourly = sorted(hourly, key=lambda r: r.get('hour', 0))
            max_rev = max((r.get('revenue', 0) for r in sorted_hourly), default=1) or 1
            rows_h  = ""
            for h in sorted_hourly:
                bar_w = int(h.get('revenue', 0) / max_rev * 100)
                rows_h += f"""
                <tr>
                    <td style="font-family:monospace;padding:4px 8px;">{str(h.get('hour',0)).zfill(2)}:00</td>
                    <td style="text-align:right;padding:4px 8px;">{h.get('transactions', 0):,}</td>
                    <td style="text-align:right;padding:4px 8px;">RM {fnum(h.get('revenue', 0))}</td>
                    <td style="padding:4px 8px;"><div style="background:#2563EB;height:8px;border-radius:4px;width:{bar_w}%;"></div></td>
                </tr>"""
            hourly_html = f"""
            <div class="section-title">Best and Slowest Hours of the Day</div>
            <table>
                <thead>
                    <tr><th>Hour</th><th style="text-align:right;">Orders</th><th style="text-align:right;">Revenue (RM)</th><th>Activity Level</th></tr>
                </thead>
                <tbody>{rows_h}</tbody>
            </table>"""

        # ── Section 5: Weather impact by shift ──────────────────────────
        wbt_html = ""
        if wbt:
            wbt_pivot = {}
            for r in wbt:
                shift   = r.get('shift', '—')
                weather = r.get('weather', '—')
                count   = r.get('count', 0)
                if shift not in wbt_pivot:
                    wbt_pivot[shift] = {}
                wbt_pivot[shift][weather] = count
            wbt_rows = ""
            for shift, conds in wbt_pivot.items():
                wbt_rows += f"""
                <tr>
                    <td style="padding:4px 8px;font-weight:600;">{shift}</td>
                    <td style="padding:4px 8px;">{WEATHER_ICON_MAP.get('Sunny','☀️')} {conds.get('Sunny', 0)} days</td>
                    <td style="padding:4px 8px;">{WEATHER_ICON_MAP.get('Cloudy','⛅')} {conds.get('Cloudy', 0)} days</td>
                    <td style="padding:4px 8px;">{WEATHER_ICON_MAP.get('Raining','🌧️')} {conds.get('Raining', 0)} days</td>
                </tr>"""
            wbt_html = f"""
            <div class="section-title">How Weather Affects Your Sales — by Time of Day</div>
            <table>
                <thead>
                    <tr><th>Shift</th><th>Sunny Days</th><th>Cloudy Days</th><th>Rainy Days</th></tr>
                </thead>
                <tbody>{wbt_rows}</tbody>
            </table>"""

        # ── Section 6: What drives the forecast ─────────────────────────
        avg_day   = total_7day / 7 if total_7day else 0
        sunny_est = fnum(avg_day * 1.12)
        rain_est  = fnum(avg_day * 0.88)
        drivers_html = f"""
        <div class="section-title">What Drives the Forecast?</div>
        <table>
            <thead><tr><th>Factor</th><th>Effect on Sales</th></tr></thead>
            <tbody>
                <tr><td style="padding:5px 8px;">☀️ Sunny or Cloudy day</td>
                    <td style="padding:5px 8px;color:#059669;font-weight:600;">Estimated RM {sunny_est} / day</td></tr>
                <tr><td style="padding:5px 8px;">🌧️ Rainy day</td>
                    <td style="padding:5px 8px;color:#DC2626;font-weight:600;">Estimated RM {rain_est} / day</td></tr>
                <tr><td style="padding:5px 8px;">🎉 Every Friday</td>
                    <td style="padding:5px 8px;">20% off all Latte drinks — boosts orders</td></tr>
                <tr><td style="padding:5px 8px;">🎁 School holidays &amp; festive seasons</td>
                    <td style="padding:5px 8px;">Higher foot traffic expected</td></tr>
                <tr><td style="padding:5px 8px;">🏢 Putrajaya — public holidays</td>
                    <td style="padding:5px 8px;color:#DC2626;">−35% (office workers stay home)</td></tr>
                <tr><td style="padding:5px 8px;">🎓 Puncak Alam — public holidays</td>
                    <td style="padding:5px 8px;color:#059669;">+15% (students &amp; residents gather)</td></tr>
            </tbody>
        </table>"""

        # ── Assemble full HTML document ──────────────────────────────────
        html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  @page {{ size: A4 portrait; margin: 15mm 12mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ 
    font-family: 'Segoe UI', Arial, sans-serif; 
    font-size: 10px; 
    color: #1E293B; 
    line-height: 1.5;
    background: #FFFFFF;
  }}
  .report-container {{
    max-width: 800px;
    margin: 0 auto;
    background: #fff;
  }}
  .header {{ 
    background: linear-gradient(135deg,#1E2A3A,#2A3B52); 
    padding: 30px; 
    color: white; 
    -webkit-print-color-adjust: exact;
  }}
  .brand {{ display:flex; align-items:center; gap:10px; margin-bottom:12px; }}
  .accent {{ height:4px; background:linear-gradient(90deg,#F59E0B,#3B82F6,#10B981); -webkit-print-color-adjust: exact; }}
  .body {{ padding: 25px 30px; }}
  
  .section-title {{ 
    font-size: 10px; 
    font-weight: 700; 
    text-transform: uppercase; 
    letter-spacing: 1px; 
    border-left: 4px solid #3B82F6; 
    padding-left: 10px; 
    margin: 25px 0 12px; 
    color: #0F172A;
    page-break-after: avoid;
  }}
  
  /* Responsive KPI Grid */
  .kpi-grid {{ 
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 20px;
  }}
  .kpi-box {{ 
    flex: 1 1 calc(25% - 12px);
    min-width: 120px;
    border: 1px solid #E2E8F0; 
    border-radius: 8px; 
    padding: 12px; 
    background: #F8FAFC;
    page-break-inside: avoid;
  }}
  .kpi-label {{ font-size: 8px; color: #64748B; text-transform: uppercase; margin-bottom: 4px; font-weight: 600; }}
  .kpi-value {{ font-size: 15px; font-weight: 700; font-family: 'Courier New', monospace; color: #1E293B; }}
  
  /* Tables */
  .table-wrapper {{ width: 100%; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; table-layout: auto; }}
  thead th {{ 
    background: #1E2A3A; 
    color: white; 
    padding: 8px 10px; 
    text-align: left; 
    font-size: 9px; 
    font-weight: 600;
    -webkit-print-color-adjust: exact;
  }}
  tbody td {{ 
    padding: 7px 10px; 
    border-bottom: 1px solid #E2E8F0; 
    font-size: 9px; 
    vertical-align: middle;
  }}
  tr {{ page-break-inside: avoid; }}
  
  .persona-box {{ 
    background: #EFF6FF; 
    padding: 12px 15px; 
    border-radius: 8px; 
    margin-bottom: 20px; 
    font-size: 10px; 
    border-left: 4px solid #3B82F6;
    color: #1E40AF;
  }}
  
  .footer {{ 
    padding: 15px 30px; 
    background: #F8FAFC; 
    border-top: 1px solid #E2E8F0; 
    display: flex; 
    justify-content: space-between; 
    font-size: 8px; 
    color: #94A3B8;
    margin-top: 30px;
  }}
  
  .note {{ 
    font-size: 9px; 
    color: #64748B; 
    line-height: 1.6; 
    margin-top: 25px; 
    padding: 15px; 
    background: #F1F5F9; 
    border-radius: 8px; 
    border: 1px dashed #CBD5E1;
  }}

  /* Printing Helpers */
  .page-break {{ page-break-before: always; }}
  
  @media screen and (max-width: 600px) {{
    .kpi-box {{ flex: 1 1 calc(50% - 12px); }}
    .header, .body, .footer {{ padding: 15px; }}
  }}
</style>
</head>
<body>
<div class="report-container">
<div class="header">
    <div class="brand">
        <div style="background:#F59E0B;padding:6px;border-radius:6px;font-size:14px;">☕</div>
        <strong style="font-size:12px;letter-spacing:0.5px;">Mini Coffee Shop</strong>
    </div>
    <div style="font-size:22px;font-weight:800;letter-spacing:-0.5px;margin-bottom:5px;">Sales Forecast Report</div>
    <div style="font-size:10px;opacity:.85;font-weight:500;">Branch: {branch_name} &nbsp; | &nbsp; Generated: {now_str}</div>
</div>
<div class="accent"></div>
<div class="body">

    <!-- Model performance summary -->
    <div class="section-title">How Reliable Is This Forecast?</div>
    <div class="kpi-grid">
        <div class="kpi-box">
            <div class="kpi-label">Error Rate</div>
            <div class="kpi-value">{mape}%</div>
            <div style="font-size:7px;color:#94A3B8;margin-top:3px;">Lower is better</div>
        </div>
        <div class="kpi-box">
            <div class="kpi-label">Typical Variation</div>
            <div class="kpi-value">RM {fnum(rmse)}</div>
            <div style="font-size:7px;color:#94A3B8;margin-top:3px;">Per day average</div>
        </div>
        <div class="kpi-box">
            <div class="kpi-label">Forecast Accuracy</div>
            <div class="kpi-value">{accuracy}%</div>
            <div style="font-size:7px;color:#94A3B8;margin-top:3px;">Higher is better</div>
        </div>
        <div class="kpi-box">
            <div class="kpi-label">7-Day Prediction</div>
            <div class="kpi-value">RM {fnum(total_7day)}</div>
            <div style="font-size:7px;color:#94A3B8;margin-top:3px;">Expected gross</div>
        </div>
    </div>

    <div class="persona-box">
        <strong>Branch Profile:</strong> {persona}
    </div>

    <!-- Day-by-day forecast -->
    <div class="section-title">Day-by-Day Forecast Breakdown — Next 7 Days</div>
    <table>
        <thead>
            <tr>
                <th>Date</th>
                <th>Day</th>
                <th>Weather</th>
                <th>Notes &amp; Promotions</th>
                <th style="text-align:right;">Predicted Sales (RM)</th>
                <th style="text-align:right;">Low Estimate</th>
                <th style="text-align:right;">High Estimate</th>
            </tr>
        </thead>
        <tbody>{forecast_rows_html}</tbody>
    </table>

    <!-- Ingredients -->
    {ingr_html}

    <!-- Historical accuracy -->
    {fva_section_html}

    <!-- Hourly breakdown -->
    {hourly_html}

    <!-- Weather by shift -->
    {wbt_html}

    <!-- What drives the forecast -->
    {drivers_html}

    <div class="note">
        <strong>About this forecast:</strong> Predictions are made using an AI model trained on your past sales data.
        It accounts for Malaysian public holidays, school breaks, weekly patterns, and upcoming weather conditions.
        The "Low Estimate" and "High Estimate" columns show the range the model is 95% confident your actual sales will fall within.
        Actual results may vary due to unexpected events, weather changes, or promotions not yet in the system.
    </div>

</div>
<div class="footer">
    <div>MCS Analytics v1.0</div>
    <div>Internal Use Only — Mini Coffee Shop</div>
    <div>{now_str}</div>
</div>
</body>
</html>"""

        try:
            from weasyprint import HTML as WP_HTML
            pdf_bytes = WP_HTML(string=html_doc).write_pdf()
            response  = make_response(pdf_bytes)
            response.headers['Content-Type']        = 'application/pdf'
            fn = f"MCS_Forecast_{branch_name.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.pdf"
            response.headers['Content-Disposition'] = f'attachment; filename="{fn}"'
            return response
        except ImportError:
            response = make_response(html_doc)
            response.headers['Content-Type']        = 'text/html; charset=utf-8'
            fn = f"MCS_Forecast_{branch_name.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.html"
            response.headers['Content-Disposition'] = f'attachment; filename="{fn}"'
            return response

    except Exception as e:
        return jsonify({"status": "error", "message": f"PDF render error: {str(e)}"}), 500


# ============================================================
#    API — PDF EXPORT (Executive Report Only, Month-Based)
# ============================================================
C_DARK    = colors.HexColor('#1E293B')   
C_MID     = colors.HexColor('#334155')   
C_MUTED   = colors.HexColor('#64748B')   
C_LIGHT   = colors.HexColor('#F1F5F9')   
C_BLUE    = colors.HexColor('#2563EB')   
C_TEAL    = colors.HexColor('#0D9488')   
C_RED     = colors.HexColor('#DC2626')   
C_AMBER   = colors.HexColor('#D97706')   
C_WHITE   = colors.white
C_BORDER  = colors.HexColor('#CBD5E1')   
C_HDRROW  = colors.HexColor('#1E293B')   
 
 
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
        cmds.append(('ROWBACKGROUNDS', (0, 1 if has_header else 0), (-1, -1),
                     [C_WHITE, C_LIGHT]))
    return TableStyle(cmds)
 
 
def _section(title, story, styles):
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width='100%', thickness=0.5, color=C_BORDER, spaceAfter=3))
    story.append(Paragraph(title, styles['RPT_SectionH']))
 
 
def _bar_cell(pct, bar_color=C_BLUE, width_pts=80):
    filled = int(pct / 100 * 12)
    bar = '█' * filled + '░' * (12 - filled)
    return f'<font color="#{bar_color.hexval()[1:] if hasattr(bar_color,"hexval") else "2563EB"}">{bar}</font>'
 
 
# ── PAGE TEMPLATE ─────────────────────────────────────────────
def _make_on_page(branch_label, month_label, now_str):
    def on_page(canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(C_DARK)
        canvas.rect(0, h - 28*mm, w, 28*mm, fill=1, stroke=0)
        canvas.setFillColor(C_BLUE)
        canvas.rect(0, h - 29.5*mm, w, 1.5*mm, fill=1, stroke=0)
 
        canvas.setFillColor(C_WHITE)
        canvas.setFont('Helvetica-Bold', 13)
        canvas.drawString(14*mm, h - 14*mm, '☕  Mini Coffee Shop — Executive Sales Report')
        canvas.setFont('Helvetica', 8)
        canvas.drawString(14*mm, h - 20*mm, f'{branch_label}  ·  {month_label}  ·  Confidential — Internal Use Only')
        canvas.drawRightString(w - 14*mm, h - 20*mm, f'Generated: {now_str}')
 
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
 
    try:
        cache_key = f"{branch_filter}_{date_from}_{date_to}"
        if cache_key in REPORT_CACHE:
            report_data = REPORT_CACHE[cache_key]
        else:
            report_data = _build_report_data(branch_filter, date_from, date_to)
            REPORT_CACHE[cache_key] = report_data
    except Exception as e:
        return jsonify({"status": "error", "message": f"Data error: {str(e)}"}), 500
 
    branch_label = branch_filter if branch_filter != 'all' else 'All Branches'
    now_str      = datetime.now().strftime('%d %b %Y, %I:%M %p')
    try:
        month_label = datetime.strptime(date_from, '%Y-%m-%d').strftime('%B %Y')
        month_key   = datetime.strptime(date_from, '%Y-%m-%d').strftime('%Y-%m')
    except Exception:
        month_label = date_from
        month_key   = date_from[:7]
 
    buf    = BytesIO()
    styles = _styles()
    M      = 14 * mm
 
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=M, rightMargin=M,
        topMargin=34*mm, bottomMargin=18*mm,
    )
 
    story = []
 
    def P(text, style='RPT_Body'):
        return Paragraph(text, styles[style])
 
    def num(n, dp=2):
        return f'{float(n or 0):,.{dp}f}'
 
    def pct_bar(pct, width=60):
        filled = max(0, min(12, int((pct or 0) / 100 * 12)))
        return '█' * filled + '░' * (12 - filled)
 
    # ─────────────────────────────────────────────────────────
    #   SECTION 1 — EXECUTIVE SUMMARY
    # ─────────────────────────────────────────────────────────
    _section('1.  Executive Summary', story, styles)
 
    rd             = report_data
    period_rev     = rd.get('period_revenue', 0)
    period_txns    = rd.get('period_txns', 0)
    daily_avg      = rd.get('daily_average', 0)
    aov            = rd.get('aov', 0)
    trend_label    = rd.get('trend_label', 'N/A')
    top_branch     = rd.get('top_branch', 'N/A')
    peak_hour      = rd.get('peak_hour', 'N/A')
    peak_day       = rd.get('peak_day', 'N/A')
    days_in_p      = max(len(rd.get('daily', [])), 1)
    avg_daily_t    = round(period_txns / days_in_p, 1)
 
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
        ('FONTNAME',      (0, 1), (0, -1), 'Helvetica-Bold'),  
        ('FONTNAME',      (2, 1), (2, -1), 'Helvetica-Bold'),  
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
        for i, k in enumerate(rec_keys):
            if k == month_key:
                mt_style.add('BACKGROUND', (0, i+1), (-1, i+1), colors.HexColor('#EFF6FF'))
                mt_style.add('FONTNAME',   (0, i+1), (-1, i+1), 'Helvetica-Bold')
                mt_style.add('TEXTCOLOR',  (-1, i+1), (-1, i+1), C_BLUE)
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
                p['product_id'],
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
            [p['product_id'], f"{int(p['qty']):,}", num(p['revenue'])]
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
            [str(p['payment_method']).upper(), f"{int(p['txn_count']):,}", f"{p['pct']}%", pct_bar(float(p['pct'] or 0))]
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
    #   SECTION 6 — TRANSACTION FLOW
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
 
    for line in ai_text.replace('\r\n', '\n').split('\n'):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 3))
            continue
        if line and line[0].isdigit() and len(line) > 2 and line[1] in '.):':
            story.append(Paragraph(f'• {line}', styles['RPT_Bullet']))
        elif line.startswith('•') or line.startswith('-'):
            story.append(Paragraph(line, styles['RPT_Bullet']))
        else:
            story.append(Paragraph(line, styles['RPT_AI']))
 
    story.append(Spacer(1, 8))
 
    top_prods_list = rd.get('top_products', [])
    findings = [
        ['Finding', 'Detail'],
        ['Revenue Trend',      trend_label],
        ['Top Branch',          top_branch],
        ['Avg Order Value',    f'RM {num(aov)}'],
        ['Busiest Window',     f'{peak_hour}  ·  {peak_day}'],
        ['Best Product',       f"{top_prods_list[0]['product_id']} ({int(top_prods_list[0]['qty']):,} units)" if top_prods_list else 'N/A'],
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
 
    on_page = _make_on_page(branch_label, month_label, now_str)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
 
    pdf_bytes = buf.getvalue()
    response  = make_response(pdf_bytes)
    response.headers['Content-Type']        = 'application/pdf'
    filename_str = f"MCS_Executive_Report_{branch_filter.replace(' ','_')}_{month_key}.pdf"
    filename_str = filename_str.replace(' ','_')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename_str}"'
    return response


# ============================================================
#    RUNTIME ENGINE EXECUTION ENTRYPOINT
# ============================================================
if __name__ == '__main__':
    # Threading explicitly active to support real-time Event Stream pipelines
    app.run(debug=True, threaded=True)