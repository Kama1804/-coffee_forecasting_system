from google import genai
from google.genai import errors, types
import os
import time
import json
import re
import random
from dotenv import load_dotenv
from pathlib import Path
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from analytics import get_dashboard_metrics

load_dotenv(dotenv_path=Path(__file__).parent / '.env', override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ============================================================
#   INTENT CLASSIFIER — detects what the user actually wants
# ============================================================

INTENT_PROFILES = {
    "trend_analysis": {
        "keywords": ["trend", "growth", "decline", "compare", "vs", "month", "year", "last", "202", "performance over", "how has", "ramadhan", "puasa", "raya", "holiday"],
        "style": "analytical_narrative",
        "depth": "deep",
    },
    "promo_intelligence": {
        "keywords": ["promo", "promotion", "discount", "deal", "campaign", "b1f1", "buy one", "voucher", "efficiency", "impact"],
        "style": "warning_driven",
        "depth": "medium",
    },
    "forecast": {
        "keywords": ["forecast", "predict", "next week", "next month", "expect", "ramalan", "will", "upcoming", "projection"],
        "style": "forward_looking",
        "depth": "deep",
    },
    "inventory": {
        "keywords": ["ingredient", "stock", "inventory", "milk", "cup", "beans", "ice", "order", "supply", "demand", "restock", "stok"],
        "style": "operational",
        "depth": "medium",
    },
    "staffing": {
        "keywords": ["staff", "worker", "shift", "schedule", "peak", "busy", "rush hour", "headcount", "manpower"],
        "style": "operational",
        "depth": "medium",
    },
    "quick_kpi": {
        "keywords": ["total revenue", "how much", "transaction", "daily average", "top branch", "peak hour", "busiest"],
        "style": "direct_answer",
        "depth": "shallow",
    },
    "chart_request": {
        "keywords": ["chart", "graph", "plot", "visual", "visualize", "draw", "show me a chart"],
        "style": "chart_with_insight",
        "depth": "medium",
    },
}

def classify_intent(user_message: str) -> dict:
    """
    Scores user message against intent profiles.
    Returns the best-matching intent + style metadata.
    Supports multi-intent (e.g. forecast + chart).
    """
    msg = user_message.lower()
    scores = {}
    for intent, profile in INTENT_PROFILES.items():
        hit = sum(1 for kw in profile["keywords"] if kw in msg)
        if hit > 0:
            scores[intent] = hit

    if not scores:
        return {"primary": "general", "style": "conversational", "depth": "medium", "multi": []}

    sorted_intents = sorted(scores, key=scores.get, reverse=True)
    primary = sorted_intents[0]
    multi = sorted_intents[1:3]  # up to 2 secondary intents

    return {
        "primary": primary,
        "style": INTENT_PROFILES[primary]["style"],
        "depth": INTENT_PROFILES[primary]["depth"],
        "multi": multi,
    }


# ============================================================
#   DYNAMIC SYSTEM PROMPT BUILDER — no more single template
# ============================================================

_RAMADHAN_WINDOWS = [
    ("2024-03-12", "2024-04-09"),
    ("2025-03-02", "2025-03-30"),
    ("2026-02-19", "2026-03-20"),
    ("2027-02-08", "2027-03-09"),
]

def _is_ramadhan(date: datetime = None) -> bool:
    d = (date or datetime.now()).strftime("%Y-%m-%d")
    return any(start <= d <= end for start, end in _RAMADHAN_WINDOWS)

def _get_ramadhan_note() -> str:
    if _is_ramadhan():
        return (
            "\n⚠️ RAMADHAN MODE ACTIVE: Business hours shift to NIGHT. "
            "Sales begin ~4:30 PM, peak ~9:00 PM. All staffing and inventory advice must reflect night-shift planning."
        )
    return ""


STYLE_PERSONAS = {
    "analytical_narrative": """
You are a sharp Malaysian F&B business analyst — think McKinsey meets hawker stall owner.
When analyzing trends, tell a STORY: what changed, why it likely changed, and what to do about it.
For seasonal comparisons: highlight growth/decline across years and interpret what this means for the business's maturity.
Lead with the most surprising or important finding. Use comparative framing ("X% higher than", "best since").
""",
    "warning_driven": """
You are a vigilant ops manager who catches problems before they hurt the business.
For promo analysis: ALWAYS check the 'Worth It' score. If ROI is < 5x, issue a ⚠️ warning about high burn.
Volume promos → immediately warn that ingredient consumption (cups, milk, ice) will spike disproportionately vs revenue.
Be direct. Don't soften the message.
""",
    "forward_looking": """
You are a forecasting advisor. Follow this internal logic (Chain-of-Thought):
1. Identify the specific date the user is asking about.
2. Verify the exact revenue forecast and weather for that date.
3. Reason how the weather and day-of-week (e.g., weekend rush vs weekday slump) affect volume.
4. Recommend a concrete action tied to that specific day.
Closed days (Sunday) = acknowledge briefly, move on.
""",
    "operational": """
You are a hands-on operations coach. Follow this internal logic (Chain-of-Thought):
1. Look at the [INVENTORY TRUTH] hard numbers provided in the context.
2. Convert those numbers into actionable items (e.g., "Order 2 cartons of milk" instead of "30,000ml").
3. Use urgency flags based on the predicted volume: 🔴 Critical (act today), 🟡 Watch (act this week), 🟢 Stable.
NEVER estimate or guess numbers — use the exact L and kg from the context.
""",
    "direct_answer": """
You are a fast-response data terminal. Answer the specific question in 1-2 sentences.
No preamble. No filler. If the user asks for one number, give one number with minimal framing.
""",
    "chart_with_insight": """
You are a data visualization advisor. Generate the chart payload AND a 2-3 sentence insight that explains what the chart reveals — not just what it shows.
The insight should answer: "What does this tell us that we couldn't see from a table?"
""",
    "conversational": """
You are a knowledgeable but approachable business advisor for a Malaysian coffee business.
Match your response style to the question: casual questions get conversational answers, analytical questions get structured breakdowns.
Never be robotic. Vary your sentence structure. Avoid starting every response the same way.
""",
}

ABSOLUTE_RULES = """
=== NON-NEGOTIABLE RULES ===
- CHART GATE: Only produce [CHART_DATA=...] if the user used the words: "chart", "graph", "plot", "visual", "visualize", or "draw". NEVER produce chart payloads otherwise.
- NEVER output raw JSON outside of [CHART_DATA={{...}}] wrappers.
- NEVER repeat the same number twice in one response.
- NEVER start your response with "Sure!", "Great question!", "Certainly!", or any filler opener.
- NEVER use the same response structure twice in a row — vary your format.
- If data is unavailable, say so honestly in one sentence and pivot to what IS available.
- If the user says "thank you", "terima kasih", or "bye", just say a polite, friendly closing in 1 sentence.
- For ROI analysis: If multiplier > 8x, say "WORTH IT". If < 5x, say "NOT WORTH IT (High Burn)".
- LANGUAGE MIRRORING: Always respond in the SAME language used by the user. If they ask in Malay, answer in Malay. If in English, answer in English. Maintain a professional Malaysian business tone.
- Ramadhan context: {ramadhan_note}
"""

RESPONSE_LENGTH_GUIDE = {
    "shallow": "Max 60 words. One punchy answer.",
    "medium": "80–150 words. Key insight + 2-3 supporting points.",
    "deep": "150–280 words. Full analysis with narrative, data points, and at least one actionable recommendation.",
}

def is_closing_statement(text: str) -> bool:
    msg = text.lower().strip()
    return any(k in msg for k in ["thank you", "terima kasih", "bye", "goodbye", "that's all", "nothing else"])

def build_dynamic_system_prompt(intent: dict) -> str:
    style = intent.get("style", "conversational")
    depth = intent.get("depth", "medium")
    persona = STYLE_PERSONAS.get(style, STYLE_PERSONAS["conversational"])
    length_guide = RESPONSE_LENGTH_GUIDE.get(depth, RESPONSE_LENGTH_GUIDE["medium"])
    ramadhan_note = _get_ramadhan_note() or "Not currently Ramadhan. Use standard peak hour (~10:00 AM)."

    # Dynamic branch context
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT branch_name, branch_code FROM branch WHERE is_active = 1")
        active_branches = [f"{r[0]} ({r[1]})" for r in cursor.fetchall()]
        conn.close()
        branch_ctx = ", ".join(active_branches) if active_branches else "all active locations"
    except Exception:
        branch_ctx = "all active locations"

    rules = ABSOLUTE_RULES.format(ramadhan_note=ramadhan_note)

    # Multi-intent supplemental guidance
    supplemental = ""
    multi = intent.get("multi", [])
    if "promo_intelligence" in multi:
        supplemental += "\nSECONDARY LENS — Promo: flag any Volume-based promotion impacts and check the 'Worth It' ROI status."
    if "forecast" in multi:
        supplemental += "\nSECONDARY LENS — Forecast: briefly tie current trend to what's coming next."
    if "inventory" in multi:
        supplemental += "\nSECONDARY LENS — Inventory: connect any demand signal to ingredient stocking action."

    return f"""You are the AI Business Advisor for 'Mini Coffee Shop' — operating in {branch_ctx}.

{persona}

=== RESPONSE LENGTH ===
{length_guide}

{rules}
{supplemental}

=== OUTPUT FORMAT GUIDE ===
- analytical_narrative → prose paragraphs, no bullet lists
- warning_driven → short paragraphs + ⚠️ callout blocks
- forward_looking → brief table or date-by-date breakdown + recommendation paragraph
- operational → urgency-flagged bullet list (🔴🟡🟢) + one summary sentence
- direct_answer → plain sentence(s), no formatting
- chart_with_insight → chart payload + 2-3 sentence narrative insight
- conversational → match tone to the question; mix formats freely
"""


# ============================================================
#   IN-MEMORY CACHING SNAPSHOT
# ============================================================

GLOBAL_CHAT_CACHE = {
    "payload_dict": None,
    "expiry_timestamp": 0
}
CACHE_TTL_SECONDS = 300
_CHART_KEYWORDS = {"chart", "graph", "plot", "visual", "visualize", "draw"}


def get_db_connection():
    db_path = os.path.join('database', 'coffee_shop.db')
    return sqlite3.connect(db_path)


# ============================================================
#   FAST KPI BYPASS — sub-second responses for simple lookups
# ============================================================

def fast_kpi_bypass(user_message: str, db_data: dict):
    """
    Regex-based bypass for single-metric lookups.
    Returns a varied, natural-language response (not a template string).
    Responses rotate phrasing to avoid feeling robotic.
    """
    if is_closing_statement(user_message):
        return random.choice([
            "Sama-sama, Boss. Semoga jualan esok lebat!",
            "Terima kasih kembali. Saya sentiasa di sini kalau ada soalan data.",
            "All the best for the next shift! Jumpa lagi.",
            "No problem. Let's hit those targets!"
        ])

    msg = user_message.lower().strip()
    has_temporal = any(k in msg for k in ['last', 'this', 'month', 'compare', 'trend', '202'])

    # Phrasing variants so the same question never gets the exact same answer twice
    rev = db_data.get('total_rev', 0)
    txns = db_data.get('total_txns', 0)
    daily = db_data.get('daily_avg', 0)
    top_branch = db_data.get('top_branch', 'N/A')
    peak = db_data.get('peak_hour', 'N/A')

    if re.search(r'\b(total revenue|how much did we make|all.?time revenue)\b', msg) and not has_temporal:
        return f"Across all active branches all-time, the network has brought in RM {rev:,.2f} in total revenue."

    if re.search(r'\b(total transactions|how many (tickets|orders|receipts)|transaction count)\b', msg) and not has_temporal:
        return f"All-time transaction count stands at {txns:,} — that's every order recorded in the database."

    if re.search(r'\b(daily average|average daily revenue|average.?per day)\b', msg):
        return f"The historical daily revenue baseline is RM {daily:,.2f} per operating day."

    if re.search(r'\b(top branch|best (branch|location)|highest revenue|busiest location)\b', msg):
        return f"{top_branch} leads on cumulative revenue — it's the strongest performer across the network historically."

    if re.search(r'\b(peak hour|busiest (hour|time)|rush hour|when.?busiest)\b', msg):
        return f"Transactions peak at the {peak}:00 slot — that's when the counter is at max capacity."

    return None


# ============================================================
#   TOKEN BUDGET RESOLVER
# ============================================================

def _resolve_token_budget(prompt: str, intent: dict, override: int | None) -> int:
    if override is not None:
        return override
    depth = intent.get("depth", "medium")
    return {"shallow": 200, "medium": 500, "deep": 900}.get(depth, 500)


# ============================================================
#   CORE GEMINI API CALL
# ============================================================

def get_ai_insight(prompt: str, max_tokens: int = None, intent: dict = None) -> tuple[bool, str]:
    """
    Core Gemini API call with dynamic system prompting.
    Intent dict drives persona, depth, and formatting style.
    """
    if not client:
        return False, "System Error: Gemini API key is missing from the environment."

    if intent is None:
        intent = classify_intent(prompt)

    system_prompt = build_dynamic_system_prompt(intent)
    full_prompt = system_prompt + "\n\n=== USER QUERY ===\n" + prompt
    token_budget = _resolve_token_budget(prompt, intent, max_tokens)

    PRIMARY_MODEL = "gemini-3.1-flash-lite"
    MAX_RETRIES = 1
    BASE_DELAY = 1

    def _call(model: str) -> str:
        response = client.models.generate_content(
            model=model,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=token_budget,
                temperature=0.55,   # Raised from 0.3 → more varied, less robotic
            )
        )
        return response.text.strip()

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            text = _call(PRIMARY_MODEL)
            return True, text
        except Exception as e:
            last_error = str(e)
            time.sleep(BASE_DELAY * (2 ** attempt))

    return False, f"AI Insight Engine currently unavailable. Error: {last_error}"


# ============================================================
#   STREAMING GEMINI API CALL
# ============================================================

def stream_ai_insight(prompt: str, intent: dict = None):
    """Generator yielding SSE-formatted text chunks from Gemini."""
    if not client:
        yield f"data: {json.dumps({'error': 'Gemini API key missing.'})}\n\n"
        return

    if intent is None:
        intent = classify_intent(prompt)

    system_prompt = build_dynamic_system_prompt(intent)
    full_prompt = system_prompt + "\n\n=== USER QUERY ===\n" + prompt
    token_budget = _resolve_token_budget(prompt, intent, None)
    PRIMARY_MODEL = "gemini-3.1-flash-lite"

    try:
        response = client.models.generate_content_stream(
            model=PRIMARY_MODEL,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=max(token_budget, 200),
                temperature=0.55,
            )
        )
        for chunk in response:
            text = getattr(chunk, "text", "") or ""
            if text:
                yield f"data: {json.dumps({'chunk': text})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    finally:
        try:
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception:
            pass


# ============================================================
#   BUSINESS ADVICE — per-branch operational recommendations
# ============================================================

def get_business_advice(branch_id: str, branch_name: str) -> tuple[bool, str]:
    """
    Gathers denormalized metrics + Prophet forecasts and generates
    3 targeted operational recommendations with dynamic framing.
    """
    branch_id = str(branch_id).upper().strip()
    metrics = get_dashboard_metrics(branch_id)
    if not metrics:
        return False, "Not enough historical data to generate advice for this branch."

    db_path = os.path.join('database', 'coffee_shop.db')
    conn = sqlite3.connect(db_path)

    forecast_df = pd.read_sql_query(
        """SELECT forecast_date, predicted_revenue FROM sales_forecast
           WHERE branch_id = ?
           AND forecast_date > (SELECT COALESCE(MAX(transaction_date), '1970-01-01') FROM sales_transaction)
           ORDER BY forecast_date ASC LIMIT 5""",
        conn, params=(branch_id,)
    )

    # Promo intelligence — differentiate value vs volume promos
    promo_info = ""
    promo_warning = ""
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT item_name, promo_code, SUM(transaction_qty) as q, SUM(Total_Bill_MYR) as r
            FROM sales_transaction
            WHERE branch_id = ? AND promo_code != 'NONE'
            GROUP BY item_name, promo_code ORDER BY q DESC LIMIT 5
        """, (branch_id,))
        top_promos = cursor.fetchall()
        if top_promos:
            lines = []
            for p in top_promos:
                ratio = p[3] / p[2] if p[2] > 0 else 0
                is_volume = any(v in p[1].upper() for v in ["B1F1", "BUY1", "QTY", "2FOR", "FREE"])
                flag = " ⚠️ VOLUME PROMO — HIGH ingredient draw" if is_volume else " (Value %)"
                lines.append(f"  - {p[0]} [{p[1]}]: {p[2]} units, RM {p[3]:,.2f} (RM {ratio:.2f}/unit){flag}")
            promo_info = "RECENT PROMO BREAKDOWN:\n" + "\n".join(lines)
            if any("VOLUME PROMO" in l for l in lines):
                promo_warning = (
                    "⚠️ INVENTORY ALERT: One or more active promotions are volume-based. "
                    "Cup and milk consumption will significantly outpace revenue projections. "
                    "Increase perishable stock order accordingly."
                )
    except Exception:
        pass
    conn.close()

    if forecast_df.empty:
        return False, "No forecast data found. Please run the Prophet engine first."

    forecast_text = forecast_df.to_string(index=False)
    peak_hours_text = ", ".join([f"{m['hour']}:00 ({m['quantity_sold']} items)" for m in metrics['peak_hours'][:3]])
    top_products_text = ", ".join([f"{m['product_category']} (RM {m['total_revenue']})" for m in metrics['product_mix'][:3]])
    ramadhan_note = _get_ramadhan_note()

    intent = {"primary": "forecast", "style": "operational", "depth": "deep", "multi": ["inventory", "staffing"]}
    system_prompt = build_dynamic_system_prompt(intent)

    advice_prompt = f"""
{system_prompt}

You are advising the owner of Mini Coffee Shop — {branch_name} branch.
Generate EXACTLY 3 recommendations, one each for Staffing, Inventory, and Revenue Opportunity.
Each recommendation must be SPECIFIC to the data below — no generic advice.

UPCOMING 5-DAY FORECAST:
{forecast_text}

PEAK HOURS: {peak_hours_text}
TOP PRODUCTS: {top_products_text}
{promo_info}
{promo_warning}
{ramadhan_note}

TONE RULES:
- Sound like an experienced ops manager, not a chatbot.
- Tie every recommendation directly to a data point.
- Use concrete numbers where possible ("order 20L extra", "add 1 staff at 9 PM").
- Do NOT write any intro or conclusion. Start immediately with the first recommendation.

FORMAT (use exactly):
**Staffing:** [action] — [data-backed reason]
**Inventory:** [action] — [data-backed reason]
**Revenue Opportunity:** [action] — [data-backed reason]
"""
    return get_ai_insight(advice_prompt, intent=intent)


# ============================================================
#   SLIM CONTEXT BUILDER — analytical router for chat queries
# ============================================================

def build_slim_context(db_data: dict, user_message: str) -> str:
    """
    Builds a data-rich context payload for the AI, shaped by detected intent.
    Only injects data sections that are relevant to the user's actual question.
    """
    from analytics import (
        multi_month_chart_pre_packager, promo_efficiency_analyzer, 
        calculate_ingredient_demand, format_to_ops_units,
        get_promo_roi_report, get_seasonal_comparison_report
    )
    from forecast_engine import ForecastEngine

    intent = classify_intent(user_message)
    msg = user_message.lower()

    # ── SEMANTIC DAY FILTERING (Upgrade 1) ──────────────────
    target_date_str = None
    if "esok" in msg or "tomorrow" in msg:
        target_date_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    elif "today" in msg or "hari ini" in msg:
        target_date_str = datetime.now().strftime("%Y-%m-%d")

    # Base context
    base = f"""DATA SNAPSHOT (as of query time):
    - Period: {db_data.get('date_range', 'N/A')}
    - All-time revenue: RM {db_data.get('total_rev', 0):,.2f}
    - All-time transactions: {db_data.get('total_txns', 0):,}
    - Daily avg: RM {db_data.get('daily_avg', 0):,.2f}
    - Peak hour: {db_data.get('peak_hour', 'N/A')}
    - Top branch: {db_data.get('top_branch', 'N/A')}"""

    sections = [base]

    # ── MULTI-YEAR SEASONAL COMPARISON (NEW) ────────────────
    if "ramadhan" in msg or "puasa" in msg:
        years_found = re.findall(r'\b(20\d{2})\b', msg)
        if not years_found: years_found = ["2025", "2026"] # Default comparison
        
        seasonal_data = get_seasonal_comparison_report('ramadhan', years_found)
        if seasonal_data:
            lines = [f"=== RAMADHAN YEAR-OVER-YEAR ({', '.join(years_found)}) ==="]
            for r in seasonal_data:
                lines.append(f"  Year {r['year']}: RM {r['revenue']:,.2f} total | Avg RM {r['daily_avg']:,.2f}/day | {r['transactions']:,} orders")
            sections.append("\n".join(lines))

    # ── MULTI-YEAR CAMPAIGN ROI (NEW) ───────────────────────
    if "promo" in msg or "campaign" in msg or "discount" in msg:
        # Detect if user is asking for a specific campaign like "Post Raya"
        campaign_match = re.search(r'campaign ([\w\s]+) last', msg) or re.search(r'discount for ([\w\s]+) last', msg)
        fuzzy_name = campaign_match.group(1) if campaign_match else None
        
        if fuzzy_name:
            roi_data = get_promo_roi_report(fuzzy_name)
            if roi_data:
                lines = [f"=== MULTI-YEAR ROI: {fuzzy_name.upper()} ==="]
                for r in roi_data:
                    lines.append(
                        f"  [{r['yr']}] {r['promo_code']}: RM {r['total_net']:,.2f} Net | "
                        f"Multiplier: {r['roi_multiplier']}x | {r['worth_it_score']}"
                    )
                sections.append("\n".join(lines))

    # ── FORECAST + PRECISION INVENTORY (Upgrade 2) ──────────
    if intent["primary"] in ("forecast", "inventory", "operational") or any(k in msg for k in ["forecast", "predict", "week", "ramalan", "weather", "stok", "stock"]):
        try:
            engine = ForecastEngine()
            
            # Dynamic branch retrieval
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT branch_code, branch_name FROM branch WHERE is_active = 1")
            active_branches = cursor.fetchall()
            conn.close()

            all_forecasts_text = []
            combined_items = []

            for b_code, b_name in active_branches:
                success, fc_result = engine.generate_5_day_forecast(b_code, b_name)
                if success:
                    fc_list = fc_result.get('forecast', [])
                    
                    # Filter by date if applicable
                    if target_date_str:
                        fc_list = [d for d in fc_list if d['ds'] == target_date_str]

                    def _fmt_days(inner_list):
                        lines = []
                        for d in inner_list:
                            dt = datetime.strptime(d['ds'], '%Y-%m-%d')
                            if d.get('is_closed'):
                                lines.append(f"  {d['ds']} ({dt:%A}): CLOSED")
                            else:
                                w = d.get('weather') or {}
                                promos = ", ".join(d.get('promotions', [])) or "none"
                                lines.append(
                                    f"  {d['ds']} ({dt:%A}): RM {d['yhat']:,.2f} | "
                                    f"{w.get('temp', 28)}°C {w.get('label','Cloudy')} | Promos: {promos}"
                                )
                        return "\n".join(lines)

                    all_forecasts_text.append(f"{b_name} ({b_code}):\n{_fmt_days(fc_list)}")
                    for d in fc_list:
                        combined_items.extend(d.get('predicted_items', []))

            if target_date_str:
                sections.append(f"TARGET DATE DETECTED: {target_date_str}")
            
            if all_forecasts_text:
                sections.append(f"=== FORECAST DATA ===\n" + "\n\n".join(all_forecasts_text))

            # ── INVENTORY TRUTH (Upgrade 2) ──
            if combined_items:
                raw_demand = calculate_ingredient_demand(combined_items)
                ops_demand = format_to_ops_units(raw_demand)
                sections.append(f"=== [INVENTORY TRUTH] CALCULATED DEMAND ===\n{json.dumps(ops_demand, indent=2)}")

        except Exception as e:
            sections.append(f"=== FORECAST ===\nError: {e}")

    # ── PROMO INTELLIGENCE ──────────────────────────────────
    if intent["primary"] == "promo_intelligence" or "promo_intelligence" in intent["multi"]:
        efficiency = promo_efficiency_analyzer()
        if efficiency:
            promo_lines = []
            for p in efficiency:
                is_volume = p.get("promo_type", "").upper() in ["B1F1", "VOLUME", "QTY"]
                flag = " ← ⚠️ VOLUME: high ingredient burn" if is_volume else " ← Value-based (revenue-safe)"
                promo_lines.append(
                    f"  [{p['promo_code']}] {p['promo_type']}: "
                    f"Discount ratio {p['discount_ratio']:.1%}, {p['total_qty']} units{flag}"
                )
            sections.append("=== PROMOTION INTELLIGENCE ===\n" + "\n".join(promo_lines))

    # ── TARGETED MONTHLY BREAKDOWN ──────────────────────────
    month_match = re.search(r'\b(20\d{2}-\d{2})\b', user_message)
    if month_match:
        target_month = month_match.group(1)
        try:
            # ── Speed Opt: Define date range for Index usage ──
            start_date = f"{target_month}-01"
            # Calculate end of month
            y, m = map(int, target_month.split('-'))
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            end_date = f"{target_month}-{last_day}"

            conn = sqlite3.connect(os.path.join('database', 'coffee_shop.db'))
            cursor = conn.cursor()

            # 1. Total Metrics (Range Query)
            cursor.execute("""
                SELECT COALESCE(SUM(Total_Bill_MYR),0), COUNT(transaction_id), COUNT(DISTINCT transaction_date)
                FROM sales_transaction WHERE transaction_date BETWEEN ? AND ?
            """, (start_date, end_date))
            m_rev, m_txns, m_days = cursor.fetchone()
            m_days = m_days or 1

            # 2. Branch performance
            cursor.execute("""
                SELECT store_location, COALESCE(SUM(Total_Bill_MYR),0), COUNT(transaction_id)
                FROM sales_transaction WHERE transaction_date BETWEEN ? AND ?
                GROUP BY store_location
            """, (start_date, end_date))
            branches = [f"  {r[0]}: RM {r[1]:,.2f} | {r[2]:,} txns | ATV RM {r[1]/r[2] if r[2]>0 else 0:.2f}" for r in cursor.fetchall()]

            # 3. Top Items
            cursor.execute("""
                SELECT item_name, SUM(transaction_qty) as q FROM sales_transaction
                WHERE transaction_date BETWEEN ? AND ?
                GROUP BY item_name ORDER BY q DESC LIMIT 5
            """, (start_date, end_date))
            top_items = [f"  {r[0]}: {r[1]:,} units" for r in cursor.fetchall()]

            # 4. Peaks (Consolidated)
            cursor.execute("""
                SELECT Hour, "Day Name" FROM sales_transaction
                WHERE transaction_date BETWEEN ? AND ?
            """, (start_date, end_date))
            # We fetch raw then use python for peak finding to avoid more DB roundtrips if data is small,
            # but for 17k rows, 2 specific sub-queries are better.
            cursor.execute("""SELECT Hour FROM sales_transaction WHERE transaction_date BETWEEN ? AND ? GROUP BY Hour ORDER BY COUNT(*) DESC LIMIT 1""", (start_date, end_date))
            peak_hr = f"{cursor.fetchone()[0]}:00" if cursor.rowcount != 0 else "N/A"
            cursor.execute("""SELECT "Day Name" FROM sales_transaction WHERE transaction_date BETWEEN ? AND ? GROUP BY "Day Name" ORDER BY COUNT(*) DESC LIMIT 1""", (start_date, end_date))
            busy_day = cursor.fetchone()[0] if cursor.rowcount != 0 else "N/A"

            # 5. Growth vs Previous Month (Range Query)
            prev_dt = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=1)
            p_y, p_m = prev_dt.year, prev_dt.month
            p_start = f"{p_y:04d}-{p_m:02d}-01"
            p_end = prev_dt.strftime("%Y-%m-%d")
            
            cursor.execute("SELECT COALESCE(SUM(Total_Bill_MYR),0) FROM sales_transaction WHERE transaction_date BETWEEN ? AND ?", (p_start, p_end))
            prev_rev = cursor.fetchone()[0]
            delta = ((m_rev - prev_rev) / prev_rev * 100) if prev_rev > 0 else None
            delta_str = f"{delta:+.1f}% vs {p_y:04d}-{p_m:02d}" if delta is not None else "no prior month data"

            conn.close()
            sections.append(f"""=== {target_month} MONTHLY BREAKDOWN ===
            Revenue: RM {m_rev:,.2f} ({delta_str})
            Transactions: {m_txns:,} | Daily avg: RM {m_rev/m_days:,.2f} | Peak day: {busy_day} | Peak hour: {peak_hr}
            Branch performance:
            {chr(10).join(branches)}
            Top 5 items by volume:
            {chr(10).join(top_items)}""")
        except Exception as e:
            sections.append(f"=== {target_month} BREAKDOWN ===\nError: {e}")

    # ── STAFFING PEAKS ──────────────────────────────────────
    if intent["primary"] == "staffing" or "staffing" in intent["multi"]:
        peaks = db_data.get('branch_peaks', {})
        peak_lines = ["=== PEAK HOURS (top 3 per branch) ==="]
        for b_name, b_peaks in peaks.items():
            peak_lines.append(f"  {b_name}: {', '.join(b_peaks)}")
        sections.append("\n".join(peak_lines))

    # ── INVENTORY DEMAND ────────────────────────────────────
    if intent["primary"] == "inventory" or "inventory" in intent["multi"]:
        sections.append(f"=== PRODUCT CATEGORY PERFORMANCE ===\n{db_data.get('categories', 'No data')}")

    # ── TREND DATA ──────────────────────────────────────────
    if intent["primary"] == "trend_analysis" or (
        not month_match and any(k in msg for k in ['trend', 'month', 'growth', 'decline', 'compare', 'last', 'year'])
    ):
        sections.append(f"=== MONTHLY REVENUE TREND (last 6 months) ===\n{db_data.get('monthly_trend_summary', 'No trend data')}")

    # ── RAMADHAN SPECIAL CONTEXT ─────────────────────────────
    if "ramadhan" in msg or "puasa" in msg:
        # Fetch data for the 2026 window specifically
        r_start, r_end = "2026-02-19", "2026-03-20"
        try:
            conn = sqlite3.connect(os.path.join('database', 'coffee_shop.db'))
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(Total_Bill_MYR),0), COUNT(*), COUNT(DISTINCT transaction_date)
                FROM sales_transaction WHERE transaction_date BETWEEN ? AND ?
            """, (r_start, r_end))
            r_rev, r_txns, r_days = cursor.fetchone()
            
            # Top branch during Ramadhan
            cursor.execute("""
                SELECT store_location, SUM(Total_Bill_MYR) as rev
                FROM sales_transaction WHERE transaction_date BETWEEN ? AND ?
                GROUP BY store_location ORDER BY rev DESC LIMIT 1
            """, (r_start, r_end))
            r_top_row = cursor.fetchone()
            r_top = r_top_row[0] if r_top_row else "N/A"
            
            # Peak hour during Ramadhan
            cursor.execute("""
                SELECT Hour, COUNT(*) as cnt
                FROM sales_transaction WHERE transaction_date BETWEEN ? AND ?
                GROUP BY Hour ORDER BY cnt DESC LIMIT 1
            """, (r_start, r_end))
            r_peak_row = cursor.fetchone()
            r_peak = f"{int(r_peak_row[0]):02d}:00" if r_peak_row else "N/A"

            conn.close()
            sections.append(f"""=== HISTORICAL RAMADHAN 2026 DATA ({r_start} to {r_end}) ===
            - Total Ramadhan Revenue: RM {r_rev:,.2f}
            - Total Transactions: {r_txns:,}
            - Operating Days: {r_days}
            - Daily Avg: RM {r_rev/max(r_days,1):,.2f}
            - Top Performing Branch: {r_top}
            - Peak Transaction Hour: {r_peak} (Note: Sales peak shifted to late afternoon/night during fasting)""")
        except Exception as e:
            sections.append(f"=== RAMADHAN DATA ===\nError fetching historical Ramadhan data: {e}")

    # ── CHART DATA ──────────────────────────────────────────
    if intent["primary"] == "chart_request" or any(k in msg for k in _CHART_KEYWORDS):
        m_count = 3
        m_match = re.search(r'last (\d+) month', msg)
        if m_match:
            m_count = int(m_match.group(1))
        try:
            from analytics import multi_month_chart_pre_packager
            chart_data = multi_month_chart_pre_packager(months=m_count)
            sections.append(f"=== CHART DATA ===\n[CHART_DATA={chart_data}]")
        except Exception as e:
            sections.append(f"=== CHART DATA ===\nError generating chart payload: {e}")

    return "\n\n".join(sections)


# ============================================================
#   MAIN CHAT ENTRY POINT 
# ============================================================

def process_chat_message(user_message: str, db_data: dict) -> tuple[bool, str]:
    """
    Primary entry point for chat messages.
    1. Classify intent
    2. Try fast bypass for simple KPI lookups
    3. Build context + call Gemini with dynamic prompt
    """
    if is_closing_statement(user_message):
        return True, random.choice([
            "Sama-sama, Boss. Semoga jualan esok lebat!",
            "Terima kasih kembali. Saya sentiasa di sini kalau ada soalan data.",
            "All the best for the next shift! Jumpa lagi.",
            "No problem. Let's hit those targets!"
        ])

    intent = classify_intent(user_message)

    # Fast path: single-metric lookups bypass the LLM entirely
    if intent["primary"] == "quick_kpi":
        bypass = fast_kpi_bypass(user_message, db_data)
        if bypass:
            return True, bypass

    context = build_slim_context(db_data, user_message)
    full_prompt = f"{context}\n\n=== OWNER'S QUESTION ===\n{user_message}"

    return get_ai_insight(full_prompt, intent=intent)


def stream_chat_message(user_message: str, db_data: dict):
    """Streaming version of process_chat_message."""
    if is_closing_statement(user_message):
        bye = random.choice([
            "Sama-sama, Boss. Semoga jualan esok lebat!",
            "Terima kasih kembali. Saya sentiasa di sini kalau ada soalan data.",
            "All the best for the next shift! Jumpa lagi.",
            "No problem. Let's hit those targets!"
        ])
        yield f"data: {json.dumps({'chunk': bye})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"
        return

    intent = classify_intent(user_message)

    # Fast bypass doesn't stream — yield it as a single chunk
    if intent["primary"] == "quick_kpi":
        bypass = fast_kpi_bypass(user_message, db_data)
        if bypass:
            yield f"data: {json.dumps({'chunk': bypass})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

    context = build_slim_context(db_data, user_message)
    full_prompt = f"{context}\n\n=== OWNER'S QUESTION ===\n{user_message}"

    yield from stream_ai_insight(full_prompt, intent=intent)


# ============================================================
#   BACKWARD COMPATIBILITY STUB
# ============================================================

def build_chat_system_context(db_data: dict) -> str:
    """Legacy fallback for older app.py import chains."""
    return (
        f"You are the AI Business Advisor for 'Mini Coffee Shop'.\n"
        f"Period: {db_data.get('date_range', 'N/A')} | "
        f"Revenue: RM {db_data.get('total_rev', 0):,.2f} | "
        f"Transactions: {db_data.get('total_txns', 0):,} | "
        f"Daily avg: RM {db_data.get('daily_avg', 0):,.2f} | "
        f"Peak: {db_data.get('peak_hour', 'N/A')} | "
        f"Top branch: {db_data.get('top_branch', 'N/A')}"
    )
