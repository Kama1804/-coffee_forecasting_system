from google import genai
from google.genai import errors
import os
from dotenv import load_dotenv
from pathlib import Path
import sqlite3
import pandas as pd
from analytics import get_dashboard_metrics

# Load environment variables - override=True forces fresh load every time
load_dotenv(dotenv_path=Path(__file__).parent / '.env', override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Initialize Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def get_ai_insight(prompt):
    if not client:
        return False, "System Error: Gemini API key is missing from the environment."

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return True, response.text

    except errors.APIError as e:
        error_msg = str(e).lower()
        if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
            return False, "AI is currently busy. Please wait 30 seconds and try again."
        elif "403" in error_msg or "400" in error_msg or "invalid" in error_msg or "api_key" in error_msg:
            return False, "AI Connection Error: Your API key is invalid. Please check your .env file."
        else:
            return False, f"Gemini API Error: {str(e)}"

    except Exception as e:
        error_msg = str(e).lower()
        if "timeout" in error_msg:
            return False, "The AI took too long to respond. Please check your internet connection."
        return False, f"Connection Error: {str(e)}"

def get_business_advice(branch_id, branch_name):
    """
    Gathers database metrics and Prophet forecasts, constructs a strict 
    system prompt, and asks Gemini to generate operational advice.
    """
    print(f"Gathering data for {branch_name}...")
    
    # 1. Fetch Analytics (Peak Hours & Products)
    metrics = get_dashboard_metrics(branch_id)
    if not metrics:
        return False, "Not enough historical data to generate advice."

    # 2. Fetch the 7-Day Prophet Forecast from SQLite
    db_path = os.path.join('database', 'coffee_shop.db')
    conn = sqlite3.connect(db_path)
    
    query = f"""
        SELECT forecast_date, predicted_revenue 
        FROM sales_forecast 
        WHERE branch_id = {branch_id} 
        ORDER BY forecast_date ASC
    """
    forecast_df = pd.read_sql_query(query, conn)
    conn.close()

    if forecast_df.empty:
        return False, "No forecast data found. Please run Prophet engine first."

    # 3. Format the data into readable text for the AI
    forecast_text = forecast_df.to_string(index=False)
    
    # Grab the top 3 peak hours and top 3 products
    peak_hours_text = ", ".join([f"{m['hour']} ({m['quantity_sold']} items)" for m in metrics['peak_hours'][:3]])
    top_products_text = ", ".join([f"{m['product_category']} (RM {m['total_revenue']})" for m in metrics['product_mix'][:3]])

    # 4. Construct the Decision Support System Prompt
    system_prompt = f"""
    You are an expert AI Business Advisor for a 'Mini Coffee Shop' located in {branch_name}. 
    Your job is to analyze the following data and provide 3 actionable, highly specific recommendations for the business owner. Focus strictly on Staffing and Inventory.

    --- CURRENT DATA CONTEXT ---
    Upcoming 7-Day Revenue Forecast:
    {forecast_text}

    Historically Busiest Peak Hours: {peak_hours_text}
    Top Performing Product Categories: {top_products_text}

    --- INSTRUCTIONS ---
    1. Identify the highest and lowest earning days in the forecast and suggest staffing adjustments.
    2. Suggest inventory prep based on the Top Performing Products.
    3. Keep your response professional, concise, and format it with clear bullet points.
    4. Do not include introductory pleasantries (e.g., "Sure, here is the advice"). Start immediately with the insights.
    """

    print("Sending context to Gemini AI...")
    return get_ai_insight(system_prompt)

# --- TEST BLOCK ---
if __name__ == "__main__":
    print("Testing Decision Support Agent...\n")
    
    # Test for Putrajaya (Branch 1)
    success, advice = get_business_advice(branch_id=1, branch_name="Putrajaya")
    
    if success:
        print("\n=== AI BUSINESS ADVICE ===")
        print(advice)
        print("==========================")
    else:
        print(f"FAILED: {advice}")
        
# --- TEST BLOCK ---
if __name__ == "__main__":
    print("Testing Gemini AI Connection...\n")
    success, result = get_ai_insight(
        "Hello! Are you online? Please reply with 'Yes, I am online and ready to analyze coffee sales.'"
    )
    if success:
        print(f"GEMINI SAYS: {result}")
    else:
        print(f"FAILED: {result}")