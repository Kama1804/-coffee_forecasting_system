from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify, make_response, Response
import os
import sqlite3
import time
import io
import json
import html
import math
import re
from functools import wraps
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Core Module Dependencies
from init_db import initialize_database
from etl_pipeline import ETLPipeline
from gemini_agent import (get_ai_insight, stream_ai_insight, build_slim_context,
                           fast_kpi_bypass, classify_intent, process_chat_message,
                           stream_chat_message)
from forecast_engine import ForecastEngine, MY_PUBLIC_HOLIDAYS, MY_SEASONS
from analytics import (get_dashboard_metrics, calculate_ingredient_demand, 
                       revenue_decline_and_product_mix_profiler, weather_payday_cross_tabulation,
                       get_ramadhan_peak_hours, get_regular_peak_hours)
from io import BytesIO

load_dotenv(override=True)

app = Flask(__name__)


# ============================================================
#    CUSTOM JSON ENCODER — fixes numpy int64/float64 serialization
# ============================================================
def _sanitize_for_json(obj):
    """
    Recursively convert numpy scalars and any non-serializable numeric
    types to native Python ints/floats so jsonify never throws
    'Object of type int64 is not JSON serializable'.
    Does NOT require numpy to be imported here — uses the .item() method
    that every numpy scalar exposes, plus a NaN/Inf guard for floats.
    """
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_json(i) for i in obj]
    elif isinstance(obj, tuple):
        return tuple(_sanitize_for_json(i) for i in obj)
    elif hasattr(obj, 'item'):
        # All numpy scalar types (int64, float32, bool_, …) expose .item()
        return obj.item()
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


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

def sanitize_history_for_llm(history_list):
    """Strips bulky chart JSON structures from session history strings to maintain rapid generation speeds."""
    cleaned = []
    for turn in history_list:
        bot_msg = turn.get('bot', '')
        bot_msg_clean = re.sub(r'\[CHART_DATA=.*?\]', '[Visual Chart Component Sent]', bot_msg)
        cleaned.append({
            "user": turn.get('user', ''),
            "bot": bot_msg_clean
        })
    return cleaned


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
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT branch_name FROM branch WHERE is_active = 1 ORDER BY branch_name ASC")
    branches = [row[0] for row in cursor.fetchall()]
    conn.close()
    return render_template('dashboard.html', branches=branches)

@app.route('/forecast')
@login_required
def forecast():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT branch_code, branch_name, location_type, description, holiday_effect FROM branch WHERE is_active = 1 ORDER BY branch_name ASC")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return render_template('forecast.html', branches=branches)

@app.route('/report')
@login_required
def report():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT branch_name FROM branch WHERE is_active = 1 ORDER BY branch_name ASC")
    branches = [row[0] for row in cursor.fetchall()]
    conn.close()
    return render_template('report.html', branches=branches)

@app.route('/chatbot')
@login_required
def chatbot():
    return render_template('chatbot.html')

@app.route('/manage-business')
@login_required
def manage_business():
    return render_template('manage_business.html')

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
                    # Trigger Auto-Tune AI Intelligence (Self-Learning)
                    from analytics import sync_business_intelligence
                    sync_success, sync_logs = sync_business_intelligence(DB_PATH)
                    print(f"[AI AUTO-TUNE] {sync_logs}")

                    # Store missing recipes in session for the UI alert ONLY after DB success
                    session['missing_recipes'] = getattr(pipeline, 'missing_recipes', [])
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
        for f in os.listdir(app.config['UPLOAD_FOLDER']):
            if not f.endswith('.csv'):
                try:
                    os.remove(os.path.join(app.config['UPLOAD_FOLDER'], f))
                except Exception:
                    pass
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

    # Fetch active branch codes for the dynamic UI hint
    active_codes = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT branch_code FROM branch WHERE is_active = 1")
        active_codes = [row[0] for row in cursor.fetchall()]
        conn.close()
    except: pass

    return render_template('upload.html', profile=profile, past_uploads=past_uploads, active_codes=active_codes)


# ============================================================
#  RECIPE REGISTRY API
# ============================================================
@app.route('/api/get-missing-recipes')
@login_required
def api_get_missing_recipes():
    """Returns the list of items from the last upload that need recipes."""
    missing = session.get('missing_recipes', [])
    return jsonify({"status": "success", "missing": missing})

@app.route('/api/save-recipe', methods=['POST'])
@login_required
def api_save_recipe():
    """Saves a new recipe to the database."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data provided"}), 400
    
    item_name = data.get('item_name')
    if not item_name:
        return jsonify({"status": "error", "message": "Item name is required"}), 400
        
    try:
        conn = get_db_connection()
        cursor = conn.row_factory = None # Reset row factory for insert
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO product_recipes 
            (item_name, beans_g, milk_ml, choco_g, ice_g, whip_g, cup_type, custom_ingredients)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            item_name,
            float(data.get('beans_g', 0)),
            float(data.get('milk_ml', 0)),
            float(data.get('choco_g', 0)),
            float(data.get('ice_g', 0)),
            float(data.get('whip_g', 0)),
            data.get('cup_type', 'None'),
            json.dumps(data.get('custom_ingredients', {}))
        ))
        conn.commit()
        conn.close()
        
        # Remove from session list once saved
        missing = session.get('missing_recipes', [])
        if item_name in missing:
            missing.remove(item_name)
            session['missing_recipes'] = missing
            session.modified = True
            
        return jsonify({"status": "success", "message": f"Recipe for {item_name} saved successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ============================================================
#    BUSINESS MANAGEMENT API (Step 5)
# ============================================================
@app.route('/api/manage/branches')
@login_required
def api_manage_branches():
    try:
        from analytics import HOLIDAYS # We'll need this list
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Fetch base branch data
        cursor.execute("SELECT * FROM branch ORDER BY branch_code ASC")
        branches = [dict(row) for row in cursor.fetchall()]
        
        # 2. Enrich with Maturity Stats
        enriched_branches = []
        for b in branches:
            # Count unique days
            cursor.execute("SELECT COUNT(DISTINCT transaction_date) FROM sales_transaction WHERE branch_id = ?", (b['branch_code'],))
            total_days = cursor.fetchone()[0] or 0
            
            # Count holidays seen
            placeholders = ', '.join(['?'] * len(HOLIDAYS))
            cursor.execute(f"""
                SELECT COUNT(DISTINCT transaction_date) 
                FROM sales_transaction 
                WHERE branch_id = ? AND transaction_date IN ({placeholders})
            """, [b['branch_code']] + HOLIDAYS)
            holidays_seen = cursor.fetchone()[0] or 0
            
            b['maturity_days'] = total_days
            b['maturity_holidays'] = holidays_seen
            enriched_branches.append(b)

        conn.close()
        return jsonify({"status": "success", "branches": enriched_branches})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/branch/<path:code>')
@login_required
def api_manage_get_branch(code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM branch WHERE branch_code = ?", (code,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return jsonify(dict(row))
        return jsonify({"status": "error", "message": "Branch not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/branch/save', methods=['POST'])
@login_required
def api_manage_save_branch():
    data = request.get_json()
    action = data.get('action', 'add')
    code = data.get('branch_code')
    name = data.get('branch_name')
    l_type = data.get('location_type')
    desc = data.get('description', '')
    h_effect = float(data.get('holiday_effect_pct') or 0) / 100.0
    dist = data.get('district')
    state = data.get('state')
    lat = float(data.get('latitude') or 0)
    lon = float(data.get('longitude') or 0)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if action == 'add':
            cursor.execute("""
                INSERT INTO branch (branch_code, branch_name, location_type, district, state, latitude, longitude, description, holiday_effect, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (code, name, l_type, dist, state, lat, lon, desc, h_effect))
        else:
            cursor.execute("""
                UPDATE branch 
                SET branch_name = ?, location_type = ?, district = ?, state = ?, latitude = ?, longitude = ?, description = ?, holiday_effect = ?
                WHERE branch_code = ?
            """, (name, l_type, dist, state, lat, lon, desc, h_effect, code))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Branch {code} saved successfully."})
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": f"Branch Code {code} already exists."}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/branch/toggle', methods=['POST'])
@login_required
def api_manage_toggle_branch():
    data = request.get_json()
    code = data.get('branch_code')
    active = data.get('is_active')
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE branch SET is_active = ? WHERE branch_code = ?", (active, code))
        conn.commit()
        conn.close()
        status_txt = "activated" if active else "deactivated"
        return jsonify({"status": "success", "message": f"Branch {code} {status_txt}."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/recipes')
@login_required
def api_manage_recipes():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM product_recipes ORDER BY item_name ASC")
        recipes = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({"status": "success", "recipes": recipes})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/recipe/<path:name>')
@login_required
def api_manage_get_recipe(name):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM product_recipes WHERE item_name = ?", (name,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return jsonify(dict(row))
        return jsonify({"status": "error", "message": "Recipe not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/recipe/save', methods=['POST'])
@login_required
def api_manage_save_recipe():
    data = request.get_json()
    name = data.get('item_name')
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE product_recipes 
            SET beans_g = ?, milk_ml = ?, choco_g = ?, ice_g = ?, whip_g = ?, cup_type = ?, custom_ingredients = ?
            WHERE item_name = ?
        """, (
            float(data.get('beans_g') or 0),
            float(data.get('milk_ml') or 0),
            float(data.get('choco_g') or 0),
            float(data.get('ice_g') or 0),
            float(data.get('whip_g') or 0),
            data.get('cup_type'),
            data.get('custom_ingredients', '{}'),
            name
        ))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Recipe for {name} updated successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/manage/recipe/toggle', methods=['POST'])
@login_required
def api_manage_toggle_recipe():
    data = request.get_json()
    name = data.get('item_name')
    active = data.get('is_active')
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE product_recipes SET is_active = ? WHERE item_name = ?", (active, name))
        conn.commit()
        conn.close()
        status_txt = "activated" if active else "deactivated"
        return jsonify({"status": "success", "message": f"Recipe for {name} {status_txt}."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


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
#    PROMOTION INTELLIGENCE API
# ============================================================
@app.route('/api/promo-efficiency')
@login_required
def api_promo_efficiency():
    """Returns promotion performance and ROI metrics."""
    branch = request.args.get('branch', 'all')
    time_filter = request.args.get('time', 'all')
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(transaction_date) as max_d FROM sales_transaction")
        max_date = cursor.fetchone()['max_d'] or datetime.today().strftime('%Y-%m-%d')
        conn.close()

        where, params = build_where(branch, time_filter, max_date, alias='s')
        
        from analytics import promo_efficiency_analyzer
        efficiency = promo_efficiency_analyzer(where, params)
        return jsonify({"status": "success", "data": _sanitize_for_json(efficiency)})
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
            prev_label = prev_dt.strftime('%b %Y')
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

        use_yearly  = (time_filter == 'all')
        use_monthly = time_filter.startswith('year_')

        if use_yearly:
            date_col = "strftime('%Y', s.transaction_date)"
        elif use_monthly:
            date_col = "strftime('%Y-%m', s.transaction_date)"
        else:
            date_col = "s.transaction_date"

        cursor.execute(f"""
            SELECT {date_col} as period, 
                   SUM(s.gross_sales_MYR) as gross,
                   SUM(s.discount_amount_MYR) as disc,
                   SUM(s.Total_Bill_MYR) as net
            FROM sales_transaction s {where}
            GROUP BY period ORDER BY period ASC
        """, params)
        trend_rows = cursor.fetchall()

        trend_labels = []
        trend_gross  = []
        trend_disc   = []
        trend_net    = []
        for r in trend_rows:
            p = r['period']
            trend_gross.append(round(r['gross'], 2))
            trend_disc.append(round(r['disc'], 2))
            trend_net.append(round(r['net'], 2))
            if use_yearly:
                trend_labels.append(p)
            elif use_monthly and p:
                try:
                    dt = datetime.strptime(p + '-01', '%Y-%m-%d')
                    trend_labels.append(dt.strftime('%b %Y'))
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

        cursor.execute(f"""
            SELECT s.item_name, SUM(s.transaction_qty) as qty
            FROM sales_transaction s {where}
            GROUP BY s.item_name ORDER BY qty DESC LIMIT 5
        """, params)
        top_prods = cursor.fetchall()
        top_prod_labels = [r['item_name'] for r in top_prods]

        cursor.execute(f"""
            SELECT s.item_name, SUM(s.transaction_qty) as qty
            FROM sales_transaction s {where}
            GROUP BY s.item_name ORDER BY qty ASC LIMIT 3
        """, params)
        weak_prods = cursor.fetchall()
        weak_prod_labels = [r['item_name'] for r in weak_prods]

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

        outlet_period_col = "strftime('%Y', s.transaction_date)" if use_yearly else "strftime('%Y-%m', s.transaction_date)"

        cursor.execute(f"""
            SELECT DISTINCT {outlet_period_col} as period
            FROM sales_transaction s {where}
            ORDER BY period ASC
        """, params)
        all_month_periods = [r['period'] for r in cursor.fetchall()]

        all_month_labels = []
        for p in all_month_periods:
            if use_yearly:
                all_month_labels.append(p)
            else:
                try:
                    dt = datetime.strptime(p + '-01', '%Y-%m-%d')
                    all_month_labels.append(dt.strftime('%B %Y'))
                except Exception:
                    all_month_labels.append(p)

        monthly_by_branch = {b: [0] * len(all_month_periods) for b in all_branches}
        aov_by_branch     = {b: [0] * len(all_month_periods) for b in all_branches}
        period_idx_map    = {p: i for i, p in enumerate(all_month_periods)}

        cursor.execute(f"""
            SELECT {outlet_period_col} as period,
                   s.store_location,
                   SUM(s.Total_Bill_MYR) as rev,
                   COUNT(s.transaction_id) as txns
            FROM sales_transaction s {where}
            GROUP BY period, s.store_location
            ORDER BY period ASC
        """, params)
        for r in cursor.fetchall():
            p = r['period']
            b = r['store_location']
            if p in period_idx_map and b in monthly_by_branch:
                idx = period_idx_map[p]
                rev = r['rev']
                txns = r['txns']
                monthly_by_branch[b][idx] = round(rev, 2)
                aov_by_branch[b][idx]     = round(rev / txns, 2) if txns > 0 else 0

        

        cursor.execute("""
            SELECT DISTINCT strftime('%Y-%m', transaction_date) as period
            FROM sales_transaction
            WHERE transaction_date IS NOT NULL AND transaction_date != ''
            ORDER BY period ASC
        """)
        available_months = [r['period'] for r in cursor.fetchall() if r['period']]

        branch_id = None
        if branch_filter != 'all':
            cursor.execute("SELECT branch_code FROM branch WHERE branch_name = ?", (branch_filter,))
            br_row = cursor.fetchone()
            if br_row: branch_id = br_row[0]

        conn.close()

        return jsonify({
            "status": "success",
            "available_months": available_months,
            "trend": {
                "labels":     trend_labels,
                "raw":        [r['period'] for r in trend_rows],
                "data":       trend_net,
                "gross":      trend_gross,
                "discount":   trend_disc,
                "is_monthly": use_monthly,
                "is_yearly":  use_yearly
            },
            "monthly": {
                "labels":    all_month_labels,
                "raw":       all_month_periods,
                "branches":  all_branches,
                "by_branch": monthly_by_branch,
                "aov_by_branch": aov_by_branch
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
            ],
            "peak_data": {
                "regular": _sanitize_for_json(get_regular_peak_hours(branch_id)),
                "ramadhan": _sanitize_for_json(get_ramadhan_peak_hours(branch_id))
            }
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
    """Gathers critical operational metrics from SQLite and packages the global parameters."""
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

    # 🟢 FIX: Correctly query for the overall top-performing branch (highest cumulative historical revenue)
    cursor.execute("""
        SELECT store_location, SUM(Total_Bill_MYR) as rev
        FROM sales_transaction
        GROUP BY store_location
        ORDER BY rev DESC LIMIT 1
    """)
    tb_row = cursor.fetchone()
    top_branch = tb_row['store_location'] if tb_row else 'N/A'

    # 🟢 FIX: Correctly query for the overall peak transaction hour slot
    cursor.execute("""
        SELECT Hour, COUNT(*) as cnt
        FROM sales_transaction
        GROUP BY Hour
        ORDER BY cnt DESC LIMIT 1
    """)
    ph_row = cursor.fetchone()
    peak_hour = f"{int(ph_row['Hour'] or 0):02d}:00" if ph_row else 'N/A'

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
        'top_branch': top_branch,
        'peak_hour': peak_hour
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

    # PURIFICATION STEP: Scrub bulky chart layouts from memory window before calling Gemini
    purified_history = sanitize_history_for_llm(session['chat_history'][-3:])
    history_blocks = []
    for turn in purified_history:
        history_blocks.append(f"User: {turn['user']}\nAI: {turn['bot']}")

    history_text = "\n\n".join(history_blocks)

    final_prompt = f"""{system_context}

=== CONVERSATION HISTORY (last {len(history_blocks)} turns) ===
{history_text if history_text else "(No prior conversation)"}

=== INCOMING MESSAGE ===
User: {user_message}
AI:"""

    intent = classify_intent(user_message)
    success, ai_response = get_ai_insight(final_prompt, intent=intent)  

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

    # PURIFICATION STEP: Scrub chart layouts going into real-time SSE stream engine
    purified_history = sanitize_history_for_llm(session['chat_history'][-3:])
    history_blocks = []
    for turn in purified_history:
        history_blocks.append(f"User: {turn['user']}\nAI: {turn['bot']}")

    final_prompt = f"""{system_context}

=== CONVERSATION HISTORY ===
{chr(10).join(history_blocks) if history_blocks else "(No prior conversation)"}

=== INCOMING MESSAGE ===
User: {user_message}
AI:"""

    session['_pending_user_msg'] = user_message
    session.modified = True

    intent = classify_intent(user_message)
    return Response(
        stream_ai_insight(final_prompt, intent=intent),
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
    branch_id   = request.args.get('branch_id',   None)
    branch_name = request.args.get('branch_name', None)
    
    # Fallback to first active branch if none provided
    if not branch_id or not branch_name:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT branch_code, branch_name FROM branch WHERE is_active = 1 LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            if row:
                branch_id = row[0]
                branch_name = row[1]
        except: pass

    if not branch_id:
        return jsonify({"status": "error", "message": "No active branch found"}), 404

    try:
        cache_key = f"{branch_id}_{branch_name}"
        if cache_key in FORECAST_CACHE:
            result = FORECAST_CACHE[cache_key]
        else:
            engine          = ForecastEngine()
            success, result = engine.generate_5_day_forecast(branch_id, branch_name)
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
                trend_label = f"{sign}{pct:.1f}% vs previous {span} days"
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
    cursor.execute(f"""
        SELECT s.item_name as product_id,
               SUM(s.transaction_qty) as qty,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.item_name ORDER BY qty DESC LIMIT 5
    """, base_params())
    top_products_raw = [dict(r) for r in cursor.fetchall()]

    top_products = []
    for p in top_products_raw:
        p['pct'] = round(p['revenue'] / period_revenue * 100, 1) if period_revenue else 0.0
        # No more SKU mapping needed, name is already friendly
        top_products.append(p)

    cursor.execute(f"""
        SELECT s.item_name as product_id,
               SUM(s.transaction_qty) as qty,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.item_name ORDER BY qty ASC LIMIT 3
    """, base_params())
    bottom_products_raw = [dict(r) for r in cursor.fetchall()]
    
    bottom_products = []
    for p in bottom_products_raw:
        # No more SKU mapping needed
        bottom_products.append(p)

    # ── Category breakdown ─────────────────────────────────────
    cursor.execute(f"""
        SELECT s.product_category,
               ROUND(SUM(s.Total_Bill_MYR), 2) as revenue
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.product_category ORDER BY revenue DESC
    """, base_params())
    category_breakdown = []
    for r in cursor.fetchall():
        row = dict(r)
        row['pct'] = round(row['revenue'] / period_revenue * 100, 1) if period_revenue else 0.0
        category_breakdown.append(row)

    # ── Payment method breakdown ───────────────────────────────
    cursor.execute(f"""
        SELECT s.payment_method,
               COUNT(*) as txn_count
        FROM sales_transaction s
        {where_clause}
        GROUP BY s.payment_method ORDER BY txn_count DESC
    """, base_params())
    payment_breakdown = []
    for r in cursor.fetchall():
        row = dict(r)
        row['pct'] = round(row['txn_count'] / period_txns * 100, 1) if period_txns else 0.0
        payment_breakdown.append(row)

    # ── Monthly trend — Consecutive 6 months ending at selected month ──
    cursor.execute("SELECT DISTINCT store_location FROM sales_transaction ORDER BY store_location")
    all_branches = [r[0] for r in cursor.fetchall() if r[0]]

    # Generate 6 consecutive month keys ending at selected month
    target_month_str = date_from[:7]
    target_dt = datetime.strptime(target_month_str + "-01", "%Y-%m-%d")
    
    trend_keys = []
    for i in range(5, -1, -1):
        m = target_dt.month - i
        y = target_dt.year
        while m <= 0:
            m += 12
            y -= 1
        trend_keys.append(f"{y:04d}-{m:02d}")

    trend_labels = []
    for k in trend_keys:
        try:
            trend_labels.append(datetime.strptime(k + '-01', '%Y-%m-%d').strftime('%b %Y'))
        except Exception:
            trend_labels.append(k)

    by_branch_monthly = {b: [0]*6 for b in all_branches}
    
    cursor.execute(f"""
        SELECT strftime('%Y-%m', transaction_date) as month, store_location,
               SUM(Total_Bill_MYR) as rev
        FROM sales_transaction
        WHERE strftime('%Y-%m', transaction_date) IN ({",".join(["?"]*6)})
        GROUP BY month, store_location
    """, trend_keys)
    
    month_idx_map = {m: i for i, m in enumerate(trend_keys)}
    for r in cursor.fetchall():
        m_key = r['month']
        br_nm = r['store_location']
        if m_key in month_idx_map and br_nm in by_branch_monthly:
            by_branch_monthly[br_nm][month_idx_map[m_key]] = round(r['rev'], 2)

    monthly_trend = {
        "labels":    trend_labels,
        "keys":      trend_keys,
        "branches":  all_branches,
        "by_branch": by_branch_monthly
    }

    # ── Forecast data (upcoming 5-day) ────────────────────────
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
            {fc_where} GROUP BY f.forecast_date ORDER BY f.forecast_date ASC LIMIT 5
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
        cursor.execute("SELECT branch_code FROM branch WHERE branch_name = ?", (branch_filter,))
        br_row = cursor.fetchone()
        b_id = br_row[0] if br_row else None
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

    pva_within_range = 0
    pva_count_exceeded = 0
    pva_count_on_target = 0
    pva_count_safe = 0
    pva_count_off = 0
    for r in pva_with_actual:
        act = r['actual']
        y   = r['yhat']
        lo  = r['yhat_lower']
        up  = r['yhat_upper']
        if act > up:
            pva_count_exceeded += 1
        elif act < lo:
            pva_count_off += 1
        elif act < y:
            pva_count_safe += 1
            pva_within_range += 1
        else:
            pva_count_on_target += 1
            pva_within_range += 1

    try:
        branch_id = None
        if branch_filter != 'all':
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT branch_code FROM branch WHERE branch_name = ?", (branch_filter,))
            row = cursor.fetchone()
            conn.close()
            if row: branch_id = row[0]
            
        diagnostics = revenue_decline_and_product_mix_profiler(branch_id, reference_date=date_to)
    except Exception:
        diagnostics = {}

    # ── Promotion Efficiency Analysis ──────────────────────────
    try:
        from analytics import promo_efficiency_analyzer
        promo_efficiency = promo_efficiency_analyzer(where_clause, base_params())
    except Exception:
        promo_efficiency = []

    conn.close()

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
        f"Then give exactly 3 numbered, actionable steps the owner can take this month to improve revenue. "
        f"Ensure each step is a complete, finished sentence."
    )
    _report_intent = {"primary": "trend_analysis", "style": "analytical_narrative", "depth": "medium", "multi": []}
    success_ai, insight = get_ai_insight(ai_prompt, intent=_report_intent)

    raw_result = {
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
        "pva_count_exceeded":   pva_count_exceeded,
        "pva_count_on_target":  pva_count_on_target,
        "pva_count_safe":       pva_count_safe,
        "pva_count_off":        pva_count_off,
        "pva_days_with_data":   len(pva_with_actual),
        "diagnostics":          diagnostics,
        "promo_efficiency":     promo_efficiency,
        "ai_insight":           insight if success_ai else "AI insight temporarily unavailable."
    }

    return _sanitize_for_json(raw_result)


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
    if request.method == 'POST':
        body        = request.get_json() or {}
        branch_id   = body.get('branch_id',   None)
        branch_name = body.get('branch_name', None)
        month_filter = body.get('month_filter', None)
        result      = body.get('forecast_data', None)
    else:
        branch_id    = request.args.get('branch_id',   None)
        branch_name  = request.args.get('branch_name', None)
        month_filter = request.args.get('month', None,  type=str)
        result       = None

    # Dynamic Fallback
    if not branch_id or not branch_name:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT branch_code, branch_name FROM branch WHERE is_active = 1 LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            if row:
                branch_id = row[0]
                branch_name = row[1]
        except: pass

    if not branch_id:
        return jsonify({"status": "error", "message": "No active branch found"}), 404

    if result is None:
        try:
            cache_key = f"{branch_id}_{branch_name}"
            if cache_key in FORECAST_CACHE:
                result = FORECAST_CACHE[cache_key]
            else:
                engine          = ForecastEngine()
                success, result = engine.generate_5_day_forecast(branch_id, branch_name)
                if not success:
                    return jsonify({"status": "error", "message": result}), 500
                FORECAST_CACHE[cache_key] = result
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    try:
        now_str  = datetime.now().strftime('%d %b %Y, %I:%M %p')
        forecast = result.get('forecast', [])
        fva_all  = result.get('forecast_vs_actual', [])
        
        fva_rows = []
        fva_title = "How Accurate Has the Forecast Been? — Recent 7 Days"
        
        if fva_all:
            sorted_fva = sorted(fva_all, key=lambda r: r['ds'])
            if month_filter:
                fva_rows = [r for r in sorted_fva if r['ds'].startswith(month_filter)]
                try:
                    dt_mo = datetime.strptime(month_filter, '%Y-%m')
                    fva_title = f"How Accurate Has the Forecast Been? — {dt_mo.strftime('%B %Y')}"
                except:
                    fva_title = f"How Accurate Has the Forecast Been? — {month_filter}"
            else:
                fva_rows = sorted_fva[-7:]

        html_doc = render_template('forecast_pdf_export.html',
                                   data=result,
                                   branch_name=branch_name,
                                   now_str=now_str,
                                   fva_title=fva_title,
                                   fva_rows=fva_rows)

        try:
            from weasyprint import HTML as WP_HTML
            pdf_bytes = WP_HTML(string=html_doc).write_pdf()
            response  = make_response(pdf_bytes)
            response.headers['Content-Type']        = 'application/pdf'
            fn = f"MCS_Forecast_{branch_name.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.pdf"
            response.headers['Content-Disposition'] = f'attachment; filename="{fn}"'
            return response
        except Exception as e:
            print("PDF Render Error (WeasyPrint):", e)
            response = make_response(html_doc)
            response.headers['Content-Type']        = 'text/html; charset=utf-8'
            fn = f"MCS_Forecast_{branch_name.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.html"
            response.headers['Content-Disposition'] = f'attachment; filename="{fn}"'
            return response

    except Exception as e:
        return jsonify({"status": "error", "message": f"PDF render error: {str(e)}"}), 500


@app.route('/api/export-pdf')
@login_required
def api_export_pdf():
    branch_filter = request.args.get('branch',    'all')
    date_from     = request.args.get('date_from', None)
    date_to       = request.args.get('date_to',   None)

    if not date_from or not date_to:
        return jsonify({"status":"error", "message":"date_from and date_to are required."}), 400

    try:
        cache_key = f"{branch_filter}_{date_from}_{date_to}"
        rd = REPORT_CACHE.get(cache_key) or _build_report_data(branch_filter, date_from, date_to)
        REPORT_CACHE[cache_key] = rd
    except Exception as e:
        return jsonify({"status":"error", "message":f"Data error: {str(e)}"}), 500

    try:
        month_label = datetime.strptime(date_from,'%Y-%m-%d').strftime('%B %Y')
        month_key   = datetime.strptime(date_from,'%Y-%m-%d').strftime('%Y-%m')
    except Exception:
        month_label = date_from
        month_key   = date_from[:7]

    now_str = datetime.now().strftime('%d %b %Y, %I:%M %p')

    html_content = render_template('report_pdf_export.html',
                                   data=rd,
                                   branch=branch_filter,
                                   month_label=month_label,
                                   date_from=date_from,
                                   date_to=date_to,
                                   now_str=now_str)

    try:
        from weasyprint import HTML as WP_HTML
        pdf_bytes = WP_HTML(string=html_content).write_pdf()
        
        response = make_response(pdf_bytes)
        response.headers['Content-Type'] = 'application/pdf'
        slug = branch_filter.replace(' ','_')
        response.headers['Content-Disposition'] = (
            f'attachment; filename="MCS_Executive_Report_{slug}_{month_key}.pdf"')
        return response
    except Exception as e:
        print("PDF Export Error (WeasyPrint):", e)
        response = make_response(html_content)
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        slug = branch_filter.replace(' ','_')
        response.headers['Content-Disposition'] = (
            f'attachment; filename="MCS_Executive_Report_{slug}_{month_key}.html"')
        return response
    

# ============================================================
#    RUNTIME ENGINE EXECUTION ENTRYPOINT
# ============================================================
if __name__ == '__main__':
    # Threading explicitly active to support real-time Event Stream pipelines
    app.run(debug=True, threaded=True)