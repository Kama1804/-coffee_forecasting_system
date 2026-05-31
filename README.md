# Mini Coffee Shop Sales Forecasting System

![Python Version](https://img.shields.io/badge/python-3.x-blue.svg)
![Flask](https://img.shields.io/badge/flask-3.x-green.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

An AI-powered business intelligence platform designed specifically for small coffee shop owners in Malaysia. It transforms raw Point of Sale (POS) data into actionable insights, providing accurate sales forecasts, automated executive reporting, and a Gemini-powered conversational AI advisor.

## 🚀 Main Features

*   **Automated Data Pipeline (ETL):** Seamlessly upload raw POS CSV logs. The system cleans the data, handles collisions, normalizes categories, and enriches it with historical weather data via the Open-Meteo API.
*   **Predictive Analytics (Prophet Engine):** Generates 7-day revenue forecasts tailored with Branch Personas (e.g., student vs. office worker demographics). It incorporates advanced regressors including Malaysian public holidays (mapped to 2027), Ramadan seasonality, custom promotional events, and 5-day future weather forecasts via OpenWeatherMap.
*   **AI Business Advisor:** A Gemini-powered chatbot (`gemini-3.1-flash-lite`) that acts as a business consultant. Ask questions in English, Bahasa Melayu, or Manglish. It generates insights on Staffing, Inventory, and Revenue opportunities, and can even plot interactive charts directly in the chat.
*   **Live Dashboard & Visualizations:** Interactive real-time metrics, day-by-hour heatmaps, product mix analysis, and branch performance comparisons powered by Plotly.js and Chart.js.
*   **Executive Reporting:** Generate comprehensive, multi-page PDF reports.
    *   **Executive Sales Report:** Detailed period-over-period breakdown (ReportLab).
    *   **AI Sales Forecast Report:** Forward-looking prediction breakdown with Prophet model transparency metrics (WeasyPrint).
*   **Production-Grade Performance:** 
    *   **KPI Bypass:** Factual questions (revenue, top branch, etc.) bypass the LLM for sub-second responses.
    *   **Slim Context Injection:** Dynamically filters business data to reduce token costs and API latency.
    *   **In-Memory Caching:** Context-aware TTL caching reduces database pressure.
    *   **SQL Optimization:** Indexed schema for high-speed aggregations across millions of rows.

## 🛠️ Technologies Used

### Backend & AI
*   **Framework:** Flask (Python)
*   **Database:** SQLite (Indexed for production-level query performance)
*   **Forecasting Model:** Prophet (v1.3.0), scikit-learn
*   **Generative AI:** Google Gemini (Primary: `gemini-3.1-flash-lite`, Fallback: `gemini-2.5-flash-lite`)
*   **External APIs:** Open-Meteo (Historical Weather), OpenWeatherMap (Future Weather)
*   **PDF Generation:** WeasyPrint, ReportLab

### Frontend
*   **Markup / Styling:** HTML5, CSS3, Bootstrap 5
*   **Animations:** AOS (Animate On Scroll)
*   **Charts & Visuals:** Plotly.js, Chart.js

## ⚙️ Installation & Prerequisites

### Prerequisites
*   Python 3.9+
*   API Keys:
    *   [Google Gemini API Key](https://aistudio.google.com/app/apikey)
    *   [OpenWeatherMap API Key](https://openweathermap.org/api)

### Setup Instructions

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/yourusername/coffee-forecasting-system.git
    cd coffee-forecasting-system
    ```

2.  **Create and activate a virtual environment:**
    ```bash
    python -m venv venv
    
    # On Windows:
    venv\Scripts\activate
    
    # On macOS/Linux:
    source venv/bin/activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment Variables:**
    Create a `.env` file in the root directory and add your credentials:
    ```ini
    FLASK_SECRET_KEY=your_secure_flask_key
    ADMIN_USERNAME=admin
    ADMIN_PASSWORD=password123
    OPENWEATHER_API_KEY=your_openweathermap_api_key
    GEMINI_API_KEY=your_gemini_api_key
    ```

5.  **Initialize the Database:**
    ```bash
    python init_db.py
    ```

6.  **Run the Application:**
    ```bash
    python app.py
    ```
    The application will be available at `http://127.0.0.1:5000/`.

## 📂 Project Structure

```text
coffee_forecasting_system/
├── app.py                   # Main Flask application and API routes
├── analytics.py             # Analytics helper functions
├── check_weather.py         # Utility to test weather API integration
├── etl_pipeline.py          # Data ingestion, cleaning, and weather enrichment logic
├── forecast_engine.py       # Prophet model configuration, holidays, and regressors
├── gemini_agent.py          # AI integration, prompt engineering, and fallback logic
├── init_db.py               # SQLite schema initialization
├── weather_api.py           # OpenWeatherMap fetch utility for 5-day forecasts
├── requirements.txt         # Python dependencies
├── .env                     # Environment variables (Create this file)
├── database/                # SQLite database directory
│   └── coffee_shop.db
├── uploads/                 # Temporary storage for uploaded CSVs
├── templates/               # HTML Views (Jinja2)
│   ├── base.html            # Main layout wrapper
│   ├── dashboard.html       # Analytics dashboard
│   ├── forecast.html        # AI Forecast view
│   ├── chatbot.html         # Gemini AI chat interface
│   ├── report.html          # Reporting and PDF export interface
│   └── upload.html          # CSV ingestion interface
└── static/                  # Static assets
    ├── css/
    │   └── style.css        # Custom styles
    ├── js/                  # Frontend scripts
    └── images/              # Assets and logos
```

## 📖 Usage Examples

1.  **Ingest Data:** Navigate to the **Upload** page and upload your POS CSV file. The ETL pipeline will automatically sanitize the data and save it.
2.  **View Dashboard:** Open the **Dashboard** to see live KPIs, historical sales trends, and peak hour heatmaps. Use the top filters to segment by branch or specific month/year.
3.  **Generate Forecasts:** Go to the **Forecast** tab and select a branch. The system will run the Prophet engine, integrate upcoming weather, and output a 7-day prediction alongside accuracy metrics (MAPE).
4.  **Consult the AI:** Open the **Chatbot** and ask a question.
    *   *Example 1:* "Which branch performed better last month and by how much?"
    *   *Example 2:* "Give me staffing advice for Putrajaya next week considering the forecast."
    *   *Example 3:* "Can you show me a chart comparing the top 3 products?"
5.  **Export Reports:** Navigate to the **Reports** section, select your parameters, and click "Export to PDF" to generate a polished, boardroom-ready document.

## 🤝 Contributions

Contributions are welcome! If you would like to improve this project, please follow these steps:

1. Fork the project.
2. Create your feature branch (`git checkout -b feature/AmazingFeature`).
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

Please ensure your code adheres to the existing style and includes appropriate error handling.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
