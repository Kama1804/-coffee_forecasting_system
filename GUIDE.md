# Mini Coffee Shop Forecasting System: Comprehensive Guide (Phases)

This document outlines the logical phases of the **Coffee Forecasting System**, justifying the design and technical architecture used to provide a "Glass Box" experience for the owner.

---

## 🏛️ System Architecture Overview
The system is built as a **Modular Intelligence Platform**. Instead of a monolithic app, it separates data cleaning, predictive math, and AI reasoning into distinct layers.

### Phase 1: Data Ingestion & Sanitization (`etl_pipeline.py`)
**Purpose:** To transform messy POS logs into a "Clean Data Warehouse."
*   **The Logic:** Owners often have inconsistent naming (e.g., "Iced Latte" vs "Latte Iced"). The ETL pipeline standardizes these into a uniform schema.
*   **Safety Gate:** It validates the 17-column enterprise CSV format. If a column is missing, it stops immediately to prevent data corruption.
*   **Recipe Registry:** A critical subset of this phase. If a new product is detected, the system flags it. This ensures that Phase 3 (Inventory) has the math it needs to calculate ingredients.

### Phase 2: Contextual Enrichment (`weather_api.py` & `analytics.py`)
**Purpose:** To give the data "Local Awareness."
*   **Weather Intelligence:** The system doesn't just see a "Friday." It sees a "Raining Friday in Putrajaya." It fetches historical weather (Open-Meteo) and future forecasts (OpenWeatherMap).
*   **Operational Filters:**
    *   **Ramadhan Mode:** Automatically shifts peak-hour analysis to the night shift (4:30 PM - 12:00 AM) during the fasting month.
    *   **Payday Logic:** Specifically tracks the 25th–28th of each month to identify spending surges.

### Phase 3: Predictive Intelligence (`forecast_engine.py`)
**Purpose:** To answer "What happens next?"
*   **The Prophet Engine:** Uses additive regression models to handle seasonality.
*   **Branch Personas:** This is a unique feature.
    *   **Putrajaya:** Modeled as "Office/Government" (Weekday peaks).
    *   **Puncak Alam:** Modeled as "Student/Resident" (Weekend/Holiday peaks).
*   **Ingredient Demand Math:** Converts Ringgit forecasts into physical stock requirements (grams of beans, liters of milk) using the Recipe Registry.

### Phase 4: Conversational Consultation (`gemini_agent.py`)
**Purpose:** To turn data into natural-language advice.
*   **Intent Classification:** Before answering, the AI classifies the query (e.g., "Is this a staffing question or a revenue trend?").
*   **KPI Bypass:** For simple lookups (e.g., "What was total revenue?"), the system bypasses the expensive LLM and uses a fast regex engine to give instant, RM-accurate answers.
*   **Slim Context Injection:** Only the most relevant data is sent to Gemini to keep responses fast and token costs low.

---

## 📖 Page-by-Page User Guide

### 1. Dashboard (The Pulse)
*   **Function:** Real-time monitoring of KPIs and trends.
*   **Key Detail:** Includes the **Revenue Retention Chart** (Gross vs. Net) to show exactly how much money is being "lost" to promotions.

### 2. Data Ingestion (The Brain Setup)
*   **Function:** Feeding the system.
*   **Key Detail:** Owners can see their "Warehouse Profile"—how much data they have for each branch.

### 3. AI Forecast (The Planner)
*   **Function:** Staffing and procurement guide.
*   **Key Detail:** Features the **"What Drives the Forecast?"** sidebar, explaining how weather and holidays influenced the AI's prediction.

### 4. DSS Chatbot (The Advisor)
*   **Function:** 24/7 business consultant.
*   **Key Detail:** Can generate **Visual Charts** directly in the chat window.

### 5. PDF Reports (The Publication)
*   **Function:** Boardroom-ready documentation.
*   **Key Detail:** Includes an **AI-Written Executive Summary** with 3 actionable steps for the next month.

---

## 🔗 Sidebar Integration Justification
The **"System Guide"** link will be placed at the **bottom of the sidebar nav**, just above the status pill.
*   **Why?** It should be easily accessible but secondary to daily operational tools. It serves as a "Help/Documentation" anchor that the owner can click whenever they feel overwhelmed by the data.


  Here is the blueprint for the "System Intelligence & User Guide" page:

  ---

  Section 1: The "Core Engine" (How it Works)
  This explains the backend logic in a way that makes the owner feel like they have a high-tech advantage.
   * The ETL Pipeline: "Our system doesn't just store data; it enriches it. Every time you upload, we cross-reference your sales with local weather patterns and Malaysian public holidays to find hidden
     correlations."
   * The Prophet Model: "We use the same forecasting technology used by global tech companies, tuned specifically for the Malaysian 'Branch Persona'—whether you're serving office workers in Putrajaya or students
     in Puncak Alam."

  ---

  Section 2: Page-by-Page Breakdown (The User Guide)

  You can use a "Tabbed" or "Card-based" layout to explain each module:

  1. 📂 Data Ingestion (Upload Page)
   * Purpose: To turn raw POS logs into a "Clean Data Warehouse."
   * Key Content:
       * CSV Validator: Checks for 17 mandatory columns.
       * Recipe Registry: If you sell a new "Oatmilk Latte," the system stops here and asks you for the recipe so it can track your inventory accurately.
       * Data Enrichment: The "behind-the-scenes" fetching of historical weather.

  2. 📊 Operations Hub (Dashboard)
   * Purpose: Your "Daily Pulse" and historical health check.
   * Key Content:
       * KPI Tiles: Real-time Revenue, Volume, and AOV (Average Order Value).
       * Heatmaps: Shows exactly which hour is your busiest (differentiates between Normal vs. Ramadhan timings).
       * Payday Analysis: Specifically tracks if your customers spend more during the 25th–28th window.

  3. 🔮 Predictive Insights (Forecast Page)
   * Purpose: To plan your staffing and shopping for next week.
   * Key Content:
       * 7-Day Outlook: A line chart showing predicted revenue.
       * Ingredient Shopping Guide: The system converts "Predicted Cups" into "Kg of Beans" and "Liters of Milk."
       * Weather Impact: Tells you if a predicted "Thunderstorm" next Tuesday will likely drop your sales by 40%.

  4. 🤖 AI Business Advisor (Chatbot)
   * Purpose: To get instant answers without looking at charts.
   * Key Content:
       * Natural Language: Ask in English or Manglish.
       * Fast-Track KPIs: The bot bypasses the LLM to give you sub-second factual data on your revenue.
       * Visual Charts: The AI can generate a chart directly in the chat if you ask for a comparison.

  5. 📜 Executive Reporting (Report Page)
   * Purpose: Professional documentation for stakeholders or monthly reviews.
   * Key Content:
       * PDF Export: Boardroom-ready documents.
       * Trend Analysis: Comparison of this month vs. last month.
       * Actionable Advice: The AI writes 3 specific steps for you to take next month.

  I suggest breaking the tutorial into 4 Pillars of Intelligence. Each step corresponds to your backend modules:

  Step 1: Data Ingestion (The "Brain" Setup)
   * What the owner does: Uploads the POS CSV.
   * How it works (Backend): Explain etl_pipeline.py. 
       * Detail: "We don't just 'upload' your file. Our system scans every row, removes duplicates, and standardizes your product names so your 'Iced Latte' and 'LATTE (ICED)' are counted together perfectly."
   * Visual Idea: A "Data Cleaning" animation showing raw text turning into organized categories.

  Step 2: Contextual Enrichment (The "Local Awareness")
   * What the owner sees: Weather icons and Holiday tags on their dashboard.
   * How it works (Backend): Explain the Open-Meteo integration in the ETL.
       * Detail: "The system automatically looks up historical weather for Putrajaya or Puncak Alam. It identifies if it was a 'Thunderstorm' day, which helps the AI understand why sales might have been lower
         that day, preventing 'false alarms' in your trends."

  Step 3: Predictive Modeling (The "Future Sight")
   * What the owner sees: A 7-day revenue line and an Ingredient Shopping List.
   * How it works (Backend): Explain forecast_engine.py (Prophet).
       * Detail: "We use a 'Prophet' engine that learns your shop’s unique 'Persona'. If you are in Puncak Alam, the system knows students go home on holidays. If you are in Putrajaya, it knows office workers are
         your primary drivers. It then calculates exactly how many grams of coffee beans you need based on the predicted cup count."

  Step 4: AI Consultation (The "Expert Advisor")
   * What the owner does: Chats with the Gemini bot.
   * How it works (Backend): Explain gemini_agent.py and analytics.py.
       * Detail: "The AI isn't just guessing. It has 'Slim Context'—a tiny, secure summary of your latest sales and upcoming weather. When you ask for staffing advice, it checks the forecast to see if a 'Payday
         Friday' is coming up, ensuring you aren't understaffed during a rush."

  ---

  3. My Implementation Recommendation: "The Interactive Blueprint"

  Instead of just text, I recommend a Hybrid Landing Page that uses "Logic Cards". 

  The Design Idea:
   1. Header: "Your AI Partner: Under the Hood"
   2. The Journey Map: Use a vertical timeline (CSS border-left).
   3. The Backend-to-Business Mapping:
       * Backend File: etl_pipeline.py → User Benefit: "Clean Data = Accurate Reports"
       * Backend File: forecast_engine.py → User Benefit: "Never Run Out of Milk Again"
       * Backend File: gemini_agent.py → User Benefit: "A Consultant in Your Pocket"

  4. Why this is better than a simple manual:
   * Reduces Support Queries: When users understand that "Missing Recipes" (from ETLPipeline._check_for_missing_recipes) cause ingredient errors, they will fix the recipes themselves.
   * Higher Engagement: Users feel more "tech-savvy" when they understand the workflow.
   * Scalability: As you add new features (e.g., "Supplier Integration"), you simply add a new "Intelligence Card" to the tutorial.