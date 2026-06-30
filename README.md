# Venezuela FX Monitor · Monitor Cambiario Venezuela

**English:** This bot doesn't just track the Venezuelan parallel exchange rate — it predicts it. Every morning it forecasts whether the spread between the BCV official rate and the parallel market will widen, hold, or narrow over the next 24 hours. Every forecast is automatically scored against what actually happened. The accuracy table below is a live track record, not a claim.

**Español:** Este bot no solo monitorea la tasa de cambio paralela venezolana — la predice. Cada mañana pronostica si la brecha entre la tasa oficial BCV y el mercado paralelo se ensanchará, se mantendrá o se estrechará en las próximas 24 horas. Cada pronóstico se puntúa automáticamente contra lo que realmente ocurrió. La tabla de precisión es un historial verificable en vivo, no una promesa.

---

## Today's Forecast · Pronóstico de Hoy

| Metric · Métrica | Value · Valor |
|---|---|
| BCV Oficial | **623.02** VES/USD |
| Paralelo | **734.22** VES/USD |
| Brecha · Spread | **17.9%** |
| Binance P2P USDT/VES | **736.27** VES/USDT (+0.3% vs paralelo) |
| **Pronóstico 24h · 24h Forecast** | **↓ Estrechando · Narrowing (48%)** |
| Actualizado · Updated | 2026-06-30 04:28 UTC |

![Venezuela BCV vs parallel rate — last 30 days](data/chart.png)

---

## Live Accuracy Track Record · Historial de Precisión en Vivo

Every day two models compete on the same forecast: a **naive base-rate benchmark** (what usually happens) and a **stat challenger** (kernel-weighted analogs over recent spread dynamics plus oil, news, and payday-cycle signals). Each prediction is scored against the actual 24h outcome with the **Brier score** — lower is better; a random guess scores 0.667, a perfect forecaster 0.000. Scores are shown with a 95% confidence interval, and the challenger only counts as beating the baseline once the gap is statistically significant (✓; ≈ means ahead but not yet significant). Honest status: with only a few dozen scored forecasts so far, gaps this small are provisional — we report the uncertainty instead of hiding it.

Cada día compiten dos modelos sobre el mismo pronóstico: una **línea base** (lo que suele ocurrir) y un **retador stat** (analogías ponderadas sobre la dinámica reciente de la brecha más señales de petróleo, noticias y quincena). Cada predicción se puntúa contra el resultado real a 24h con el **Brier score** — menor es mejor; aleatorio 0.667, perfecto 0.000. Los puntajes se muestran con intervalo de confianza del 95%, y el retador solo cuenta como superior a la línea base cuando la diferencia es estadísticamente significativa (✓; ≈ significa por delante pero aún no significativo). Estado honesto: con apenas unas decenas de pronósticos evaluados, diferencias tan pequeñas son provisionales — reportamos la incertidumbre en vez de ocultarla.

| Model · Modelo | Brier ↓ | vs Baseline | Forecasts Scored · Evaluados |
|---|---|---|---|
| naive (baseline) | 0.716 ± 0.076 | — | 46 |
| stat | 0.678 ± 0.060 | −0.037 ≈ | 46 |

*Accumulating live data since May 2026 · Acumulando datos en vivo desde mayo 2026*

→ Full forecast probabilities: [data/forecast.json](data/forecast.json)
→ Full accuracy history: [data/accuracy.json](data/accuracy.json)

---

## Signals Used · Señales Utilizadas

The models draw on four categories of signal:

| Signal | Source | Why it matters · Por qué importa |
|---|---|---|
| BCV & parallel rates | bcv.org.ve, ve.dolarapi.com | Core data — scraped every 30 min |
| Brent crude oil | FRED API (St. Louis Fed) | Oil shocks historically precede VES moves |
| FX news headlines | RSS: efectococuyo, elnacional, runrunes | BCV interventions & policy signals |
| Payday cycle | Computed from date | FX demand spikes predictably at quincena (15th & month-end) |

---

## Data Access · Acceso a Datos

All data is published automatically to this repository and free to use.

| File | Contents | Updated · Actualizado |
|---|---|---|
| [data/current.json](data/current.json) | Latest rate reading · Última lectura | Every scrape · Cada scrape |
| [data/forecast.json](data/forecast.json) | 24h forecasts, all models · Pronósticos 24h | Daily · Diario |
| [data/accuracy.json](data/accuracy.json) | Running Brier scores · Brier acumulado | Daily · Diario |
| [data/recent.csv](data/recent.csv) | Last 7 days of 30-min scrapes | Every scrape · Cada scrape |
| [data/daily.csv](data/daily.csv) | Daily avg / min / max spread | Every scrape · Cada scrape |
| [data/archive/](data/archive/) | Full history by month · Historial completo | Monthly · Mensual |

---

*Data is informational only and reflects scraped public sources. · Los datos son meramente informativos y reflejan fuentes públicas.*
