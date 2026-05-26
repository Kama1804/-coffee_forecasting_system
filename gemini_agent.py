from google import genai
from google.genai import errors
import os
import time
from dotenv import load_dotenv
from pathlib import Path
import sqlite3
import pandas as pd
from analytics import get_dashboard_metrics

load_dotenv(dotenv_path=Path(__file__).parent / '.env', override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Ensure initialization uses the correct, official new SDK instantiation pattern
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ============================================================
#    RESPONSE STYLE INSTRUCTION
# ============================================================

RESPONSE_STYLE_INSTRUCTION = """
RESPONSE FORMAT RULES — FOLLOW STRICTLY:
- Answer length must match question complexity:
  * Simple/factual question (e.g. "What's the top branch?") → 1–3 sentences MAX
  * Analysis question (e.g. "Why is revenue dropping?") → 3–5 bullet points or short paragraphs
  * Strategy/planning question (e.g. "Give me a full staffing plan") → structured with headers, up to 8 bullet points
  * Monthly breakdown question → provide table format with exact RM figures per branch

- NEVER pad with "Great question!", "Based on the data...", "In conclusion..."
- NEVER repeat the question back
- Lead with the most important insight FIRST
- Use RM values and exact numbers wherever possible
- If you recommend an action, state it in ONE clear sentence before the reason

CHART INSTRUCTION — READ CAREFULLY:
When the user asks for a chart, graph, visual, or comparison, you MUST output a chart data block.
The block format is EXACTLY this (no spaces, no markdown wrapping, no code fences):
[CHART_DATA={"type":"bar","labels":["Label1","Label2"],"values":[100,200],"title":"Chart Title"}]

Rules for the chart block:
1. Place it at the very END of your response on its own line
2. Do NOT wrap it in ```json or any markdown
3. Use "bar" for comparisons, "line" for trends over time, "horizontal" for product rankings
4. Values must be plain numbers (no RM prefix, no commas)
5. Always use real numbers from the database context above
"""


def get_ai_insight(prompt: str) -> tuple[bool, str]:
    """
    Core Gemini API call with automatic retry + model fallback using the official new SDK.
    Returns (success: bool, response_text: str)
    """
    if not client:
        return False, "System Error: Gemini API key is missing from the environment."

    full_prompt = RESPONSE_STYLE_INSTRUCTION + "\n\n" + prompt

    # 🟢 CHANGED: Pointing to the new high-allowance 500 RPD Model
    PRIMARY_MODEL  = "gemini-3.1-flash-lite"
    FALLBACK_MODEL = "gemini-2.5-flash-lite" # Safe secondary fallback alternative
    MAX_RETRIES    = 3
    BASE_DELAY     = 2

    def _call(model: str) -> str:
        # 🟢 SYNTAX FIX: New SDK uses client.models.generate_content instead of direct sub-paths
        response = client.models.generate_content(
            model=model,
            contents=full_prompt
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
            if attempt > 0:
                print(f"[GEMINI] Succeeded on attempt {attempt + 1} with {PRIMARY_MODEL}")
            return True, text

        except errors.APIError as e:
            error_msg = str(e).lower()
            last_error = str(e)

            if _is_auth_error(error_msg):
                return False, "AI Connection Error: Your API key is invalid or lacks permission. Check your .env file."

            if _is_retryable(error_msg) or "404" in error_msg:
                delay = BASE_DELAY * (2 ** attempt)
                print(f"[GEMINI] {PRIMARY_MODEL} returned error… retrying in {delay}s (attempt {attempt+1}/{MAX_RETRIES})")
                time.sleep(delay)
                continue

            return False, f"Gemini API Error: {str(e)}"

        except Exception as e:
            error_msg = str(e).lower()
            last_error = str(e)
            if "timeout" in error_msg:
                delay = BASE_DELAY * (2 ** attempt)
                print(f"[GEMINI] Timeout on attempt {attempt+1}, retrying in {delay}s")
                time.sleep(delay)
                continue
            return False, f"Connection Error: {str(e)}"

    # Fallback model processing layer
    print(f"[GEMINI] {PRIMARY_MODEL} exhausted or busy. Trying fallback: {FALLBACK_MODEL}…")
    for attempt in range(2):
        try:
            text = _call(FALLBACK_MODEL)
            print(f"[GEMINI] Fallback {FALLBACK_MODEL} succeeded.")
            return True, text + f"\n\n*(Answered by {FALLBACK_MODEL} — primary model path was resetting)*"

        except errors.APIError as e:
            error_msg = str(e).lower()
            if _is_auth_error(error_msg):
                return False, "AI Connection Error: API key is invalid. Check your .env file."
            delay = BASE_DELAY * (2 ** attempt)
            print(f"[GEMINI] Fallback attempt {attempt+1} failed, waiting {delay}s")
            time.sleep(delay)

        except Exception:
            time.sleep(BASE_DELAY)

    return False, (
        "⚡ Gemini AI is experiencing high demand right now. "
        "Please try again in 30–60 seconds. "
        f"(Last error: {last_error})"
    )


def get_business_advice(branch_id: int, branch_name: str) -> tuple[bool, str]:
    """
    Gathers database metrics and Prophet forecasts, generates operational advice.
    """
    print(f"Gathering data for {branch_name}...")

    metrics = get_dashboard_metrics(branch_id)
    if not metrics:
        return False, "Not enough historical data to generate advice."

    db_path = os.path.join('database', 'coffee_shop.db')
    conn = sqlite3.connect(db_path)
    forecast_df = pd.read_sql_query(
        f"SELECT forecast_date, predicted_revenue FROM sales_forecast "
        f"WHERE branch_id = {branch_id} "
        f"AND forecast_date > (SELECT COALESCE(MAX(sale_date), '1970-01-01') FROM sales_transaction) "
        f"ORDER BY forecast_date ASC LIMIT 7",
        conn
    )
    conn.close()

    if forecast_df.empty:
        return False, "No forecast data found. Please run Prophet engine first."

    forecast_text     = forecast_df.to_string(index=False)
    peak_hours_text   = ", ".join([f"{m['hour']} ({m['quantity_sold']} items)" for m in metrics['peak_hours'][:3]])
    top_products_text = ", ".join([f"{m['product_category']} (RM {m['total_revenue']})" for m in metrics['product_mix'][:3]])

    system_prompt = f"""
You are an expert AI Business Advisor for 'Mini Coffee Shop' in {branch_name}, Malaysia.
Analyze the data below and give EXACTLY 3 actionable recommendations — one for Staffing, one for Inventory, one for Revenue Opportunity.

UPCOMING 7-DAY FORECAST:
{forecast_text}

PEAK HOURS: {peak_hours_text}
TOP PRODUCTS: {top_products_text}

FORMAT: Use this exact structure:
**Staffing:** [1 sentence action] — [1 sentence reason with specific data]
**Inventory:** [1 sentence action] — [1 sentence reason with specific data]
**Revenue Opportunity:** [1 sentence action] — [1 sentence reason with specific data]

Do NOT write any introduction or conclusion. Start immediately with **Staffing:**.
"""

    print("Sending context to Gemini AI...")
    return get_ai_insight(system_prompt)


# ============================================================
#    CHATBOT SYSTEM CONTEXT BUILDER
# ============================================================

def build_chat_system_context(db_data: dict) -> str:
    """
    Builds the system context block prepended to every chat message.
    """
    return f"""You are the AI Business Advisor for 'Mini Coffee Shop' — a Malaysian coffee chain with branches in Putrajaya and Puncak Alam.
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

=== 7-DAY PROPHET FORECAST ===
{db_data.get('forecast_summary', 'No forecast generated yet.')}

=== CHART DATA ARRAYS (use these exact numbers for chart blocks) ===
Branch names: {db_data.get('arr_branches', [])}
Branch all-time revenues: {db_data.get('arr_branch_revs', [])}
Top product names: {db_data.get('arr_products', [])}
Top product revenues: {db_data.get('arr_product_revs', [])}

=== BEHAVIOUR RULES ===
1. Malaysian context only — use "RM" not "$". Reference "Raya", "cuti umum" where relevant.
2. Reply in the SAME language as the user (English, Bahasa Melayu, or Manglish/Rojak).
3. Time period mapping:
   - "last month" → use LAST MONTH BRANCH COMPARISON section
   - "this month" → use CURRENT MONTH SO FAR section
   - "YYYY-MM" or month+year format → extract from MONTHLY REVENUE TREND section
   - "compare branches" → use LAST MONTH first, note all-time gap
4. Classify intent before answering:
   - LOOKUP: specific number → 1–3 sentences with exact RM figures
   - COMPARISON: two branches or periods → side-by-side with % difference
   - ANALYSIS: "why" questions → 3–5 insight bullets with data evidence
   - RECOMMENDATION: "should I / what should I do" → 1 clear action, then reason
   - MONTHLY BREAKDOWN: specific month requested → full table: branch | revenue | transactions | daily avg
5. For branch comparisons ALWAYS include: revenue, transactions, daily average, and the revenue gap.
6. Never give vague answers. Every claim needs a number from the data above.
7. For monthly breakdown requests: always show both branches side by side in a markdown table.
"""


# ============================================================
#    TEST BLOCK
# ============================================================
if __name__ == "__main__":
    print("Testing Gemini AI Connection with New SDK & 3.1 Flash Lite model...\n")
    success, result = get_ai_insight(
        "Hello! Respond with exactly: 'AI Advisor online. Ready to analyze Mini Coffee Shop data.'"
    )
    if success:
        print(f"GEMINI SAYS: {result}")
    else:
        print(f"FAILED: {result}")