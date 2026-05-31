# Product Requirement Document (PRD): Mini Coffee Shop Sales Forecasting System

## 1. Introduction
The **Mini Coffee Shop Sales Forecasting System** is an AI-powered business intelligence platform designed for small coffee shop owners in Malaysia. It transforms raw POS (Point of Sale) data into actionable insights and accurate sales forecasts, enabling better inventory management, staffing optimization, and data-driven decision-making.

## 2. Problem Statement
Small coffee shop owners often struggle with:
- **Unpredictable Sales:** Fluctuations due to weather, public holidays, and seasonal trends (e.g., Ramadan, school holidays).
- **Inventory Waste:** Over-ordering perishables leads to losses; under-ordering leads to missed sales.
- **Suboptimal Staffing:** Poorly timed shifts result in high labor costs during slow hours or poor service during peaks.
- **Complexity of Data:** Difficulty in interpreting raw transaction logs without specialized data analysis skills.

## 3. Goals & Objectives
- **Centralize Data:** Automate the cleaning and ingestion of sales data via an ETL pipeline.
- **Predictive Analytics:** Provide accurate 7-day sales forecasts using the Prophet time-series model.
- **AI-Driven Insights:** Offer a natural language interface (Chatbot) for owners to query their business data and receive strategic advice.
- **Executive Reporting:** Generate professional PDF reports summarizing performance and providing actionable recommendations.
- **Operational Efficiency:** Help owners optimize staffing and stock based on predicted demand and historical patterns (e.g., peak hours, weather effects).

## 4. Target Audience
- **Coffee Shop Owners/Managers:** Non-technical users who need quick, understandable business insights.
- **Operational Staff:** To understand shift requirements and preparation needs.

## 5. Functional Requirements

### 5.1 ETL & Data Management
- **CSV Ingestion:** Support for uploading POS transaction logs in CSV format.
- **Data Cleaning (Self-Healing):** Automated removal of duplicate transaction IDs (`txn_reference`), handling of missing values, and normalization of product names/categories.
- **Weather Enrichment (Historical):** Integration with Open-Meteo API to fetch historical weather conditions for each transaction vector during ingestion.
- **Database Storage:** Persistent storage of transactions, branches, and forecasts in a SQLite database with extended timeout limits to prevent locking collisions.

### 5.2 Dashboard & Visualization
- **KPI Tracking:** Real-time display of Total Revenue, Volume Sold, Total Transactions, and Daily Average.
- **Trend Analysis:** Interactive charts for revenue trends (daily/monthly) powered by Plotly.js.
- **Sparklines:** Real-time transaction trends visualized with Chart.js.
- **Product Performance:** Identification of top-performing and slow-moving products/categories.
- **Customer Behavior:** Heatmaps showing peak transaction times by day and hour.
- **Branch Comparison:** Comparative analysis of performance across multiple branches (e.g., Putrajaya vs. Puncak Alam).

### 5.3 Forecasting Engine
- **7-Day Forecast:** Daily revenue predictions for each branch using the Prophet model.
- **Branch Personas:** The model applies specific modifiers based on branch demographics (e.g., Office/Govt workers in Putrajaya vs. Students in Puncak Alam).
- **Future Weather Integration:** Uses OpenWeatherMap API to fetch 5-day weather forecasts for prediction inputs.
- **External Regressors:** Integration of weather conditions, public holidays, weekend/weekday modifiers, and custom promotional events (e.g., "Friday Promos") into the forecast model.
- **Malaysian Context:** Specialized handling of Malaysian Public Holidays, school holidays, and Ramadan effects (mapped out to 2027).
- **Model Transparency:** Display of Mean Absolute Percentage Error (MAPE), RMSE, and Accuracy % to communicate forecast reliability.

### 5.4 AI Business Advisor (Chatbot)
- **Natural Language Querying:** Users can ask questions about sales performance, branch comparisons, and trends.
- **Context-Aware Responses:** The AI uses the latest database snapshots and temporal context (e.g., payday cycles) to provide accurate answers.
- **Actionable Advice:** Specifically provides recommendations for Staffing, Inventory, and Revenue Opportunities.
- **Interactive Charts in Chat:** Ability to generate and display Plotly-compatible chart data blocks within the chat interface.

### 5.5 Reporting
- **Executive Summaries:** AI-generated monthly performance summaries.
- **Dual PDF Export Paths:**
  - **Executive Sales Report:** Multi-page detailed analysis generated using ReportLab.
  - **AI Sales Forecast Report:** Visual prediction breakdown generated using WeasyPrint.
- **Historical Comparison:** "Predicted vs. Actual" analysis to validate forecast accuracy over time.

## 6. Non-Functional Requirements
- **Performance:** 
    - **KPI Response Time:** < 1 second (via Fast KPI Bypass).
    - **Standard AI Response Time:** < 4 seconds (via Slim Context & Optimized Generation).
    - **Caching:** Dashboard and reports load efficiently due to an **In-Memory Context TTL Caching** system (`GLOBAL_CHAT_CACHE`, `REPORT_CACHE`, `FORECAST_CACHE`).
    - **DB Performance:** Fully indexed SQLite schema for high-speed analytical queries.
- **Usability:** Mobile-responsive web interface based on Bootstrap with AOS (Animate On Scroll) for polished aesthetics.
- **Reliability:** Built-in SQL lock resolution (extended 30s timeout) and model fallback mechanisms (Primary/Fallback AI models with 1-retry limit).
- **Security:** Admin-only access controlled via environment variables and session-based authentication.

## 7. Tech Stack
- **Backend:** Flask (Python 3.x)
- **Frontend:** HTML5, CSS3 (Vanilla + Bootstrap 5), JavaScript (Plotly.js for main charts, Chart.js for sparklines)
- **Database:** SQLite
- **Forecasting:** Prophet (v1.3.0), scikit-learn
- **AI Model:** Google Gemini (Primary: `gemini-3.1-flash-lite`, Fallback: `gemini-2.5-flash-lite`)
- **External APIs:** 
  - **Open-Meteo:** Historical weather for ETL.
  - **OpenWeatherMap:** Future weather for forecasting.
- **Reporting:** WeasyPrint (Forecasts), ReportLab (Executive Reports)

## 8. Data Schema (Core Entities)
- **Branch:** `branch_id` (PK), `branch_name`, `location_type`.
- **Sales Transaction:** `transaction_id` (PK), `txn_reference` (UNIQUE), `sale_date`, `transaction_time`, `branch_id` (FK), `product_category`, `product_name`, `quantity_sold`, `unit_price`, `total_revenue`, `payment_method`, `weather_condition`.
- **Sales Forecast:** `forecast_id` (PK), `forecast_date`, `branch_id` (FK), `predicted_revenue`, `lower_bound_revenue`, `upper_bound_revenue`.

## 9. Future Roadmap
- **Inventory Integration:** Linking sales forecasts to ingredient-level inventory requirements.
- **Staff Scheduling Module:** Automated shift generation based on predicted peak hours.
- **Multi-Tenant Support:** Allowing different coffee shop chains to manage their own isolated data.
- **Real-time API Integration:** Direct integration with popular POS systems (e.g., StoreHub, Slurp).
