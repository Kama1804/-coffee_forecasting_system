from google import genai
from google.genai import errors, types
import os
import time
import json
import re
from dotenv import load_dotenv
from pathlib import Path
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from analytics import get_dashboard_metrics, SKU_MAPPING

load_dotenv(dotenv_path=Path(__file__).parent / '.env', override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Helper tool to convert database SKU codes back to friendly menu names for the AI context
REVERSE_SKU_LOOKUP = {v: k.title() for k, v in SKU_MAPPING.items()}

# ============================================================
#    RESPONSE STYLE INSTRUCTION
# ============================================================
RESPONSE_STYLE_INSTRUCTION = """
You are Gemini AI Advisor for a Decision Support System (DSS) dashboard used by a mobile food truck and stall booth coffee business.

=== CORE RESPONSE RULES ===
1. Keep responses concise (Max 150 words unless detailed analysis is requested).
2. Prioritize performance: Avoid long explanations or number repetition.
3. Use professional business language.
4. *** CHART GATE — ABSOLUTE RULE ***
   You MUST check the user's message for explicit visualization intent words BEFORE generating any chart.
   ALLOWED trigger words: "chart", "graph", "plot", "visual", "visualize", "show me a chart", "draw".
   If NONE of those words appear in the user's message → DO NOT produce any [CHART_DATA=...] block.
   Answering a question about revenue, trends, or comparisons does NOT automatically require a chart.
   Text answers are always preferred unless the user explicitly asks for a chart.
5. NEVER output raw JSON directly. JSON MUST ONLY exist inside: [CHART_DATA={...}]
6. NEVER use markdown code blocks for chart payloads.
7. ALWAYS ensure chart JSON is valid.

=== CHART PAYLOAD FORMAT ===
Must always be: [CHART_DATA={"type":"bar","title":"...","labels":[...],"datasets":[{"label":"...","data":[...]}]}]

=== STRICT JSON RULES ===
1. ALWAYS use "labels", "datasets", "label", "data".
2. NEVER use "values" or "name" inside dataset objects.
3. labels length MUST match dataset data length.
4. Valid JSON only (double quotes, no trailing commas).

=== RESPONSE STRUCTURE ===
1. Short insight summary.
2. Key business observation.
3. Visualization payload (ONLY if the user EXPLICITLY asked for a chart/graph/plot; MUST be the LAST part).

Never explain the JSON. Never place extra text after CHART_DATA.
"""

_CHART_KEYWORDS = {"chart", "graph", "plot", "visual", "visualize", "draw"}


# ============================================================
#    IN-MEMORY CACHING SNAPSHOT
# ============================================================
GLOBAL_CHAT_CACHE = {
    "payload_dict": None,
    "expiry_timestamp": 0
}
CACHE_TTL_SECONDS = 300


def get_db_connection():
    db_path = os.path.join('database', 'coffee_shop.db')
    return sqlite3.connect(db_path)


def fast_kpi_bypass(user_message: str, db_data: dict):
    """Robust regex-based bypass parser for sub-second metrics loops."""
    msg = user_message.lower().strip()
    
    # Exclude bypass when specific relative temporal or comparative queries are made
    has_temporal_modifiers = any(k in msg for k in ['last', 'this', 'month', 'compare', 'trend', '202'])
    
    # 1. Total Revenue Matches
    if re.search(r'\b(total revenue|how much did we make|all time revenue|revenue)\b', msg) and not has_temporal_modifiers:
        return f"The all-time total revenue across all operating channels stands at RM {db_data.get('total_rev', 0):,.2f}."
        
    # 2. Volume and Transaction Matches
    if re.search(r'\b(total transactions|how many tickets|transaction count|receipts)\b', msg) and not has_temporal_modifiers:
        return f"The network has finalized {db_data.get('total_txns', 0):,} total transactions all-time."
        
    # 3. Daily Averages
    if re.search(r'\b(daily average|average daily revenue|average revenue per day)\b', msg):
        return f"The historical net baseline average revenue is RM {db_data.get('daily_avg', 0):,.2f} per day."
        
    # 4. Top Performing Operations
    if re.search(r'\b(top branch|best branch|highest revenue branch|busiest location)\b', msg):
        return f"The top performing channel by aggregate historical revenue volume is {db_data.get('top_branch', 'N/A')}."
        
    # 5. Peak Hours
    if re.search(r'\b(peak hour|busiest hour|rush hour|busiest time)\b', msg):
        return f"The transactional core peak operating timeline occurs at hour slot {db_data.get('peak_hour', 'N/A')}."
        
    return None


def _resolve_token_budget(prompt: str, override: int | None) -> int:
    if override is not None:
        return override
    prompt_lower = prompt.lower()
    if any(kw in prompt_lower for kw in _CHART_KEYWORDS):
        return 800
    if "executive summary" in prompt_lower or "report" in prompt_lower:
        return 600
    return 500


def get_ai_insight(prompt: str, max_tokens: int = None) -> tuple[bool, str]:
    """Core Gemini API call using the official new SDK."""
    if not client:
        return False, "System Error: Gemini API key is missing from the environment."

    full_prompt = RESPONSE_STYLE_INSTRUCTION + "\n\n" + prompt
    token_budget = _resolve_token_budget(prompt, max_tokens)

    PRIMARY_MODEL  = "gemini-3.1-flash-lite"
    FALLBACK_MODEL = "gemini-2.5-flash-lite"
    MAX_RETRIES    = 1
    BASE_DELAY     = 1

    def _call(model: str) -> str:
        response = client.models.generate_content(
            model=model,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=token_budget,
                temperature=0.3,
            )
        )
        return response.text.strip()

    def _is_retryable(error_msg: str) -> bool:
        return any(k in error_msg for k in ["503", "unavailable", "overloaded", "429", "quota", "exhausted", "resource_exhausted", "rate"])

    def _is_auth_error(error_msg: str) -> bool:
        return any(k in error_msg for k in ["403", "400", "invalid", "api_key", "permission"])

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            text = _call(PRIMARY_MODEL)
            return True, text
        except errors.APIError as e:
            error_msg = str(e).lower()
            last_error = str(e)
            if _is_auth_error(error_msg):
                return False, "AI Connection Error: Your API key is invalid or lacks permission. Check your .env file."
            if _is_retryable(error_msg) or "404" in error_msg:
                delay = BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
                continue
            return False, f"Gemini API Error: {str(e)}"
        except Exception as e:
            error_msg = str(e).lower()
            last_error = str(e)
            if "timeout" in error_msg:
                delay = BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
                continue
            return False, f"Connection Error: {str(e)}"

    print(f"[GEMINI] Trying fallback: {FALLBACK_MODEL}…")
    for attempt in range(2):
        try:
            text = _call(FALLBACK_MODEL)
            return True, text + f"\n\n*(Answered by {FALLBACK_MODEL} — primary model path was resetting)*"
        except errors.APIError as e:
            error_msg = str(e).lower()
            if _is_auth_error(error_msg):
                return False, "AI Connection Error: API key is invalid. Check your .env file."
            delay = BASE_DELAY * (2 ** attempt)
            time.sleep(delay)
        except Exception:
            time.sleep(BASE_DELAY)

    return False, f"Gemini AI is experiencing high demand right now. Please try again in 30–60 seconds. (Last error: {last_error})"


def stream_ai_insight(prompt: str):
    """Generator that yields text chunks from Gemini as SSE-formatted strings."""
    if not client:
        yield f"data: {json.dumps({'error': 'Gemini API key missing.'})}\n\n"
        return

    PRIMARY_MODEL = "gemini-3.1-flash-lite"
    full_prompt = RESPONSE_STYLE_INSTRUCTION + "\n\n" + prompt
    MAX_STREAM_TOKENS = max(_resolve_token_budget(prompt, None), 200)

    try:
        response = client.models.generate_content_stream(
            model=PRIMARY_MODEL,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=MAX_STREAM_TOKENS,
                temperature=0.3,
            )
        )
        for chunk in response:
            text = getattr(chunk, "text", "") or ""
            if text:
                yield f"data: {json.dumps({'chunk': text})}\n\n"
    except GeneratorExit:
        print("[STREAM LOG] Client disconnected or stream closed early. Exiting clean.")
        raise 
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    finally:
        try:
            yield f"data: {json.dumps({'done': True})}\n\n"
        except (RuntimeError, ValueError):
            pass


def get_business_advice(branch_id: str, branch_name: str) -> tuple[bool, str]:
    """Gathers denormalized metrics and Prophet forecasts to generate operational advice."""
    branch_id = str(branch_id).upper().strip()
    metrics = get_dashboard_metrics(branch_id)
    if not metrics:
        return False, "Not enough historical data to generate advice."

    db_path = os.path.join('database', 'coffee_shop.db')
    conn = sqlite3.connect(db_path)
    forecast_df = pd.read_sql_query(
        "SELECT forecast_date, predicted_revenue FROM sales_forecast "
        "WHERE branch_id = ? "
        "AND forecast_date > (SELECT COALESCE(MAX(transaction_date), '1970-01-01') FROM sales_transaction) "
        "ORDER BY forecast_date ASC LIMIT 5",
        conn, params=(branch_id,)
    )
    conn.close()

    if forecast_df.empty:
        return False, "No forecast data found. Please run Prophet engine first."

    forecast_text = forecast_df.to_string(index=False)
    peak_hours_text = ", ".join([f"{m['hour']} ({m['quantity_sold']} items)" for m in metrics['peak_hours'][:3]])
    top_products_text = ", ".join([f"{REVERSE_SKU_LOOKUP.get(m['product_id'], m['product_id'])} (RM {m['total_revenue']})" for m in metrics['product_mix'][:3]])

    system_prompt = f"""
You are an expert AI Business Advisor for 'Mini Coffee Shop' in {branch_name}, Malaysia.
Analyze the data below and give EXACTLY 3 actionable recommendations — one for Staffing, one for Inventory, one for Revenue Opportunity.

UPCOMING 5-DAY FORECAST:
{forecast_text}

PEAK HOURS: {peak_hours_text}
TOP PRODUCTS: {top_products_text}

FORMAT: Use this exact structure:
**Staffing:** [1 sentence action] — [1 sentence reason with specific data]
**Inventory:** [1 sentence action] — [1 sentence reason with specific data]
**Revenue Opportunity:** [1 sentence action] — [1 sentence reason with specific data]

Do NOT write any introduction or conclusion. Start immediately with **Staffing:**.
"""
    return get_ai_insight(system_prompt)


def build_slim_context(db_data: dict, user_message: str) -> str:
    """Analytical Router: Context generator incorporating forecasting and precise targeted monthly filters."""
    from analytics import multi_month_chart_pre_packager
    from forecast_engine import ForecastEngine
    msg = user_message.lower()

    base = f"""You are the AI Business Advisor for 'Mini Coffee Shop' (Malaysia). 
BE BLAZINGLY FAST. Use bullet points. No conversational filler.
Data period: {db_data.get('date_range', 'N/A')} | All-time revenue: RM {db_data.get('total_rev', 0):,.2f}"""

    sections = [base]

    # TARGETED MONTHLY RESOLVER (Matches e.g. "2026-05" or "2026-04" patterns)
    month_match = re.search(r'\b(20\d{2}-\d{2})\b', user_message)
    if month_match:
        target_month = month_match.group(1)
        try:
            db_path = os.path.join('database', 'coffee_shop.db')
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 1. Total monthly aggregates
            cursor.execute("""
                SELECT COALESCE(SUM(Total_Bill_MYR), 0), COUNT(DISTINCT transaction_id), COUNT(DISTINCT transaction_date)
                FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
            """, (target_month,))
            m_rev, m_txns, m_days = cursor.fetchone()
            m_days = m_days or 1
            m_daily_avg = m_rev / m_days
            
            # 2. Revenue and transactions by branch
            cursor.execute("""
                SELECT store_location, COALESCE(SUM(Total_Bill_MYR), 0), COUNT(transaction_id)
                FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY store_location
            """, (target_month,))
            br_rows = cursor.fetchall()
            br_ctx = "\n".join([f"  - {r[0]}: RM {r[1]:,.2f} ({r[2]:,} transactions)" for r in br_rows]) or "  - No branch logs found."

            # 3. Top 3 products sold during that month
            cursor.execute("""
                SELECT product_id, SUM(transaction_qty) as total_qty
                FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY product_id
                ORDER BY total_qty DESC LIMIT 3
            """, (target_month,))
            prod_rows = cursor.fetchall()
            prod_ctx = "\n".join([f"  - {REVERSE_SKU_LOOKUP.get(r[0], r[0])}: {r[1]:,} units" for r in prod_rows]) or "  - No product logs found."

            # 4. Busiest hour and busiest day of week during that month
            cursor.execute("""
                SELECT Hour, COUNT(*) as c
                FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY Hour ORDER BY c DESC LIMIT 1
            """, (target_month,))
            hour_row = cursor.fetchone()
            busy_hour = f"{hour_row[0]}:00" if hour_row else "N/A"

            cursor.execute("""
                SELECT "Day Name", COUNT(*) as c
                FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY "Day Name" ORDER BY c DESC LIMIT 1
            """, (target_month,))
            day_row = cursor.fetchone()
            busy_day = day_row[0] if day_row else "N/A"

            sections.append(f"""=== TARGETED PERFORMANCE BREAKDOWN FOR {target_month} ===
* Total Monthly Revenue: RM {m_rev:,.2f}
* Total Monthly Transactions: {m_txns:,}
* Daily Average Sales: RM {m_daily_avg:,.2f}
* Busiest Day: {busy_day}
* Peak Hour: {busy_hour}
* Branch Sales Performance Summary:
{br_ctx}
* Top 3 Best Selling Products:
{prod_ctx}""")
            conn.close()
        except Exception as e:
            sections.append(f"=== TARGETED PERFORMANCE BREAKDOWN FOR {target_month} ===\nError reading month snapshot metrics: {str(e)}")

    # 🟢 FORECAST & LIVE WEATHER HIGH-ACCURACY RESOLVER
    # Intercepts: forecast, predict, ramalan, cuaca, temp, week, minggu, etc.
    if any(k in msg for k in ['forecast', 'predict', 'ramalan', 'cuaca', 'temp', 'degree', 'celsius', 'week', 'minggu', 'unju']):
        try:
            engine = ForecastEngine()
            success_pj, pj_fc = engine.generate_5_day_forecast("STB-PJ1", "Putrajaya")
            success_pa, pa_fc = engine.generate_5_day_forecast("FT-PA1", "Puncak Alam")
            
            if success_pj and success_pa:
                pj_list = pj_fc.get('forecast', [])
                pa_list = pa_fc.get('forecast', [])
                
                start_date = pj_list[0]['ds'] if pj_list else 'N/A'
                end_date = pj_list[-1]['ds'] if pj_list else 'N/A'
                
                total_days = len(pj_list)
                closed_days = sum(1 for d in pj_list if d.get('is_closed', False))
                open_days = total_days - closed_days
                
                pj_lines = []
                for d in pj_list:
                    ds = d['ds']
                    dt_obj = datetime.strptime(ds, '%Y-%m-%d')
                    day_name = dt_obj.strftime('%A')
                    if d.get('is_closed', False):
                        pj_lines.append(f"  - {ds} ({day_name}): SHOP CLOSED (RM 0.00) — No forecast needed.")
                    else:
                        w = d.get('weather') or {}
                        w_text = f"{w.get('temp', 28.0)}°C ({w.get('label', 'Cloudy')})"
                        promos = ", ".join(d.get('promotions', [])) or "None"
                        pj_lines.append(f"  - {ds} ({day_name}): RM {d['yhat']:,.2f} | Weather: {w_text} | Promotions: {promos}")
                        
                pa_lines = []
                for d in pa_list:
                    ds = d['ds']
                    dt_obj = datetime.strptime(ds, '%Y-%m-%d')
                    day_name = dt_obj.strftime('%A')
                    if d.get('is_closed', False):
                        pa_lines.append(f"  - {ds} ({day_name}): SHOP CLOSED (RM 0.00) — No forecast needed.")
                    else:
                        w = d.get('weather') or {}
                        w_text = f"{w.get('temp', 28.0)}°C ({w.get('label', 'Cloudy')})"
                        promos = ", ".join(d.get('promotions', [])) or "None"
                        pa_lines.append(f"  - {ds} ({day_name}): RM {d['yhat']:,.2f} | Weather: {w_text} | Promotions: {promos}")
                
                sections.append(f"""=== LIVE ACCURATE PROPHET FORECAST ({start_date} to {end_date}) ===
* Sunday Closed Rule: Sunday is a scheduled rest day. The shop is closed, revenue is RM 0.00, and no forecast/weather is calculated.
* Active Operating Days: {open_days} operating day(s) with predictions (since {closed_days} of the 5 days is/are closed).
* Combined 5-day Ingredient Depletion: {json.dumps(pj_fc.get('ingredient_demand', {}), indent=1)}

* Putrajaya (STB-PJ1) Daily Projections:
{chr(10).join(pj_lines)}

* Puncak Alam (FT-PA1) Daily Projections:
{chr(10).join(pa_lines)}""")
        except Exception as e:
            sections.append(f"=== LIVE PROPHET FORECAST ===\nError calculating live forecasts on-the-fly: {str(e)}")

    # Staffing Vector Injection
    if any(k in msg for k in ['staff', 'people', 'worker', 'shift', 'peak', 'busy', 'hour', 'time']):
        peaks = db_data.get('branch_peaks', {})
        sections.append(f"=== PEAK HOURS (Top 3 per branch) ===\n- Puncak Alam (FT-PA1): {', '.join(peaks.get('FT-PA1', ['N/A']))}\n- Putrajaya (STB-PJ1): {', '.join(peaks.get('STB-PJ1', ['N/A']))}")

    # Inventory Matrix + Dynamic Demand Calculations Injection
    if any(k in msg for k in ['ingredient', 'inventory', 'stock', 'beans', 'milk', 'ice', 'cup', 'demand', 'order']):
        sections.append(f"=== RECENT PRODUCT CATEGORY PERFORMANCE ===\n{db_data.get('categories', 'No data')}")
        
        try:
            engine = ForecastEngine()
            _, pj_fc = engine.generate_5_day_forecast("STB-PJ1", "Putrajaya")
            _, pa_fc = engine.generate_5_day_forecast("FT-PA1", "Puncak Alam")
            
            pj_demand = pj_fc.get('ingredient_demand', {})
            pa_demand = pa_fc.get('ingredient_demand', {})
            
            combined_demand = {k: round(pj_demand.get(k, 0) + pa_demand.get(k, 0), 2) for k in pj_demand.keys()}
            sections.append(f"=== PREDICTED 5-DAY TOTAL INVENTORY DRAWDOWN DEMAND ===\n{json.dumps(combined_demand, indent=2)}")
        except Exception as e:
            sections.append(f"=== PREDICTED INVENTORY DRAWDOWN DEMAND ===\nForecast sub-calculations busy: {str(e)}")

    # Trend Analytics Injection (Only append if we didn't perform a targeted month lookup)
    if not month_match and any(k in msg for k in ['trend', 'month', 'growth', 'decline', 'revenue', 'year', 'last', 'compare', 'vs']):
        sections.append(f"=== MONTHLY TREND (Last 6 Months) ===\n{db_data.get('monthly_trend_summary', 'No trend data')}")

    # Visual Presentation Matrix Injection
    if any(k in msg for k in ['chart', 'graph', 'visual', 'plot', 'draw', 'show chart']):
        m_count = 3 
        m_match = re.search(r'last (\d+) month', msg)
        if m_match: m_count = int(m_match.group(1))
        chart_data = multi_month_chart_pre_packager(months=m_count)
        sections.append(f"=== CHART DATA (READY-TO-USE) ===\n[CHART_DATA={chart_data}]")

    return "\n\n".join(sections)


def build_chat_system_context(db_data: dict) -> str:
    """Backward compatibility fallback stub for historical app.py import chains."""
    return f"""You are the AI Business Advisor for 'Mini Coffee Shop' — Putrajaya and Puncak Alam.
=== LIVE DATABASE SNAPSHOT ===
Full Data Period: {db_data.get('date_range', 'N/A')}
Total Revenue (all-time): RM {db_data.get('total_rev', 0):,.2f}
Total Transactions (all-time): {db_data.get('total_txns', 0):,}
Overall Daily Average: RM {db_data.get('daily_avg', 0):,.2f}
Peak Transaction Hour: {db_data.get('peak_hour', 'N/A')}
Top Branch (all-time): {db_data.get('top_branch', 'N/A')}
"""