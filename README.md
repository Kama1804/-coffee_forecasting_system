# Mini Coffee Shop Sales Forecasting System (Production Edition)

![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)
![Flask](https://img.shields.io/badge/flask-3.x-green.svg)
![Gemini AI](https://img.shields.io/badge/Google_Gemini-3.1_Flash_Lite-purple.svg)
![Forecasting](https://img.shields.io/badge/Engine-Prophet_2.0-orange.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

An AI-powered business intelligence, sales forecasting, and inventory planning platform designed specifically for Malaysian F&B coffee shop owners and multi-outlet managers. The platform transforms raw Point-of-Sale (POS) transaction CSV files into predictive operational insights, multi-outlet analytics, ingredient purchasing schedules, executive PDF reports, interactive scenario simulators, and a context-aware Gemini AI advisory chatbot.

---

## 🚀 Core Production Features

* **Automated Data Ingestion & ETL Validation (`etl_pipeline.py`)**
  * **17-Column Schema Validation:** Enforces strict column structure, standardizing timestamps, store codes, payment categories, and financial figures.
  * **Multi-Layer Data Sanitization:** Rejects duplicate invoice numbers (`Transaction_ID`), filters invalid/negative financial numbers, and ensures transaction idempotency.
  * **Historical Weather Ingestion:** Automatically queries historical weather data via the **Open-Meteo API** using outlet GPS coordinates. Votes for dominant daily weather conditions (*Fair / Sunny*, *Cloudy*, *Raining*, *Thunderstorm*) based on a 2.5mm precipitation volume threshold.
  * **Missing Recipe Audits:** Identifies newly uploaded menu items that lack recipe registry definitions and alerts administrators.

* **Predictive Sales Forecasting Engine 2.0 (`forecast_engine.py`)**
  * **Facebook Prophet Multiplicative Engine:** Models baseline sales trends and multiplicative seasonal patterns.
  * **Malaysian Holiday Engine:** Built-in holiday regressor calendar (2024–2027) covering Chinese New Year, Hari Raya Aidilfitri, Hari Raya Aidiladha, Deepavali, Merdeka Day, Malaysia Day, Labour Day, Wesak, and Agong's Birthday.
  * **Ramadan Operational Shift Modeling:** Dynamically accounts for daytime fasting sales drops and evening break-fast (*Iftar*) surges during Ramadan windows.
  * **School Holiday Regressors:** Tracks Malaysian national school term breaks (March, June, August, Year-End).
  * **Future Weather Regressors:** Integrates live 5-day weather forecasts via **OpenWeatherMap API** to dynamically scale upcoming daily forecasts (e.g., fair weather boost vs. rainy day dampener).
  * **Outlet Persona Profiles:** Distinguishes demographic patterns across branches (e.g., Putrajaya weekday government office profile vs. Puncak Alam weekend student campus profile).

* **Recipe Registry & Inventory Planning (`analytics.py`)**
  * **Ingredient Master Matrix:** Stores exact cup component proportions: Coffee Beans (g), Fresh Milk (ml), Chocolate Powder (g), Ice (g), Whipped Cream (g), Cup Type, and custom items.
  * **Automated 5-Day Ingredient Demand Planner:** Converts forecasted cup sales into precise purchasing estimates (kg of coffee beans, liters of milk, kg of chocolate, kg of ice, and total cup counts) to optimize procurement and minimize waste.

* **App-Like Conversational AI Advisor (`gemini_agent.py`)**
  * **Google Gemini Integration:** Built using the modern `google-genai` SDK with `gemini-3.1-flash-lite` (with fallback to `gemini-2.5-flash`).
  * **Dynamic Intent Classifier (`classify_intent`):** Automatically categorizes user queries into specialized intent profiles (`trend_analysis`, `promo_intelligence`, `forecast`, `inventory`, `staffing`, `quick_kpi`, `chart_request`).
  * **Fast SQL KPI Bypass:** Solves high-frequency direct business queries (e.g., total sales, top branches, peak hours) instantly via pre-calculated database aggregations, bypassing API latency and token cost.
  * **Performance Caching & Streaming:** Utilizes 300-second TTL in-memory caching and Server-Sent Events (SSE) streaming (`/api/chat/stream`) for real-time responsiveness.
  * **PWA Mobile Viewport:** Features a height-locked chat interface with pinned header/footer elements, scrollable conversation canvas (`.chat-canvas`), and session history save/restore/clear capabilities.

* **Multi-Outlet Branch & Recipe Management (`/manage-business`)**
  * **Outlet Configuration:** Create, update, or toggle active status for branches.
  * **Micro-Location Coordinates:** Configure exact GPS coordinates (latitude/longitude) per branch for hyper-local weather ingestion.
  * **Holiday Sensitivity:** Adjust holiday multiplier sensitivity (`holiday_effect`) per location persona.
  * **Recipe Editor:** Complete UI for setting and updating ingredient parameters across all menu items.

* **Interactive User Manual & Live Simulators (`/user-manual`)**
  * **Responsive PWA Manual:** Mobile-optimized layout with clean grid navigation and dynamic view switching.
  * **LaTeX Formula Rendering:** Uses MathJax CDN for dynamic mathematical equation rendering with horizontal touch-scrolling containers for mobile viewports.
  * **Interactive Scenario Calculators:** Embedded live simulators for *Weather & Holiday Demand Adjustments* and *Promotion ROI Multipliers*.

* **Executive Analytics & PDF Exporter (`/report`, `/forecast`)**
  * **Print-Ready A4 Reports:** Multi-page executive reports featuring Month-over-Month growth, peak hour heatmaps, payday drift indexes, category breakdowns, and promotional efficiency.
  * **Automated PDF Export:** Standardized PDF document exporter endpoints (`/api/export-pdf`, `/api/export-forecast-pdf`) powered by **WeasyPrint** / **ReportLab**.

* **Automated Documentation & Visual Capture Tools (`capture_manual.py`, `generate_pdf_manual.py`)**
  * **Playwright Screenshot Suite (`capture_manual.py`):** Headless browser automation script that logs in, navigates all system screens, applies filters, and captures clean documentation snapshots.
  * **PDF Manual Generator (`generate_pdf_manual.py`):** Automatically compiles captured screenshots, task-oriented user guides, and system documentation into a PDF user manual.

* **Automated Testing & QA Suite (`test_suite.py`)**
  * **Comprehensive Test Coverage:** Built on `pytest` to validate ETL duplicate rejection, financial bounds, ingredient calculation math, holiday enrichment, Prophet tuning, Gemini intent classification, session security, and REST API endpoints.

---

## 🛠️ Technology Stack

### Backend
- **Core Framework:** Flask (Python 3.9+) with Threaded SSE support.
- **Database:** SQLite (Enterprise indexed schema).
- **Forecasting Engine:** Facebook Prophet, scikit-learn, NumPy, pandas.
- **AI Engine:** Google Gemini API (`google-genai` SDK, primary: `gemini-3.1-flash-lite`).
- **Reporting & Export:** WeasyPrint, ReportLab.
- **Browser Automation:** Playwright.

### Frontend
- **Design & Layout:** Bootstrap 5, custom CSS (Dark/Light responsive theme).
- **Visualization:** Plotly.js, Chart.js for real-time heatmaps, bar charts, and trend lines.
- **Mathematical Equations:** MathJax 3 (LaTeX dynamic rendering).

---

## ⚙️ Setup & Installation

### Prerequisites
- Python 3.9 or higher
- [Google Gemini API Key](https://aistudio.google.com/app/apikey)
- [OpenWeatherMap API Key](https://openweathermap.org/api)

### Step-by-Step Installation

1. **Clone the Repository & Navigate to Workspace:**
   ```bash
   git clone https://github.com/yourusername/coffee-forecasting-system.git
   cd coffee-forecasting-system
   ```

2. **Create & Activate Virtual Environment:**
   ```bash
   # Windows (PowerShell)
   python -m venv venv
   .\venv\Scripts\activate

   # Linux / macOS
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Create a `.env` file in the root project directory:
   ```ini
   FLASK_SECRET_KEY=your_random_secret_key
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=your_secure_password
   GEMINI_API_KEY=your_google_gemini_api_key
   OPENWEATHER_API_KEY=your_openopenweathermap_api_key
   DB_PATH=database/coffee_shop.db
   UPLOAD_FOLDER=uploads
   ```

5. **Initialize Database Schema & Default Recipes:**
   ```bash
   python init_db.py
   ```
   *This initializes the SQLite database schema, creates indexes, seeds default branch personas, and populates initial product recipes.*

6. **Start the Application:**
   ```bash
   python app.py
   ```
   Access the system at `http://127.0.0.1:5000` in your browser. Default login credentials match `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env`.

---

## 🧪 Automated Testing & QA

The system includes a test suite covering unit tests, white-box ETL checks, financial boundary validations, and API endpoint security.

Run the test suite with `pytest`:
```bash
pytest test_suite.py
```

### Main Test Pillars Covered:
- **ETL Validation (UT-001 to UT-008):** Ingestion duplicate rejection, missing column detection, empty file handling, non-negative transaction validation.
- **Database Operations:** Idempotent transaction inserts and branch schema persistence.
- **Business Logic & Inventory Math:** Recipe demand scaling (beans, milk, choco, ice, cups), promo code normalization.
- **Forecast Engine & Holiday Rules:** Malaysian public holiday flags and Prophet auto-tuning adjustments.
- **AI Chatbot & KPI Bypass:** Intent classification scoring, fallback profile resolution, fast SQL KPI bypass execution.
- **Security & Authentication:** Session protection, login credential checks, unauthorized route access handling.

---

## 📸 Documentation & Snapshot Tools

The project provides automated scripts to capture UI snapshots and generate user manual PDFs:

1. **Capture Browser Screenshots:**
   Requires Playwright (`pip install playwright` & `playwright install`).
   ```bash
   python capture_manual.py
   ```
   *Logs into a local server instance and captures high-resolution screenshots across all major pages and filter states into `manual_screenshots/`.*

2. **Generate PDF User Manual:**
   ```bash
   python generate_pdf_manual.py
   ```
   *Compiles captured screenshots, instructions, LaTeX formulas, and operational guides into a structured PDF document.*

---

## ☁️ Production Deployment (Railway)

The application is configured for deployment on **Railway** using their native Nixpacks builder (`nixpacks.toml` & `railway.json`).

### 1. Environment Variables Configuration
In your Railway service dashboard, set the following environment variables:

| Variable | Example Value | Description |
|---|---|---|
| `DB_PATH` | `/data/coffee_shop.db` | Path inside persistent volume for SQLite storage. |
| `UPLOAD_FOLDER` | `/data/uploads` | Path inside persistent volume for uploaded POS files. |
| `PORT` | `8080` | Network port for container binding. |
| `GEMINI_API_KEY` | `AIzaSy...` | Google AI Studio key for chatbot advisor. |
| `OPENWEATHER_API_KEY` | `a1b2c3...` | OpenWeatherMap API key for weather forecasting. |
| `FLASK_SECRET_KEY` | `supersecretkey` | Session signing key. |
| `ADMIN_USERNAME` | `admin` | Dashboard login username. |
| `ADMIN_PASSWORD` | `securepassword` | Dashboard login password. |

### 2. Attaching Persistent Volume
To ensure database records and uploaded CSVs persist across redeployments:
1. Open your service **Settings** on Railway.
2. Under **Volumes**, click **Add Volume**.
3. Set the **Mount Path** to `/data`.
4. The system's self-healing startup script automatically detects the volume directory and initializes missing SQLite databases and recipes upon launch.

---

## 📂 Project Structure

```text
coffee_forecasting_system/
├── app.py                            # Flask application routes, SSE streaming, & API endpoints
├── analytics.py                      # SQL data aggregations, payday drift, & ingredient demand math
├── etl_pipeline.py                   # 17-column CSV POS validator & Open-Meteo weather encoder
├── forecast_engine.py                # Prophet 2.0 multiplicative forecasting & MY holiday engine
├── gemini_agent.py                   # Gemini AI agent, intent classifier, & fast KPI bypass
├── weather_api.py                    # OpenWeatherMap future 5-day forecast API client
├── init_db.py                        # SQLite database schema initialization & seed data
├── capture_manual.py                 # Playwright automated browser screenshot capture tool
├── generate_pdf_manual.py            # PDF documentation generator script
├── test_suite.py                     # Automated pytest test suite (UT-001 - UT-008 & integration)
├── coffee_forecasting_system_manual.md # Comprehensive Markdown stakeholder user manual
├── chapter_6_system_implementation.md# System implementation documentation chapter
├── chapter_7_system_testing.md       # System testing and verification documentation chapter
├── chapter_8_conclusion.md           # Project conclusion chapter
├── requirements.txt                  # Python dependencies specification
├── nixpacks.toml                     # Nixpacks build configuration for Railway deployment
├── railway.json                      # Railway service deployment manifest
├── database/                         # Local SQLite database storage directory
├── static/                           # Custom CSS styles, JS assets, Plotly.js, & Chart.js
├── templates/                        # Jinja2 HTML templates
│   ├── base.html                     # Responsive shell, sidebar navigation, & theme layout
│   ├── login.html                    # Authenticated session sign-in portal
│   ├── dashboard.html                # MoM stats, peak hours, payday indexes, & promo charts
│   ├── forecast.html                 # Prophet sales trend line graphs & ingredient demand planner
│   ├── forecast_pdf_export.html      # PDF export view for forecasting reports
│   ├── report.html                   # Interactive 5-page A4 executive reporting preview
│   ├── report_pdf_export.html        # PDF export view for executive analytics reports
│   ├── chatbot.html                  # PWA height-locked Gemini AI advisory chat UI
│   ├── manage_business.html          # Outlet status controls, coordinates, & recipe editor
│   ├── upload.html                   # POS CSV data ingestion portal & duplicate check UI
│   └── manual_landing.html           # Interactive manual with LaTeX MathJax formulas & simulators
└── testing/                          # Additional test scripts & accuracy benchmarks
    ├── test_pipeline_integration.py
    ├── prophet_test.py
    ├── test_upload.py
    └── get_accuracy.py
```

---

## 📊 Usage Workflow

1. **Ingest POS Data (`/upload`):** Upload your daily/monthly Point-of-Sale CSV export. The ETL pipeline validates columns, rejects duplicates, and fetches historical weather.
2. **Review Dashboard (`/dashboard`):** Analyze aggregate revenue, payday drift indicators, discount retention ratio, and peak operating hours.
3. **Generate Forecasts (`/forecast`):** View 5-day sales predictions generated by Prophet with weather and holiday adjustments. Print or export the **Ingredient Shopping List** for stock purchasing.
4. **Consult AI Advisor (`/chatbot`):** Query the chatbot in natural language (English or Malay) for staffing recommendations, inventory advice, or sales performance analysis.
5. **Manage Business & Recipes (`/manage-business`):** Add new branch locations, adjust GPS coordinates, or configure item ingredient parameters in the Recipe Registry.
6. **Export Executive Reports (`/report`):** View and export A4 multi-page executive summaries as PDF documents.
7. **Consult User Manual (`/user-manual`):** Learn system math formulas, view interactive simulators, or run scenario calculations.

---

## 📄 License

Distributed under the **MIT License**. Built for the Malaysian F&B sector.
