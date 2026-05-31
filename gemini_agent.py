from google import genai
from google.genai import errors, types
import os
import time
import json
from dotenv import load_dotenv
from pathlib import Path
import sqlite3
import pandas as pd
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


def fast_kpi_bypass(user_message: str, db_data: dict):
    """Bypass Gemini for simple KPI questions to achieve < 1s response."""
    msg = user_message.lower().strip()
    
    if msg in ["total revenue", "what is the total revenue?", "what is the total revenue", "revenue"]:
        return f"The all-time total revenue is RM {db_data.get('total_rev', 0):,.2f}."
    if msg in ["total transactions", "what are the total transactions?", "transactions"]:
        return f"There have been {db_data.get('total_txns', 0):,} total transactions all-time."
    if "daily average" in msg or msg == "average daily revenue":
        return f"The overall daily average revenue is RM {db_data.get('daily_avg', 0):,.2f}."
    if msg in ["top branch", "what is the top branch?", "best branch"]:
        return f"The top branch all-time is {db_data.get('top_branch', 'N/A')}."
    if "peak hour" in msg or msg == "busiest hour":
        return f"The overall peak transaction hour is {db_data.get('peak_hour', 'N/A')}."
    if "payday" in msg and "status" in msg:
        return f"Payday Cycle Status: {db_data.get('payday_context', 'Standard operating period')}."
    
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


def build_chat_system_context(db_data: dict) -> str:
    """Builds the comprehensive system context block prepended to chat instances."""
    return f"""You are the AI Business Advisor for 'Mini Coffee Shop' — a Malaysian mobile coffee network operating Putrajaya and Puncak Alam branches.
You have REAL-TIME access to the full sales database below. All numbers are live and accurate.
NEVER say "I don't have access to that data" — the data IS in this prompt. USE IT.

=== LIVE DATABASE SNAPSHOT ===
Full Data Period: {db_data.get('date_range', 'N/A')}
Total Revenue (all-time): RM {db_data.get('total_rev', 0):,.2f}
Total Transactions (all-time): {db_data.get('total_txns', 0):,}
Overall Daily Average: RM {db_data.get('daily_avg', 0):,.2f}
Peak Transaction Hour: {db_data.get('peak_hour', 'N/A')}
Top Branch (all-time): {db_data.get('top_branch', 'N/A')}
Payday Cycle Status: {db_data.get('payday_context', 'Standard operating period')}

=== ALL-TIME BRANCH COMPARISON ===
{db_data.get('branch_summary', 'No data')}

=== LAST MONTH BRANCH COMPARISON ({db_data.get('last_mo_label', 'Last Month')}) ===
{db_data.get('last_mo_branch_summary', 'No data for last month.')}

=== CURRENT MONTH SO FAR ({db_data.get('curr_mo_label', 'This Month')}) ===
{db_data.get('curr_mo_branch_summary', 'No data yet this month.')}

=== MONTHLY REVENUE TREND — LAST 6 MONTHS (per branch) ===
{db_data.get('monthly_trend_summary', 'No trend data.')}

=== TOP 3 PRODUCTS (by volume, all-time) ===
{db_data.get('top_products', 'No data')}

=== CATEGORY REVENUE (all-time) ===
{db_data.get('categories', 'No data')}

=== WEATHER IMPACT (avg daily revenue by condition) ===
{db_data.get('weather_summary', 'No data')}

=== 5-DAY PROPHET FORECAST ===
{db_data.get('forecast_summary', 'No forecast generated yet.')}

=== CHART DATA ARRAYS ===
Branch names: {json.dumps(db_data.get('arr_branches', []))}
Branch all-time revenues: {json.dumps(db_data.get('arr_branch_revs', []))}
Top product names: {json.dumps(db_data.get('arr_products', []))}
Top product revenues: {json.dumps(db_data.get('arr_product_revs', []))}

=== BEHAVIOUR RULES ===
1. Malaysian context only — use "RM" not "$". Reference festive operations (CNY, Raya) where relevant.
2. Reply in the SAME language as the user (English, Bahasa Melayu, or Rojak/Manglish mixtures).
3. Time period mapping:
   - "last month" → use LAST MONTH BRANCH COMPARISON section
   - "this month" → use CURRENT MONTH SO FAR section
   - "compare branches" → use LAST MONTH first, note all-time gap
4. Classify intent before answering:
   - LOOKUP: 1–3 sentences with exact RM figures.
   - COMPARISON: side-by-side with % difference profiles.
   - ANALYSIS: 3–5 insight bullets with database evidence.
   - RECOMMENDATION: 1 clear operational action, then data reason.
   - MONTHLY BREAKDOWN: markdown table displaying branch | revenue | transactions | daily avg.
5. Every business claim needs a supporting number from the metrics data snapshot above.
"""


def build_slim_context(db_data: dict, user_message: str) -> str:
    """Analytical Router: Inject pre-calculated metrics eliminating redundant DB connections."""
    from analytics import multi_month_chart_pre_packager
    msg = user_message.lower()

    base = f"""You are the AI Business Advisor for 'Mini Coffee Shop' (Malaysia). 
BE BLAZINGLY FAST. Use bullet points. No conversational filler.
Data period: {db_data.get('date_range', 'N/A')} | All-time revenue: RM {db_data.get('total_rev', 0):,.2f}"""

    sections = [base]

    if any(k in msg for k in ['staff', 'people', 'worker', 'shift', 'peak', 'busy', 'hour', 'time']):
        peaks = db_data.get('branch_peaks', {})
        sections.append(f"=== PEAK HOURS (Top 3 per branch) ===\n- Puncak Alam (FT-PA1): {', '.join(peaks.get('FT-PA1', ['N/A']))}\n- Putrajaya (STB-PJ1): {', '.join(peaks.get('STB-PJ1', ['N/A']))}")

    if any(k in msg for k in ['ingredient', 'inventory', 'stock', 'beans', 'milk', 'ice', 'cup', 'demand', 'order']):
        sections.append(f"=== RECENT PRODUCT CATEGORY PERFORMANCE ===\n{db_data.get('categories', 'No data')}")

    if any(k in msg for k in ['trend', 'month', 'growth', 'decline', 'revenue', 'year', 'last', 'compare', 'vs']):
        sections.append(f"=== MONTHLY TREND (Last 6 Months) ===\n{db_data.get('monthly_trend_summary', 'No trend data')}")

    if any(k in msg for k in ['chart', 'graph', 'visual', 'plot', 'draw', 'show chart']):
        m_count = 3 
        import re
        m_match = re.search(r'last (\d+) month', msg)
        if m_match: m_count = int(m_match.group(1))
        chart_data = multi_month_chart_pre_packager(months=m_count)
        sections.append(f"=== CHART DATA (READY-TO-USE) ===\n[CHART_DATA={chart_data}]")

    return "\n\n".join(sections)