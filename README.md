# Venezuela FX Monitor · Monitor Cambiario Venezuela

**English:** This bot doesn't just track the Venezuelan parallel exchange rate — it predicts it. Every morning it forecasts whether the spread between the BCV official rate and the parallel market will widen, hold, or narrow over the next 24 hours. Every forecast is automatically scored against what actually happened. The accuracy table below is a live track record, not a claim.

**Español:** Este bot no solo monitorea la tasa de cambio paralela venezolana — la predice. Cada mañana pronostica si la brecha entre la tasa oficial BCV y el mercado paralelo se ensanchará, se mantendrá o se estrechará en las próximas 24 horas. Cada pronóstico se puntúa automáticamente contra lo que realmente ocurrió. La tabla de precisión es un historial verificable en vivo, no una promesa.

---

## Today's Forecast · Pronóstico de Hoy

| Metric · Métrica | Value · Valor |
|---|---|
| BCV Oficial | **517.96** VES/USD |
| Paralelo | **690.23** VES/USD |
| Brecha · Spread | **33.3%** |
| Binance P2P USDT/VES | **692.64** VES/USDT (+0.3% vs paralelo) |
| **Pronóstico 24h · 24h Forecast** | **↑ Ensanchando · Widening (33%)** |
| Actualizado · Updated | 2026-05-16 04:18 UTC |

![Venezuela BCV vs parallel rate — last 30 days](data/chart.png)

---

## Live Accuracy Track Record · Historial de Precisión en Vivo

Every day, four models compete. Each prediction is scored against the actual outcome using **Brier score** — a standard probabilistic accuracy metric where lower is better, a random guess scores 0.667, and a perfect forecaster scores 0.000. Any model that fails to beat the naive baseline gets rejected.

Cada día, cuatro modelos compiten. Cada predicción se puntúa con **Brier score** — una métrica estándar donde menor es mejor, un pronóstico aleatorio puntúa 0.667 y uno perfecto puntúa 0.000. Cualquier modelo que no supere la línea base se descarta.

| Model · Modelo | Brier ↓ | vs Baseline | Forecasts Scored · Evaluados |
|---|---|---|---|
| naive (baseline) | 1.2840 | — | 1 |
| stat | 0.6667 | −0.6173 ✓ | 1 |
| stat\_v2 (oil + news · petróleo + noticias) | — | — | 0 |
| stat\_v3 (+ payday · quincena) | — | — | 0 |

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
