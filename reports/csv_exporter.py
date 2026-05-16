"""Exports rate data in a tiered structure for browsing on GitHub."""
import os
import json
import logging
import csv
import io
import tempfile
from datetime import datetime, timezone
from dotenv import load_dotenv
from db.db import get_connection, get_latest_forecast, get_forecast_brier_summary
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
        "WHERE timestamp >= datetime('now', '-7 days') "
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
_MODEL_ORDER = ["naive", "stat", "stat_v2", "stat_v3", "momentum", "markov", "ensemble"]
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


def _build_accuracy_json() -> str:
    rows = get_forecast_brier_summary()
    by_model = {r["model_name"]: r for r in rows}
    naive_brier = by_model.get("naive", {}).get("mean_brier")
    models_out = {}
    for m in _MODEL_ORDER:
        d = by_model.get(m, {})
        brier = d.get("mean_brier")
        n = int(d["n"]) if d.get("n") else 0
        vs_naive = None
        if brier is not None and naive_brier is not None and m != "naive":
            vs_naive = round(brier - naive_brier, 4)
        models_out[m] = {
            "mean_brier": round(brier, 4) if brier is not None else None,
            "n_scored": n,
            "vs_naive": vs_naive,
        }
    return json.dumps({
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "note": "Lower Brier is better. Random=0.667, Perfect=0.000. 30+ scored forecasts needed for significance.",
        "models": models_out,
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
    for preferred in ("ensemble", "stat_v3", "stat_v2", "stat", "naive"):
        s = _forecast_direction_display(preferred)
        if s != "—":
            forecast_str = s
            break

    accuracy_rows = get_forecast_brier_summary()
    by_model = {r["model_name"]: r for r in accuracy_rows}
    naive_brier = by_model.get("naive", {}).get("mean_brier")

    table_lines = []
    for m in _MODEL_ORDER:
        label = _MODEL_LABELS[m]
        d = by_model.get(m, {})
        brier = d.get("mean_brier")
        n = int(d["n"]) if d.get("n") else 0
        brier_str = f"{brier:.4f}" if brier is not None else "—"
        if m == "naive":
            vs_str = "—"
        elif brier is not None and naive_brier is not None:
            diff = brier - naive_brier
            sign = "−" if diff < 0 else "+"
            vs_str = f"{sign}{abs(diff):.4f} {'✓' if diff < 0 else '✗'}"
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

Every day, several models with deliberately different assumptions compete — a base-rate baseline, kernel-analog models on oil/news/payday signals, a trend-follower, and a regime-transition model. A diversified **ensemble** then blends them. The blend is reliable for a precise reason: its error equals the average member's error *minus how much the members disagree*, so combining models that fail in different situations scores better than any one alone. Each prediction is scored against the actual outcome using **Brier score** — lower is better, a random guess scores 0.667, a perfect forecaster 0.000. Any model that fails to beat the naive baseline gets rejected.

Cada día compiten varios modelos con supuestos deliberadamente distintos — una línea base, modelos de analogía sobre señales de petróleo/noticias/quincena, un seguidor de tendencia y un modelo de transición de régimen. Un **ensemble** diversificado los combina. La combinación es fiable por una razón precisa: su error es igual al error promedio de los miembros *menos cuánto difieren entre sí*, así que combinar modelos que fallan en situaciones distintas puntúa mejor que cualquiera por separado. Cada predicción se puntúa con **Brier score** — menor es mejor, aleatorio 0.667, perfecto 0.000. Cualquier modelo que no supere la línea base se descarta.

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
