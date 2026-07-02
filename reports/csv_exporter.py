"""Exports rate data in a tiered structure for browsing on GitHub."""
import os
import json
import logging
import csv
import io
import tempfile
from datetime import datetime, timezone
from dotenv import load_dotenv
from db.db import get_connection, get_latest_forecast, get_forecast_scores
from reports.github_publisher import commit_files as _commit_files
# NOTE: do NOT import generate_chart here at module level — it imports
# matplotlib (~100MB RAM). main.py imports this module on startup, so a
# top-level import would load matplotlib into every Railway restart even
# though the chart only generates every 2h. Suspected OOM root cause of
# the restart loop. Import is deferred to inside export_dashboard_to_github.

load_dotenv()
logger = logging.getLogger(__name__)

def _query(sql: str, params: tuple = ()) -> list:
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _build_csv(rows: list, columns: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([r.get(c) for c in columns])
    return buf.getvalue()


def _build_recent_csv() -> str:
    """Last 7 days of raw scrapes (~336 rows max)."""
    rows = _query(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct, source "
        "FROM rates "
        "WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days') "
        "ORDER BY timestamp DESC"
    )
    return _build_csv(rows, ["timestamp", "bcv_rate", "parallel_rate", "spread_pct", "source"])


def _build_daily_csv() -> str:
    """One row per day, summarized (one row added per day, ~365/year)."""
    rows = _query(
        "SELECT date(timestamp) as day, "
        "AVG(bcv_rate) as avg_bcv, AVG(parallel_rate) as avg_parallel, "
        "AVG(spread_pct) as avg_spread, MIN(spread_pct) as min_spread, MAX(spread_pct) as max_spread, "
        "COUNT(*) as readings "
        "FROM rates "
        "WHERE bcv_rate IS NOT NULL OR parallel_rate IS NOT NULL "
        "GROUP BY day "
        "ORDER BY day DESC"
    )
    # Round for readability
    for r in rows:
        for k in ("avg_bcv", "avg_parallel"):
            if r.get(k) is not None:
                r[k] = round(r[k], 2)
        for k in ("avg_spread", "min_spread", "max_spread"):
            if r.get(k) is not None:
                r[k] = round(r[k], 2)
    return _build_csv(rows, ["day", "avg_bcv", "avg_parallel",
                              "avg_spread", "min_spread", "max_spread", "readings"])


def _build_archive_csv(year_month: str) -> str:
    """All raw data for a specific YYYY-MM month."""
    rows = _query(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct, source "
        "FROM rates "
        "WHERE strftime('%Y-%m', timestamp) = ? "
        "ORDER BY timestamp ASC",
        (year_month,)
    )
    return _build_csv(rows, ["timestamp", "bcv_rate", "parallel_rate", "spread_pct", "source"])


def _build_current_json() -> str:
    rows = _query(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct, source "
        "FROM rates WHERE bcv_rate IS NOT NULL AND parallel_rate IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    if not rows:
        return json.dumps({"error": "no data yet"})
    r = rows[0]
    result = {
        "timestamp_utc": r["timestamp"],
        "bcv_rate": r["bcv_rate"],
        "parallel_rate": r["parallel_rate"],
        "spread_pct": r["spread_pct"],
        "source": r["source"],
    }
    from db.db import get_latest_p2p_rate
    p2p = get_latest_p2p_rate()
    if p2p:
        result["binance_p2p"] = {
            "timestamp_utc": p2p["timestamp"],
            "mid_price_ves_per_usdt": p2p["mid_price"],
            "best_bid": p2p["best_bid"],
            "best_ask": p2p["best_ask"],
            "bid_ask_spread_pct": p2p["bid_ask_spread_pct"],
        }
    return json.dumps(result, indent=2)


_DIRECTION_SYMBOL = {"widen": "↑", "stable": "→", "narrow": "↓"}
_DIRECTION_ES = {"widen": "Ensanchando", "stable": "Estable", "narrow": "Estrechando"}
_DIRECTION_EN = {"widen": "Widening", "stable": "Stable", "narrow": "Narrowing"}
_MODEL_ORDER = ["naive", "stat"]
_MODEL_LABELS = {
    "naive": "naive (baseline)",
    "stat": "stat",
    "stat_v2": "stat\\_v2 (oil + news · petróleo + noticias)",
    "stat_v3": "stat\\_v3 (+ payday · quincena)",
    "momentum": "momentum (trend · tendencia)",
    "markov": "markov (regime · régimen)",
    "ensemble": "**ensemble (blend · combinación)**",
}


def _forecast_direction_display(model_name: str) -> str:
    row = get_latest_forecast(model_name)
    if not row:
        return "—"
    probs = {"widen": row["p_widen"], "stable": row["p_stable"], "narrow": row["p_narrow"]}
    direction = max(probs, key=probs.get)
    confidence = round(probs[direction] * 100)
    sym = _DIRECTION_SYMBOL[direction]
    return f"{sym} {_DIRECTION_ES[direction]} · {_DIRECTION_EN[direction]} ({confidence}%)"


def _build_forecast_json() -> str:
    forecasts = {}
    for m in _MODEL_ORDER:
        row = get_latest_forecast(m)
        if row:
            probs = {"widen": row["p_widen"], "stable": row["p_stable"], "narrow": row["p_narrow"]}
            direction = max(probs, key=probs.get)
            forecasts[m] = {
                "made_at": row["made_at"],
                "target_at": row["target_at"],
                "p_widen": round(row["p_widen"], 4),
                "p_stable": round(row["p_stable"], 4),
                "p_narrow": round(row["p_narrow"], 4),
                "direction": direction,
                "confidence_pct": round(probs[direction] * 100, 1),
            }
    # Exclude naive (no situational info) and ensemble (already a blend of the
    # others — counting it would double-weight its members) from the vote.
    directions = [v["direction"] for k, v in forecasts.items()
                  if k not in ("naive", "ensemble")]
    consensus = max(set(directions), key=directions.count) if directions else None
    return json.dumps({
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "horizon_hours": 24,
        "consensus_direction": consensus,
        "models": forecasts,
    }, indent=2)


_UNIFORM_BRIER = 2 / 3  # always-1/3 baseline for a 3-outcome Brier


def _brier_se(briers: list[float]) -> float | None:
    """Standard error of the mean Brier (sample std / sqrt n). None if n < 2."""
    n = len(briers)
    if n < 2:
        return None
    mean = sum(briers) / n
    var = sum((b - mean) ** 2 for b in briers) / (n - 1)
    return (var ** 0.5) / (n ** 0.5)


def _model_stats() -> dict:
    """Per-model accuracy with honest uncertainty: mean Brier + 95% CI, whether
    it significantly beats naive and uniform-random, and a calibration snapshot.

    'significant' = the gap exceeds 1.96 standard errors. At small n nearly
    everything will be non-significant — which is the point: it stops us reading
    noise as signal."""
    naive_scores = get_forecast_scores("naive")
    naive_briers = [s["brier"] for s in naive_scores if s["brier"] is not None]
    naive_mean = sum(naive_briers) / len(naive_briers) if naive_briers else None
    naive_se = _brier_se(naive_briers)

    out = {}
    for m in _MODEL_ORDER:
        scores = get_forecast_scores(m)
        briers = [s["brier"] for s in scores if s["brier"] is not None]
        n = len(briers)
        mean = sum(briers) / n if n else None
        se = _brier_se(briers)
        ci95 = round(1.96 * se, 4) if se is not None else None

        vs_naive = None
        beats_naive_sig = None
        if mean is not None and naive_mean is not None and m != "naive":
            vs_naive = round(mean - naive_mean, 4)
            if se is not None and naive_se is not None:
                comb = (se ** 2 + naive_se ** 2) ** 0.5
                beats_naive_sig = bool(vs_naive < -1.96 * comb)

        vs_uniform = round(mean - _UNIFORM_BRIER, 4) if mean is not None else None
        beats_uniform_sig = None
        if mean is not None and se is not None:
            beats_uniform_sig = bool((_UNIFORM_BRIER - mean) > 1.96 * se)

        calibration = {}
        if n:
            for k in ("widen", "stable", "narrow"):
                pred = sum(s["p_" + k] for s in scores if s["brier"] is not None) / n
                obs = sum(1 for s in scores if s["actual_outcome"] == k) / n
                calibration[k] = {"predicted": round(pred, 3), "observed": round(obs, 3)}

        out[m] = {
            "mean_brier": round(mean, 4) if mean is not None else None,
            "n_scored": n,
            "brier_ci95": ci95,
            "vs_naive": vs_naive,
            "beats_naive_significant": beats_naive_sig,
            "vs_uniform": vs_uniform,
            "beats_uniform_significant": beats_uniform_sig,
            "calibration": calibration,
        }
    return out


def _build_accuracy_json() -> str:
    return json.dumps({
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "note": (
            "Lower Brier is better. Uniform-random=0.667, perfect=0. brier_ci95 is "
            "the 95% confidence interval on the mean; 'significant' means the gap "
            "exceeds 1.96 standard errors. With n<30, treat every gap as provisional "
            "noise until it stays significant over months. calibration compares each "
            "model's average predicted probability to the observed frequency per class."
        ),
        "models": _model_stats(),
    }, indent=2)


def _build_root_readme() -> str:
    current = _query(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct "
        "FROM rates WHERE bcv_rate IS NOT NULL AND parallel_rate IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1"
    )
    if current:
        r = current[0]
        bcv_str = f"**{r['bcv_rate']:.2f}** VES/USD" if r["bcv_rate"] else "—"
        par_str = f"**{r['parallel_rate']:.2f}** VES/USD" if r["parallel_rate"] else "—"
        spread_str = f"**{r['spread_pct']:.1f}%**" if r["spread_pct"] else "—"
        updated_str = r["timestamp"][:16].replace("T", " ") + " UTC"
    else:
        bcv_str = par_str = spread_str = updated_str = "—"

    from db.db import get_latest_p2p_rate
    p2p = get_latest_p2p_rate()
    if p2p and p2p.get("mid_price") and current and current[0].get("parallel_rate"):
        parallel_rate = current[0]["parallel_rate"]
        diff_pct = (p2p["mid_price"] - parallel_rate) / parallel_rate * 100
        sign = "+" if diff_pct >= 0 else ""
        p2p_str = f"**{p2p['mid_price']:.2f}** VES/USDT ({sign}{diff_pct:.1f}% vs paralelo)"
    else:
        p2p_str = "—"

    forecast_str = "—"
    for preferred in ("stat", "naive"):
        s = _forecast_direction_display(preferred)
        if s != "—":
            forecast_str = s
            break

    stats = _model_stats()
    table_lines = []
    for m in _MODEL_ORDER:
        label = _MODEL_LABELS[m]
        st = stats.get(m, {})
        brier = st.get("mean_brier")
        ci = st.get("brier_ci95")
        n = st.get("n_scored", 0)
        if brier is None:
            brier_str = "—"
        elif ci:
            brier_str = f"{brier:.3f} ± {ci:.3f}"
        else:
            brier_str = f"{brier:.3f}"
        if m == "naive":
            vs_str = "—"
        elif st.get("vs_naive") is not None:
            diff = st["vs_naive"]
            sig = st.get("beats_naive_significant")
            mark = "✓" if (diff < 0 and sig) else ("≈" if diff < 0 else "✗")
            vs_str = f"{'−' if diff < 0 else '+'}{abs(diff):.3f} {mark}"
        else:
            vs_str = "—"
        table_lines.append(f"| {label} | {brier_str} | {vs_str} | {n} |")

    accuracy_table = "\n".join(table_lines)

    return f"""# Venezuela FX Monitor · Monitor Cambiario Venezuela

**English:** This bot doesn't just track the Venezuelan parallel exchange rate — it predicts it. Every morning it forecasts whether the spread between the BCV official rate and the parallel market will widen, hold, or narrow over the next 24 hours. Every forecast is automatically scored against what actually happened. The accuracy table below is a live track record, not a claim.

**Español:** Este bot no solo monitorea la tasa de cambio paralela venezolana — la predice. Cada mañana pronostica si la brecha entre la tasa oficial BCV y el mercado paralelo se ensanchará, se mantendrá o se estrechará en las próximas 24 horas. Cada pronóstico se puntúa automáticamente contra lo que realmente ocurrió. La tabla de precisión es un historial verificable en vivo, no una promesa.

---

## Today's Forecast · Pronóstico de Hoy

| Metric · Métrica | Value · Valor |
|---|---|
| BCV Oficial | {bcv_str} |
| Paralelo | {par_str} |
| Brecha · Spread | {spread_str} |
| Binance P2P USDT/VES | {p2p_str} |
| **Pronóstico 24h · 24h Forecast** | **{forecast_str}** |
| Actualizado · Updated | {updated_str} |

![Venezuela BCV vs parallel rate — last 30 days](data/chart.png)

---

## Live Accuracy Track Record · Historial de Precisión en Vivo

Every day two models compete on the same forecast: a **naive base-rate benchmark** (what usually happens) and a **stat challenger** (kernel-weighted analogs over recent spread dynamics plus oil, news, and payday-cycle signals). Each prediction is scored against the actual 24h outcome with the **Brier score** — lower is better; a random guess scores 0.667, a perfect forecaster 0.000. Scores are shown with a 95% confidence interval, and the challenger only counts as beating the baseline once the gap is statistically significant (✓; ≈ means ahead but not yet significant). Honest status: with only a few dozen scored forecasts so far, gaps this small are provisional — we report the uncertainty instead of hiding it.

Cada día compiten dos modelos sobre el mismo pronóstico: una **línea base** (lo que suele ocurrir) y un **retador stat** (analogías ponderadas sobre la dinámica reciente de la brecha más señales de petróleo, noticias y quincena). Cada predicción se puntúa contra el resultado real a 24h con el **Brier score** — menor es mejor; aleatorio 0.667, perfecto 0.000. Los puntajes se muestran con intervalo de confianza del 95%, y el retador solo cuenta como superior a la línea base cuando la diferencia es estadísticamente significativa (✓; ≈ significa por delante pero aún no significativo). Estado honesto: con apenas unas decenas de pronósticos evaluados, diferencias tan pequeñas son provisionales — reportamos la incertidumbre en vez de ocultarla.

| Model · Modelo | Brier ↓ | vs Baseline | Forecasts Scored · Evaluados |
|---|---|---|---|
{accuracy_table}

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
"""


def _build_readme() -> str:
    return """# Venezuela Currency Data

This folder is auto-generated by the [Venezuela Currency Agent](../).

## Files

| File | Description | Updated |
|------|-------------|---------|
| **chart.png** | Visual chart of last 30 days — rates and spread | Every scrape |
| **current.json** | Latest reading (machine-readable) | Every scrape |
| **recent.csv** | Last 7 days of raw 30-min scrapes | Every scrape |
| **daily.csv** | One row per day (avg/min/max) | Every scrape |
| **archive/rates-YYYY-MM.csv** | Full raw data, one file per month | Monthly |

## How to read this data

- **Quick visual check** → open `chart.png`
- **What is the rate right now?** → `current.json`
- **What happened this week?** → `recent.csv` (sortable in GitHub)
- **What's the long-term trend?** → `daily.csv`
- **Deep analysis** → download monthly archive into Excel

## Columns

**rates:**
- `timestamp` — UTC ISO 8601
- `bcv_rate` — Official rate from bcv.org.ve
- `parallel_rate` — Black market rate
- `spread_pct` — (parallel - bcv) / bcv × 100
- `source` — which scraper provided the parallel rate

**daily:**
- `day` — date (UTC)
- `avg_bcv`, `avg_parallel` — daily averages
- `avg_spread`, `min_spread`, `max_spread` — spread statistics
- `readings` — number of scrapes that day
"""


def export_to_github() -> bool:
    """Export data files to GitHub. Returns True if anything was committed."""
    if not os.getenv("GITHUB_TOKEN") or not os.getenv("GITHUB_REPO"):
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    # All data files in ONE atomic commit. commit_files skips entirely if
    # nothing changed, so a static 30-min window produces zero commits.
    files = [
        ("data/current.json", _build_current_json().encode("utf-8")),
        ("data/recent.csv", _build_recent_csv().encode("utf-8")),
        ("data/daily.csv", _build_daily_csv().encode("utf-8")),
        (f"data/archive/rates-{month}.csv", _build_archive_csv(month).encode("utf-8")),
        ("data/README.md", _build_readme().encode("utf-8")),
    ]
    return _commit_files(files, f"Data update {ts}")


def export_dashboard_to_github() -> bool:
    """Generate chart + commit dashboard files. Runs every 2h unconditionally —
    so README, forecast.json, and accuracy.json always stay fresh independent
    of whether rate data changed."""
    if not os.getenv("GITHUB_TOKEN") or not os.getenv("GITHUB_REPO"):
        return False
    import gc
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Textual dashboard — cheap, no matplotlib. Always part of the batch so a
    # matplotlib failure can never freeze the public README / forecast data.
    files = [
        ("data/forecast.json", _build_forecast_json().encode("utf-8")),
        ("data/accuracy.json", _build_accuracy_json().encode("utf-8")),
        ("README.md", _build_root_readme().encode("utf-8")),
    ]

    # Chart — heavy and best-effort. If it renders, it joins the SAME commit;
    # if it fails, the text files still commit (one commit either way).
    from reports.chart_generator import generate_chart  # deferred — matplotlib is heavy
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        if generate_chart(tmp_path, days=30):
            with open(tmp_path, "rb") as f:
                files.append(("data/chart.png", f.read()))
        else:
            logger.warning("Chart generation returned False — committing dashboard text only")
    except Exception as e:
        logger.error(f"Chart generation failed: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        gc.collect()

    return _commit_files(files, f"Dashboard update {ts}")
