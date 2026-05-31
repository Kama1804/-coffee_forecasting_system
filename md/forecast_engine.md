# Predictive Analytics Architecture: Sales Forecasting Engine
**Document Version:** 1.0  
**Target Module:** `forecast_engine.py`  
**Author:** Senior Data Scientist  

This document provides a technical deep dive into the time-series forecasting architecture deployed for the Mini Coffee Shop system. The engine utilizes Meta's Prophet model, heavily customized to ingest local Malaysian behavioral data, dynamic weather regressions, and demographic-specific operational parameters.

---

## 1. Prophet Model Configuration
The forecasting engine is built on `prophet` (v1.3.0). The model is configured to handle the high volatility of retail coffee sales by prioritizing weekly cyclicality over rigid yearly seasonality, utilizing a multiplicative approach.

### Hyperparameters
- **Seasonality Mode (`multiplicative`):** Chosen over `additive` because sales variance (e.g., weekend spikes) scales proportionally with the baseline volume of the branch.
- **Weekly Seasonality (`True`):** Hard-enabled to capture the drastic differences between weekday and weekend coffee consumption.
- **Daily & Yearly Seasonality (`False`):** Daily is disabled because aggregation occurs at the `YYYY-MM-DD` level. Yearly is disabled to prevent the model from overfitting to sparse long-term data; seasonal events (Ramadan, School Holidays) are explicitly managed via custom dataframes instead.
- **Confidence Intervals (`interval_width=0.95`):** Configured to output 95% uncertainty bounds (`yhat_lower`, `yhat_upper`), providing business owners with a realistic risk assessment for inventory planning.

---

## 2. Demographics & Branch Personas
A critical innovation in this pipeline is the hardcoded application of demographic behavioral patterns, defined in the `BRANCH_PERSONAS` dictionary. The engine dynamically adjusts predictions based on the target branch's known customer base.

### Implementation Logic
The model applies a post-processing multiplier (`holiday_effect`) to the Prophet output (`yhat`) whenever a localized public holiday occurs.

*   **Putrajaya (Office/Government Hub):**
    *   *Profile:* Peak demand during regular work hours.
    *   *Modifier:* `holiday_effect = -0.35`
    *   *Impact:* The model mathematically dampens predicted revenue by 35% on public holidays, accurately modeling the exodus of government workers.
*   **Puncak Alam (University/Residential Hub):**
    *   *Profile:* UiTM students and local families.
    *   *Modifier:* `holiday_effect = +0.15`
    *   *Impact:* The model amplifies predicted revenue by 15% on public holidays, capturing the surge in student and residential leisure spending.

*Code Reference:* `adj_yhat = max(0.0, adj_yhat * (1 + persona['holiday_effect']))`

---

## 3. External Regressors Matrix
The Prophet model is enriched with multiple external regressors (`add_regressor`), transforming it from a simple univariate time-series model into a robust multivariate engine.

### Regressor Integrations
1. **Weather Integration (`weather_encoded`):**
   - Mapped internally via a weighting system: `{'Sunny': 1, 'Cloudy': 1, 'Raining': 0}`.
   - *Future Fetch:* The engine queries the OpenWeatherMap API for 5-day future conditions. For days 6 and 7, the engine safely defaults to `Cloudy` (weight = 1) to maintain prediction stability without API data.
   - *Standardization:* `standardize=True` to scale the boolean-like weather states against continuous sales data.
2. **Day-Type Flags (`is_weekday`, `is_weekend`):**
   - Binary matrices (0 or 1) feeding directly into the model to reinforce the weekly cyclicality.
3. **Custom Promotional Events:**
   - `friday_promo`: Triggers every Friday (`ds.dayofweek == 4`).
   - `seasonal_promo`: Triggers during specifically mapped festive windows (e.g., Post-Raya campaigns).
   - *Prior Scale:* Both promos use `prior_scale=5.0`, forcing the model to aggressively weight these events compared to default Prophet priors (0.05).

---

## 4. Malaysian Contextual Elements
To handle the unique shifts of the Malaysian calendar, the engine bypasses Prophet's built-in country holidays and utilizes a strictly controlled, future-proofed matrix (`MY_PUBLIC_HOLIDAYS` and `MY_SEASONS`) mapped through 2027.

### Seasonal & Festive Mapping
- **Lunar/Hijri Shifting:** Events like Chinese New Year, Hari Raya Aidilfitri, and Hari Raya Aidiladha do not follow the Gregorian calendar. The script explicitly defines exact dates for these moving targets to ensure the model doesn't apply holiday weights to the wrong weeks in 2026/2027.
- **Ramadan & School Holidays:** Injected into Prophet's standard `holidays` dataframe. Instead of single-day spikes, these are treated as continuous periods affecting the baseline trend for ~30 days.

### Operational Closures vs. Promos (`_build_promo_and_closure_maps`)
The script defines granular operational logic for major festivals:
- **Raya Fitri & Adha:** Triggers a 3-day complete closure (`yhat = 0`), immediately followed by a 3-day promotional spike ("3-Day Post-Raya Campaign").
- **CNY & Deepavali:** Triggers a 0-day closure but a 2-day promotional spike.
- **Sundays:** Universally hardcoded to `yhat = 0` during the prediction loop, overriding the model entirely for mandated closed days.

---

## 5. Transparency Metrics Calculation
The engine evaluates itself on the historical training data to provide operational transparency to the user. This is calculated inside `_calculate_metrics()`.

### Mathematical Processing
1. **In-Sample Prediction:** The trained model predicts over the entire historical dataframe (`forecast = model.predict(df)`).
2. **Zero-Masking:** To prevent zero-division errors (e.g., Sundays or closed holidays), a boolean mask is applied: `mask = y_true != 0`.
3. **Metric Extraction:**
   - **MAPE (Mean Absolute Percentage Error):** Evaluates the average magnitude of error in percentage terms.
     `mape = mean_absolute_percentage_error(y_true[mask], y_pred[mask])`
   - **RMSE (Root Mean Square Error):** Penalizes larger errors, useful for identifying catastrophic misses in predicting peak volume days.
     `rmse = np.sqrt(mean_squared_error(y_true, y_pred))`
   - **Accuracy Score:** A simplified user-facing metric calculated as `(1 - MAPE) * 100`, bounded to a minimum of 0%.

These metrics are packaged and served via the JSON payload to the UI, allowing the user to trust the model empirically rather than treating it as a black box.