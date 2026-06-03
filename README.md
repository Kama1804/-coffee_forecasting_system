# Mini Coffee Shop Sales Forecasting System (Production Edition)

![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)
![Flask](https://img.shields.io/badge/flask-3.x-green.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

An AI-powered business intelligence and forecasting platform designed for Malaysian coffee shop owners. This system transforms raw POS data into actionable operational insights, providing precise sales forecasts, automated inventory planning, and a Gemini-powered conversational AI advisor.

## 🚀 Core Production Features

*   **Automated ETL Pipeline & Weather Enrichment:**
    *   **Historical Enrichment:** Automatically fetches weather data from **Open-Meteo** using a daylight-weighted aggregation (07:00 - 19:00). It distinguishes between Fair/Sunny, Cloudy, Raining, and Thunderstorms based on precipitation volume (2.5mm threshold).
    *   **Data Sanitization:** Handles collisions, normalizes payment methods, and standardizes item naming conventions.

*   **Predictive Analytics (Prophet Engine 2.0):**
    *   **7-Day Revenue Forecasts:** Tailored with **Branch Personas** (e.g., Putrajaya's office workers vs. Puncak Alam's student demographic).
    *   **Dynamic Operational Intelligence:** Incorporates Malaysian public holidays (2024–2027), Ramadan seasonality, and custom festive promotion windows (CNY, Raya, Deepavali).
    *   **Weather-Driven Regressors:** Adjusts sales expectations based on 5-day future weather forecasts via **OpenWeatherMap**.

*   **Recipe Registry & Inventory Planning:**
    *   **Automated Ingredient Demand:** Calculates the exact amount of coffee beans (g), milk (ml), chocolate (g), ice (g), and cups (Hot/Cold) needed for the upcoming 5 days based on forecasted sales volumes.
    *   **Missing Recipe Detection:** Automatically flags new items from POS uploads that require recipe configuration.

*   **AI Business Advisor (Gemini 3.1):**
    *   **Context-Aware Chat:** Uses `gemini-3.1-flash-lite` to provide insights on staffing, inventory, and revenue.
    *   **Performance Optimization:** Features a **Fast KPI Bypass** for factual queries (e.g., "What was yesterday's revenue?") and **Slim Context Injection** with in-memory TTL caching (300s) to minimize latency and token costs.

*   **Specialized Operational Modes:**
    *   **Ramadhan Mode:** Identifies peak transaction windows (4:30 PM – 12:00 AM) during the fasting month to optimize staffing for *Buka Puasa*.
    *   **Payday & Promo Intelligence:** Cross-tabulates sales performance during payday windows (25th–28th) and evaluates the ROI of various promotion codes.

## 🛠️ Technology Stack

### Backend
- **Framework:** Flask (Python) with Threaded SSE support.
- **Database:** SQLite (Enterprise-indexed schema).
- **Forecasting:** Facebook Prophet, scikit-learn.
- **AI:** Google Gemini (Primary: `gemini-3.1-flash-lite`).
- **Reporting:** WeasyPrint & ReportLab for multi-page PDF generation.

### Frontend
- **Design:** Bootstrap 5, AOS (Animate On Scroll).
- **Interactivity:** Plotly.js & Chart.js for real-time heatmap and trend visualizations.

## ⚙️ Setup & Installation

### Prerequisites
- Python 3.9+
- [Google Gemini API Key](https://aistudio.google.com/app/apikey)
- [OpenWeatherMap API Key](https://openweathermap.org/api)

### Installation

1. **Clone & Setup Environment:**
   ```bash
   git clone https://github.com/yourusername/coffee-forecasting-system.git
   cd coffee-forecasting-system
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configuration:**
   Create a `.env` file:
   ```ini
   FLASK_SECRET_KEY=your_key
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=password
   OPENWEATHER_API_KEY=your_owm_key
   GEMINI_API_KEY=your_gemini_key
   ```

3. **Initialize & Run:**
   ```bash
   python init_db.py
   python app.py
   ```

## 📂 Project Structure

```text
coffee_forecasting_system/
├── app.py               # Flask application with EventStream (SSE) & AI logic
├── analytics.py         # Core processing, ingredient math, & specialized filters
├── etl_pipeline.py      # Data ingestion with Open-Meteo weather enrichment
├── forecast_engine.py   # Prophet model with holiday/persona/promo logic
├── gemini_agent.py      # AI prompt engineering & streaming engine
├── weather_api.py       # Future weather fetcher (OpenWeatherMap)
├── init_db.py           # Database schema & initial recipe registry
├── database/            # SQLite storage
├── templates/           # Jinja2 Views (Dashboard, Chatbot, Forecast, Reports)
└── static/              # CSS/JS assets (Plotly, Chart.js)
```

## 📊 Usage Guide

1.  **Ingest:** Upload your POS CSV. The system will enrich it with historical weather and flag missing recipes.
2.  **Dashboard:** Monitor real-time KPIs, peak hour heatmaps (Regular vs. Ramadhan), and Payday spending shifts.
3.  **Forecast:** Generate a 5-day sales outlook and download the **Ingredient Shopping Guide** for procurement.
4.  **AI Advisor:** Ask "How should I staff for next Friday?" or "Why did revenue drop last month?" for instant, data-backed advice.

## 📄 License
MIT License. Created for the Malaysian F&B sector.
