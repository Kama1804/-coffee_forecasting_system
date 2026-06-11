# Mini Coffee Shop Sales Forecasting System (Production Edition)

![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)
![Flask](https://img.shields.io/badge/flask-3.x-green.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

An AI-powered business intelligence and forecasting platform designed for Malaysian coffee shop owners. This system transforms raw POS data into actionable operational insights, providing precise sales forecasts, automated inventory planning, a Gemini-powered conversational AI advisor, and an interactive learning manual.

---

## 🚀 Core Production Features

*   **Interactive User Manual & Live Simulators (New):**
    *   **PWA Mobile Responsiveness:** The `/user-manual` page is fully responsive. On mobile screen sizes, it hides the vector SVG map and presents a touch-friendly navigation grid of buttons that dynamically syncs active states.
    *   **MathJax Responsive LaTeX Formulas:** Dynamically scales equations down on mobile screens (80%-88% font sizes) and embeds them in responsive, touch-scrollable horizontal containers to prevent visual layout breaking.
    *   **Embedded Calculators**: Interactive simulators for *Weather & Holiday Adjustments* and *Promotion ROI multipliers* allow owners to play with scenarios and see predictions in real-time.

*   **App-Like Conversational AI Advisor (Improved):**
    *   **Viewport Height Lock**: The `/chatbot` screen layout is optimized to behave like a native mobile app. The chatbot header and message footer stay pinned to the top and bottom of the viewport respectively, and only the conversation bubble canvas (`.chat-canvas`) handles scrolling.
    *   **Gemini 3.1 Integration:** Uses `gemini-3.1-flash-lite` to stream context-aware insights on staffing, inventory, and revenue.
    *   **Optimized Performance:** Features a **Fast KPI Bypass** for immediate SQL-aggregated answers to common questions and in-memory TTL caching (300s) to minimize token consumption and API latency.

*   **Automated ETL Pipeline & Weather Enrichment:**
    *   **Weather Ingestion Voting**: Automatically queries historical logs from **Open-Meteo** using branch coordinates. It votes for a dominant daylight condition (Fair/Sunny, Cloudy, Raining, Thunderstorm) using a 2.5mm precipitation volume threshold.
    *   **Data Sanitization:** Rejects duplicate invoice numbers, normalizes currency, standardizes checkout payment categories, and maps product items.

*   **Predictive Analytics (Prophet Engine 2.0):**
    *   **Tailored Outlets (Branch Personas):** Learns demographic patterns automatically (e.g., Putrajaya weekday government office workers vs. Puncak Alam weekend student leisure profiles).
    *   **Holiday & Ramadhan Tuning:** Learns historical multiplier impacts for Malaysian public holidays, CNY, Hari Raya, Deepavali, and handles the Ramadhan peak shift (afternoon coffee drop, evening breaking-fast surge).
    *   **Future Weather Regressors:** Adjusts the upcoming 5-day forecast by querying future weather forecasts from the **OpenWeatherMap API**.

*   **Recipe Registry & Inventory Planning:**
    *   **Automated Ingredient Demand:** Translates forecasted cup sales into the exact weights of coffee beans (g), fresh milk (ml), chocolate (g), ice (g), and cup quantities needed for the next 5 days.
    *   **Missing Recipe Audits:** Scans uploaded POS data and flags any new menu items lacking recipe registry parameters.

---

## 🛠️ Technology Stack

### Backend
- **Framework:** Flask (Python) with Threaded SSE support.
- **Database:** SQLite (Enterprise-indexed schema).
- **Forecasting:** Facebook Prophet, scikit-learn.
- **AI:** Google Gemini (Primary: `gemini-3.1-flash-lite`).
- **Reporting:** WeasyPrint & ReportLab for multi-page PDF generation.

### Frontend
- **Design:** Bootstrap 5, custom CSS.
- **Interactivity:** Plotly.js & Chart.js for real-time heatmaps, bar charts, and trend line charts.
- **Formulas:** MathJax CDN (automatic LaTeX conversion).

---

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
   Create a `.env` file in the root directory:
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

---

## ☁️ Production Deployment (Railway)

This system is fully optimized for containerized cloud deployment on **Railway** using their native Nixpacks builder.

### 1. Railway Environment Variables
Set the following variables in the **Variables** tab of your Railway service:

| Variable | Recommended Value | Description |
|---|---|---|
| `DB_PATH` | `/data/coffee_shop.db` | Path inside the persistent volume to store the SQLite database. |
| `UPLOAD_FOLDER` | `/data/uploads` | Path inside the persistent volume to cache uploaded POS CSV files. |
| `PORT` | `8080` | The network port the container binds to. |
| `GEMINI_API_KEY` | *your_gemini_key* | Google AI Studio key for chatbot advisor. |
| `OPENWEATHER_API_KEY` | *your_owm_key* | Weather forecasting API key. |
| `FLASK_SECRET_KEY` | *secure_random_string* | Flask session signature key. |
| `ADMIN_USERNAME` | *your_username* | Dashboard administrator login username. |
| `ADMIN_PASSWORD` | *your_password* | Dashboard administrator login password. |

### 2. Attaching Persistent Storage (Volume)
SQLite requires a persistent storage drive to prevent your data from resetting on redeployments:
1. Go to your service **Settings** in Railway.
2. Scroll to **Volumes** and click **Add Volume**.
3. Set the **Mount Path** to exactly `/data`.
4. Choose a size (e.g., `500 MB` is more than enough for thousands of transactions).

The system features an automated, self-healing startup script that auto-detects the volume, initializes the SQLite database schema if missing, and seeds your default branch personas and 15 product recipes!

---

## 📂 Project Structure

```text
coffee_forecasting_system/
├── app.py                     # Flask endpoints, SSE streaming, & route mapping
├── analytics.py               # SQL aggregates, ROI math, & payday indicators
├── etl_pipeline.py            # CSV validator & Open-Meteo weather encoder
├── forecast_engine.py         # Prophet multiplicative model & holiday tuner
├── gemini_agent.py            # AI prompt engineer, routing, & stream parser
├── weather_api.py             # OpenWeatherMap future forecast adapter
├── init_db.py                 # SQLite tables initialization & initial recipes
├── coffee_forecasting_system_manual.md # Complete stakeholder manual artifact
├── database/                  # SQLite storage directory
├── static/                    # CSS/JS stylesheet assets (Plotly, Chart.js)
└── templates/                 # Jinja2 views
    ├── base.html              # Collapsible sidebar & responsive layout shell
    ├── dashboard.html         # MoM stats, peak hours, & payday indexes
    ├── forecast.html          # Prophet line plots & ingredient shopping lists
    ├── report.html            # 5-page A4 print preview & PDF exporter
    ├── chatbot.html           # PWA height-locked AI advisory chat canvas
    ├── manage_business.html   # Outlet status settings & recipe configurations
    └── manual_landing.html    # Responsive interactive user manual & simulators
```

---

## 📊 Usage Guide

1.  **Ingest:** Upload your POS CSV on the *Data Ingestion* page. The system cleans it, pulls historical weather, and checks recipes.
2.  **Dashboard:** Review outlet growth, payday drift stats, and regular vs. Ramadhan peak operating hours.
3.  **Forecast:** Generate an AI sales projection. View the dynamic weather adjustments and print the *Ingredient Shopping Guide*.
4.  **AI Advisor:** Stream answers to business queries in English or Malay. Use context cards for real-time inventory levels.
5.  **User Manual:** Navigate to `/user-manual` to view mathematical definitions, read system specs, and simulate weather/ROI changes interactively.

---

## 📄 License
MIT License. Created for the Malaysian F&B sector.
