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
from analytics import get_dashboard_metrics

load_dotenv(dotenv_path=Path(__file__).parent / '.env', override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ============================================================
#   INTENT CLASSIFIER — detects what the user actually wants
# ============================================================

INTENT_PROFILES = {
    "trend_analysis": {
        "keywords": ["trend", "growth", "decline", "compare", "vs", "month", "year", "last", "202", "performance over", "how has"],
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
        "keywords": ["ingredient", "stock", "inventory", "milk", "cup", "beans", "ice", "order", "supply", "demand", "restock"],
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
Lead with the most surprising or important finding. Use comparative framing ("X% higher than", "best since", "reversal of").
Avoid listing raw numbers without interpretation. Every number must earn its place.
""",
    "warning_driven": """
You are a vigilant ops manager who catches problems before they hurt the business.
For promo analysis: ALWAYS distinguish Value promos (%) from Volume promos (B1F1, Qty-based).
Volume promos → immediately warn that ingredient consumption (cups, milk, ice) will spike disproportionately vs revenue.
Be direct. Use ⚠️ for critical warnings. Don't soften the message.
""",
    "forward_looking": """
You are a forecasting advisor who bridges data predictions with on-the-ground realities.
When presenting forecasts: highlight anomalies (unusually high/low days), explain likely causes (weather, promos, day-of-week).
Always connect forecast to a concrete action: "Because Tuesday looks slow, consider reducing perishable orders by X."
Closed days (Sunday) = acknowledge briefly, move on — don't dwell on RM 0.00.
""",
    "operational": """
You are a hands-on operations coach. Your job is to turn data into tomorrow's to-do list.
Be specific: not "stock up on milk" but "Based on predicted 340 cups across both branches, order at least 25L extra."
Use urgency flags: 🔴 Critical (act today), 🟡 Watch (act this week), 🟢 Stable.
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
- Ramadhan context: {ramadhan_note}
"""

RESPONSE_LENGTH_GUIDE = {
    "shallow": "Max 60 words. One punchy answer.",
    "medium": "80–150 words. Key insight + 2-3 supporting points.",
    "deep": "150–280 words. Full analysis with narrative, data points, and at least one actionable recommendation.",
}

def build_dynamic_system_prompt(intent: dict) -> str:
    style = intent.get("style", "conversational")
    depth = intent.get("depth", "medium")
    persona = STYLE_PERSONAS.get(style, STYLE_PERSONAS["conversational"])
    length_guide = RESPONSE_LENGTH_GUIDE.get(depth, RESPONSE_LENGTH_GUIDE["medium"])
    ramadhan_note = _get_ramadhan_note() or "Not currently Ramadhan. Use standard peak hour (~10:00 AM)."

    rules = ABSOLUTE_RULES.format(ramadhan_note=ramadhan_note)

    # Multi-intent supplemental guidance
    supplemental = ""
    multi = intent.get("multi", [])
    if "promo_intelligence" in multi:
        supplemental += "\nSECONDARY LENS — Promo: flag any Volume-based promotion impacts within your response."
    if "forecast" in multi:
        supplemental += "\nSECONDARY LENS — Forecast: briefly tie current trend to what's coming next."
    if "inventory" in multi:
        supplemental += "\nSECONDARY LENS — Inventory: connect any demand signal to ingredient stocking action."

    return f"""You are the AI Business Advisor for 'Mini Coffee Shop' — Putrajaya (STB-PJ1) and Puncak Alam (FT-PA1), Malaysia.

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
    msg = user_message.lower().strip()
    has_temporal = any(k in msg for k in ['last', 'this', 'month', 'compare', 'trend', '202'])

    # Phrasing variants so the same question never gets the exact same answer twice
    rev = db_data.get('total_rev', 0)
    txns = db_data.get('total_txns', 0)
    daily = db_data.get('daily_avg', 0)
    top_branch = db_data.get('top_branch', 'N/A')
    peak = db_data.get('peak_hour', 'N/A')

    if re.search(r'\b(total revenue|how much did we make|all.?time revenue)\b', msg) and not has_temporal:
        return f"Across both branches all-time, the network has brought in RM {rev:,.2f} in total revenue."

    if re.search(r'\b(total transactions|how many (tickets|orders|receipts)|transaction count)\b', msg) and not has_temporal:
        return f"All-time transaction count stands at {txns:,} — that's every order across Putrajaya and Puncak Alam."

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
    from analytics import multi_month_chart_pre_packager, promo_efficiency_analyzer
    from forecast_engine import ForecastEngine

    intent = classify_intent(user_message)
    msg = user_message.lower()

    # Base context — minimal; intent-specific sections fill in the rest
    base = f"""DATA SNAPSHOT (as of query time):
- Period: {db_data.get('date_range', 'N/A')}
- All-time revenue: RM {db_data.get('total_rev', 0):,.2f}
- All-time transactions: {db_data.get('total_txns', 0):,}
- Daily avg: RM {db_data.get('daily_avg', 0):,.2f}
- Peak hour: {db_data.get('peak_hour', 'N/A')}
- Top branch: {db_data.get('top_branch', 'N/A')}"""

    sections = [base]

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
            conn = sqlite3.connect(os.path.join('database', 'coffee_shop.db'))
            cursor = conn.cursor()

            cursor.execute("""
                SELECT COALESCE(SUM(Total_Bill_MYR),0), COUNT(DISTINCT transaction_id), COUNT(DISTINCT transaction_date)
                FROM sales_transaction WHERE strftime('%Y-%m', transaction_date) = ?
            """, (target_month,))
            m_rev, m_txns, m_days = cursor.fetchone()
            m_days = m_days or 1

            cursor.execute("""
                SELECT store_location, COALESCE(SUM(Total_Bill_MYR),0), COUNT(transaction_id)
                FROM sales_transaction WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY store_location
            """, (target_month,))
            branches = []
            for r in cursor.fetchall():
                atv = r[1] / r[2] if r[2] > 0 else 0
                branches.append(f"  {r[0]}: RM {r[1]:,.2f} | {r[2]:,} txns | ATV RM {atv:.2f}")

            cursor.execute("""
                SELECT item_name, SUM(transaction_qty) as q FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY item_name ORDER BY q DESC LIMIT 5
            """, (target_month,))
            top_items = [f"  {r[0]}: {r[1]:,} units" for r in cursor.fetchall()]

            cursor.execute("""
                SELECT Hour, COUNT(*) FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY Hour ORDER BY COUNT(*) DESC LIMIT 1
            """, (target_month,))
            hour_row = cursor.fetchone()
            peak_hr = f"{hour_row[0]}:00" if hour_row else "N/A"

            cursor.execute("""
                SELECT "Day Name", COUNT(*) FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
                GROUP BY "Day Name" ORDER BY COUNT(*) DESC LIMIT 1
            """, (target_month,))
            day_row = cursor.fetchone()
            busy_day = day_row[0] if day_row else "N/A"

            # Month-over-month delta
            prev_month = (datetime.strptime(target_month + "-01", "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m")
            cursor.execute("""
                SELECT COALESCE(SUM(Total_Bill_MYR),0) FROM sales_transaction
                WHERE strftime('%Y-%m', transaction_date) = ?
            """, (prev_month,))
            prev_rev = cursor.fetchone()[0]
            delta = ((m_rev - prev_rev) / prev_rev * 100) if prev_rev > 0 else None
            delta_str = f"{delta:+.1f}% vs {prev_month}" if delta is not None else "no prior month data"

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

    # ── FORECAST + WEATHER ──────────────────────────────────
    if intent["primary"] in ("forecast", "inventory") or any(k in msg for k in ["forecast", "predict", "week", "ramalan", "weather"]):
        try:
            engine = ForecastEngine()
            success_pj, pj_fc = engine.generate_5_day_forecast("STB-PJ1", "Putrajaya")
            success_pa, pa_fc = engine.generate_5_day_forecast("FT-PA1", "Puncak Alam")

            if success_pj and success_pa:
                def _fmt_days(fc_list):
                    lines = []
                    for d in fc_list:
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

                pj_lines = _fmt_days(pj_fc.get('forecast', []))
                pa_lines = _fmt_days(pa_fc.get('forecast', []))
                combined_demand = {
                    k: round(pj_fc.get('ingredient_demand', {}).get(k, 0) + pa_fc.get('ingredient_demand', {}).get(k, 0), 2)
                    for k in pj_fc.get('ingredient_demand', {})
                    if k != 'custom'
                }

                sections.append(f"""=== 5-DAY PROPHET FORECAST ===
Note: Sundays are closed (RM 0.00).

Putrajaya (STB-PJ1):
{pj_lines}

Puncak Alam (FT-PA1):
{pa_lines}

Combined 5-day ingredient drawdown:
{json.dumps(combined_demand, indent=2)}""")
        except Exception as e:
            sections.append(f"=== FORECAST ===\nError: {e}")

    # ── STAFFING PEAKS ──────────────────────────────────────
    if intent["primary"] == "staffing" or "staffing" in intent["multi"]:
        peaks = db_data.get('branch_peaks', {})
        sections.append(
            f"=== PEAK HOURS (top 3 per branch) ===\n"
            f"  Puncak Alam (FT-PA1): {', '.join(peaks.get('FT-PA1', ['N/A']))}\n"
            f"  Putrajaya (STB-PJ1): {', '.join(peaks.get('STB-PJ1', ['N/A']))}"
        )

    # ── INVENTORY DEMAND ────────────────────────────────────
    if intent["primary"] == "inventory" or "inventory" in intent["multi"]:
        sections.append(f"=== PRODUCT CATEGORY PERFORMANCE ===\n{db_data.get('categories', 'No data')}")

    # ── TREND DATA ──────────────────────────────────────────
    if intent["primary"] == "trend_analysis" or (
        not month_match and any(k in msg for k in ['trend', 'month', 'growth', 'decline', 'compare', 'last', 'year'])
    ):
        sections.append(f"=== MONTHLY REVENUE TREND (last 6 months) ===\n{db_data.get('monthly_trend_summary', 'No trend data')}")

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
#   MAIN CHAT ENTRY POINT — wires everything together
# ============================================================

def process_chat_message(user_message: str, db_data: dict) -> tuple[bool, str]:
    """
    Primary entry point for chat messages.
    1. Classify intent
    2. Try fast bypass for simple KPI lookups
    3. Build context + call Gemini with dynamic prompt
    """
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
        f"You are the AI Business Advisor for 'Mini Coffee Shop' — Putrajaya and Puncak Alam.\n"
        f"Period: {db_data.get('date_range', 'N/A')} | "
        f"Revenue: RM {db_data.get('total_rev', 0):,.2f} | "
        f"Transactions: {db_data.get('total_txns', 0):,} | "
        f"Daily avg: RM {db_data.get('daily_avg', 0):,.2f} | "
        f"Peak: {db_data.get('peak_hour', 'N/A')} | "
        f"Top branch: {db_data.get('top_branch', 'N/A')}"
    )