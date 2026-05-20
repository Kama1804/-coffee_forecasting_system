from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify
import os
import sqlite3
from functools import wraps
from werkzeug.utils import secure_filename
from init_db import initialize_database
from dotenv import load_dotenv
from etl_pipeline import ETLPipeline
from gemini_agent import get_ai_insight
from forecast_engine import ForecastEngine, MY_PUBLIC_HOLIDAYS, MY_SEASONS
from datetime import datetime, timedelta

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

    # Fetch data profile summary for display
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

    # Fetch past successful uploads
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

        # Revenue, Volume, Transactions
        cursor.execute(f"""
            SELECT COALESCE(SUM(s.total_revenue), 0) as rev,
                   COALESCE(SUM(s.quantity_sold), 0) as vol,
                   COUNT(s.transaction_id)           as txns
            FROM sales_transaction s {join} {where}
        """, params)
        metrics = cursor.fetchone()

        # Top Branch — always previous calendar month
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

        # Sparkline — last 7 days within current filter
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
            "top_branch":         top_branch,
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

        # Trend — monthly for all/year, daily for month/week
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

        # Category mix
        cursor.execute(f"""
            SELECT s.product_category as cat, SUM(s.total_revenue) as rev
            FROM sales_transaction s {join} {where}
            GROUP BY cat ORDER BY rev DESC
        """, params)
        cat_rows = cursor.fetchall()

        # Top 3 products
        cursor.execute(f"""
            SELECT s.product_name, SUM(s.quantity_sold) as qty
            FROM sales_transaction s {join} {where}
            GROUP BY s.product_name ORDER BY qty DESC LIMIT 3
        """, params)
        top_prods = cursor.fetchall()

        # Weak 3 products
        cursor.execute(f"""
            SELECT s.product_name, SUM(s.quantity_sold) as qty
            FROM sales_transaction s {join} {where}
            GROUP BY s.product_name ORDER BY qty ASC LIMIT 3
        """, params)
        weak_prods = cursor.fetchall()

        # Payment breakdown
        cursor.execute(f"""
            SELECT s.payment_method, COUNT(*) as cnt
            FROM sales_transaction s {join} {where}
            GROUP BY s.payment_method ORDER BY cnt DESC
        """, params)
        pay_rows = cursor.fetchall()

        # Peak hours heatmap
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

        conn.close()

        return jsonify({
            "status": "success",
            "trend": {
                "labels":     trend_labels,
                "raw":        [r['period'] for r in trend_rows],
                "data":       [round(r['rev'], 2) for r in trend_rows],
                "is_monthly": use_monthly
            },
            "category": {
                "labels": [r['cat'] for r in cat_rows],
                "data":   [round(r['rev'], 2) for r in cat_rows]
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
#   API — AI CHATBOT
# ============================================================

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({"status": "error", "message": "No message provided."}), 400

    user_message = data['message']

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COALESCE(SUM(total_revenue),0) FROM sales_transaction")
        total_rev = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM sales_transaction")
        total_txns = cursor.fetchone()[0]
        cursor.execute("SELECT MIN(sale_date), MAX(sale_date) FROM sales_transaction")
        date_row   = cursor.fetchone()
        date_range = f"{date_row[0]} to {date_row[1]}" if date_row[0] else "N/A"
        cursor.execute("SELECT COUNT(DISTINCT sale_date) FROM sales_transaction")
        days_active = cursor.fetchone()[0] or 1
        daily_avg   = total_rev / days_active

        cursor.execute("""
            SELECT product_name, SUM(quantity_sold) as qty, SUM(total_revenue) as rev
            FROM sales_transaction GROUP BY product_name ORDER BY qty DESC LIMIT 3
        """)
        top_products = "\n".join([
            f"  - {r['product_name']}: {r['qty']} units (RM {r['rev']:,.2f})"
            for r in cursor.fetchall()
        ])

        cursor.execute("""
            SELECT b.branch_name, SUM(s.total_revenue) as rev, COUNT(*) as txns
            FROM sales_transaction s JOIN branch b ON s.branch_id = b.branch_id
            GROUP BY b.branch_name ORDER BY rev DESC
        """)
        branch_rows    = cursor.fetchall()
        branch_summary = "\n".join([
            f"  - {r['branch_name']}: RM {r['rev']:,.2f} ({r['txns']} transactions)"
            for r in branch_rows
        ])
        top_branch = branch_rows[0]['branch_name'] if branch_rows else 'N/A'

        cursor.execute("""
            SELECT CAST(substr(transaction_time,1,2) AS INTEGER) as hr, COUNT(*) as cnt
            FROM sales_transaction GROUP BY hr ORDER BY cnt DESC LIMIT 1
        """)
        peak_row  = cursor.fetchone()
        peak_hour = f"{peak_row['hr']:02d}:00 - {peak_row['hr']+1:02d}:00" if peak_row else "N/A"

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

        system_prompt = f"""You are the AI Business Advisor for 'Mini Coffee Shop' — a Malaysian coffee chain with branches in Putrajaya and Puncak Alam.

CRITICAL RULES:
1. NEVER ask the user to provide data — you already have live database access below.
2. Be confident, specific, and actionable. Use exact numbers from the data.
3. Format responses with bullet points and bold headers for readability.
4. Keep answers concise but complete — max 300 words unless asked for a full report.
5. Always make at least one specific recommendation based on the data.
6. LANGUAGE RULE: Detect the language the user writes in and reply in the SAME language.
   - English -> reply in English.
   - Bahasa Melayu -> reply in Bahasa Melayu.
   - Mixed (Manglish/rojak) -> reply in the same mixed style.
   - Always use Malaysian context — "RM" not "$".

=== LIVE DATABASE METRICS ===
Data Period: {date_range}
Total Revenue: RM {total_rev:,.2f}
Total Transactions: {total_txns:,}
Daily Average Revenue: RM {daily_avg:,.2f}
Peak Transaction Hour: {peak_hour}

=== BRANCH PERFORMANCE ===
{branch_summary}
Top Branch: {top_branch}

=== TOP 3 PRODUCTS ===
{top_products}

=== CATEGORY REVENUE ===
{categories}

=== WEATHER IMPACT ON REVENUE ===
{weather_summary}

=== PROPHET 7-DAY FORECAST ===
{forecast_summary}

=== USER QUESTION ===
{user_message}"""

    except Exception as e:
        print(f"Chatbot context error: {e}")
        system_prompt = (
            f"You are the AI Business Advisor for 'Mini Coffee Shop'. "
            f"Database unavailable. Answer as expert coffee consultant.\n"
            f"User question: {user_message}"
        )

    success, response = get_ai_insight(system_prompt)
    if success:
        return jsonify({"status": "success", "response": response})
    else:
        return jsonify({"status": "error", "response": response}), 503


# ============================================================
#   API — FORECAST
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
                "status":     "success",
                "mape":       result['mape'],
                "rmse":       result['rmse'],
                "accuracy":   result.get('accuracy', 0),
                "persona":    result.get('persona', ''),
                "historical": result['historical'],
                "forecast":   result['forecast'],
                "hourly":     result.get('hourly', []),
                "forecast_vs_actual": result.get('forecast_vs_actual', []),
                "insample_fit":       result.get('insample_fit', []),
                "weather_by_time": result.get('weather_by_time', [])
            })
        else:
            return jsonify({"status": "error", "message": result}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#   API — REPORT DATA (CLIENT-SIDE PDF)
# ============================================================

@app.route('/api/report_data')
@login_required
def api_report_data():
    """Fetches all rich operational data needed for the html2pdf frontend."""
    branch_filter = request.args.get('branch', 'all')
    rpt_type      = request.args.get('type', 'weekly')
    date_from     = request.args.get('date_from', None)
    date_to       = request.args.get('date_to',   None)

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        # ── Build WHERE clause ──────────────────────────────────────
        branch_cond = "b.branch_name = ?" if branch_filter != 'all' else None
        date_cond   = "s.sale_date BETWEEN ? AND ?" if date_from and date_to else None

        conditions = [c for c in [branch_cond, date_cond] if c]
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        def base_params():
            p = []
            if branch_cond:
                p.append(branch_filter)
            if date_cond:
                p.extend([date_from, date_to])
            return p

        # Human-readable period label
        if date_from and date_to:
            period_str = f"{date_from} to {date_to}"
        else:
            limit_days = 7 if rpt_type == 'weekly' else 30
            period_str = f"Last {limit_days} Days"

        # ── 1. Daily Actuals ────────────────────────────────────────
        if date_from and date_to:
            cursor.execute(f"""
                SELECT s.sale_date as date, SUM(s.total_revenue) as revenue, COUNT(*) as txns
                FROM sales_transaction s
                JOIN branch b ON s.branch_id = b.branch_id
                {where_clause}
                GROUP BY s.sale_date ORDER BY s.sale_date ASC
            """, base_params())
        else:
            limit_days = 7 if rpt_type == 'weekly' else 30
            cursor.execute(f"""
                SELECT s.sale_date as date, SUM(s.total_revenue) as revenue, COUNT(*) as txns
                FROM sales_transaction s
                JOIN branch b ON s.branch_id = b.branch_id
                {where_clause}
                GROUP BY s.sale_date ORDER BY s.sale_date DESC LIMIT ?
            """, base_params() + [limit_days])

        daily_data = [dict(r) for r in cursor.fetchall()]
        if not (date_from and date_to):
            daily_data = list(reversed(daily_data))

        # ── 2. Weather Impact ───────────────────────────────────────
        cursor.execute(f"""
            SELECT s.weather_condition,
                   ROUND(SUM(s.total_revenue) / COUNT(DISTINCT s.sale_date), 2) as avg_rev
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            {where_clause}
            GROUP BY s.weather_condition
        """, base_params())
        weather_data = {r['weather_condition']: r['avg_rev'] for r in cursor.fetchall()}

        # ── 3. Peak Hour ──────────────────────────────
        cursor.execute(f"""
            SELECT CAST(substr(s.transaction_time, 1, 2) AS INTEGER) as hr, COUNT(*) as cnt
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            {where_clause}
            GROUP BY hr ORDER BY cnt DESC LIMIT 1
        """, base_params())
        ph = cursor.fetchone()
        if ph and ph['hr'] is not None:
            peak_hour = f"{int(ph['hr']):02d}:00–{int(ph['hr'])+1:02d}:00"
        else:
            peak_hour = "N/A"

        # ── 4. Peak Day ─────────────────────────────────────────────
        cursor.execute(f"""
            SELECT CASE strftime('%w', s.sale_date)
                WHEN '0' THEN 'Sunday' WHEN '1' THEN 'Monday' WHEN '2' THEN 'Tuesday'
                WHEN '3' THEN 'Wednesday' WHEN '4' THEN 'Thursday' WHEN '5' THEN 'Friday' WHEN '6' THEN 'Saturday'
            END as day_name, COUNT(*) as cnt
            FROM sales_transaction s
            JOIN branch b ON s.branch_id = b.branch_id
            {where_clause}
            GROUP BY day_name ORDER BY cnt DESC LIMIT 1
        """, base_params())
        pd = cursor.fetchone()
        peak_day = pd['day_name'] if pd else "N/A"

        # ── 5. Prophet Forecast ───────────────────────
        fc_conds  = ["b.branch_name = ?"] if branch_filter != 'all' else []
        fc_where  = ("WHERE " + " AND ".join(fc_conds)) if fc_conds else ""
        fc_params = [branch_filter] if branch_filter != 'all' else []

        try:
            cursor.execute(f"""
                SELECT f.forecast_date as ds, f.predicted_revenue as yhat,
                       f.lower_bound as yhat_lower, f.upper_bound as yhat_upper,
                       f.weather_condition as weather
                FROM sales_forecast f
                JOIN branch b ON f.branch_id = b.branch_id
                {fc_where}
                ORDER BY f.forecast_date ASC LIMIT 7
            """, fc_params)
            forecast_data = [dict(r) for r in cursor.fetchall()]
        except sqlite3.OperationalError:
            cursor.execute(f"""
                SELECT f.forecast_date as ds, f.predicted_revenue as yhat,
                       f.lower_bound as yhat_lower, f.upper_bound as yhat_upper,
                       'Cloudy' as weather
                FROM sales_forecast f
                JOIN branch b ON f.branch_id = b.branch_id
                {fc_where}
                ORDER BY f.forecast_date ASC LIMIT 7
            """, fc_params)
            forecast_data = [dict(r) for r in cursor.fetchall()]

        # =================================================================
        # Re-inject dynamic flags for the PDF Report 
        # =================================================================
        holiday_set = {h[0] for h in MY_PUBLIC_HOLIDAYS}
        season_map  = {}
        for start, end, name in MY_SEASONS:
            d = datetime.strptime(start, '%Y-%m-%d')
            e = datetime.strptime(end,   '%Y-%m-%d')
            while d <= e:
                season_map[d.strftime('%Y-%m-%d')] = name
                d += timedelta(days=1)

        for row in forecast_data:
            date_str = row['ds']
            dt_obj = datetime.strptime(date_str, '%Y-%m-%d')
            row['is_holiday'] = date_str in holiday_set
            row['is_friday']  = (dt_obj.weekday() == 4)
            row['season']     = season_map.get(date_str)
        # =================================================================

        conn.close()

        # ── 6. AI Insight ──
        prompt = (
            f"Write a concise 3-sentence executive summary for a Malaysian coffee shop performance report. "
            f"Peak hour is {peak_hour}. Mention one inventory and one staffing recommendation. "
            "Professional tone. No markdown asterisks or bullet points."
        )
        success, insight = get_ai_insight(prompt)

        return jsonify({
            "status": "success",
            "period": period_str,
            "daily": daily_data,
            "weather": weather_data,
            "peak_hour": peak_hour,
            "peak_day": peak_day,
            "forecast": forecast_data,
            "mape": 24.69,
            "rmse": 227.85,
            "training_days": 90,
            "ai_insight": insight if success else "AI insight temporarily unavailable."
        })

    except Exception as e:
        print("Report API Error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
#   RUN
# ============================================================
if __name__ == '__main__':
    app.run(debug=True)