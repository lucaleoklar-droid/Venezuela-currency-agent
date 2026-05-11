"""Generates a PNG chart of recent rates and spread."""
import logging
from datetime import datetime, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from db.db import get_connection
from analysis.analyzer import SPREAD_ELEVATED, SPREAD_CRITICAL, SPREAD_EMERGENCY

logger = logging.getLogger(__name__)

BG = "#1a1a1a"
PANEL = "#242424"
GRID = "#3a3a3a"
TEXT = "#e5e5e5"
MUTED = "#9a9a9a"
BCV_COLOR = "#5aa3ff"
PARALLEL_COLOR = "#ff7a7a"
SPREAD_COLOR = "#ffb547"
NORMAL_BAND = "#1f4d1f"
ELEVATED_BAND = "#4d3f1f"
CRITICAL_BAND = "#4d2a1f"
EMERGENCY_BAND = "#4d1f1f"


def _fetch_rates(days: int) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct "
        "FROM rates WHERE timestamp >= datetime('now', ?) "
        "ORDER BY timestamp ASC",
        (f"-{days} days",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _pick_window_and_formatter(span_hours: float):
    if span_hours <= 36:
        return f"últimas {round(span_hours)} horas", mdates.DateFormatter("%H:%M"), mdates.HourLocator(interval=max(1, int(span_hours / 8)))
    if span_hours <= 24 * 7:
        return f"últimos {round(span_hours / 24)} días", mdates.DateFormatter("%d %b %H:%M"), mdates.DayLocator()
    days = round(span_hours / 24)
    return f"últimos {days} días", mdates.DateFormatter("%d %b"), mdates.AutoDateLocator()


def _annotate_last(ax, x, y, text, color):
    ax.annotate(
        text, xy=(x, y), xytext=(6, 0), textcoords="offset points",
        color=color, fontsize=9, fontweight="bold", va="center",
    )


def generate_chart(output_path: str, days: int = 30) -> bool:
    rows = _fetch_rates(days)
    if len(rows) < 2:
        logger.info("Not enough data for chart yet")
        return False

    ts, bcv, parallel, spread = [], [], [], []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["timestamp"])
        except (ValueError, TypeError):
            continue
        if r["bcv_rate"] is None and r["parallel_rate"] is None:
            continue
        ts.append(t)
        bcv.append(r["bcv_rate"])
        parallel.append(r["parallel_rate"])
        spread.append(r["spread_pct"])

    if len(ts) < 2:
        return False

    span_hours = (ts[-1] - ts[0]).total_seconds() / 3600
    window_label, date_fmt, locator = _pick_window_and_formatter(span_hours)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1]},
    )
    fig.patch.set_facecolor(BG)

    for ax in (ax1, ax2):
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=MUTED, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.yaxis.label.set_color(TEXT)
        ax.xaxis.label.set_color(TEXT)
        ax.grid(True, which="major", alpha=0.25, color=GRID, linewidth=0.7)

    bcv_clean = [(t, v) for t, v in zip(ts, bcv) if v is not None]
    par_clean = [(t, v) for t, v in zip(ts, parallel) if v is not None]

    if bcv_clean:
        xs, ys = zip(*bcv_clean)
        ax1.plot(xs, ys, label="BCV oficial", color=BCV_COLOR, linewidth=2.2, marker="o", markersize=3, markevery=max(1, len(xs) // 30))
        _annotate_last(ax1, xs[-1], ys[-1], f" {ys[-1]:.2f}", BCV_COLOR)

    if par_clean:
        xs, ys = zip(*par_clean)
        ax1.plot(xs, ys, label="Paralelo", color=PARALLEL_COLOR, linewidth=2.2, marker="o", markersize=3, markevery=max(1, len(xs) // 30))
        _annotate_last(ax1, xs[-1], ys[-1], f" {ys[-1]:.2f}", PARALLEL_COLOR)

    all_rates = [v for v in bcv + parallel if v is not None]
    if all_rates:
        lo, hi = min(all_rates), max(all_rates)
        pad = max((hi - lo) * 0.12, hi * 0.01)
        ax1.set_ylim(lo - pad, hi + pad)

    ax1.set_ylabel("VES por USD", color=TEXT, fontsize=10)

    latest_bcv = bcv_clean[-1][1] if bcv_clean else None
    latest_par = par_clean[-1][1] if par_clean else None
    latest_spread = next((s for s in reversed(spread) if s is not None), None)
    subtitle_parts = [window_label]
    if latest_bcv is not None and latest_par is not None:
        subtitle_parts.append(f"BCV {latest_bcv:.2f} · Paralelo {latest_par:.2f}")
    if latest_spread is not None:
        subtitle_parts.append(f"Brecha {latest_spread:.1f}%")
    fig.text(0.07, 0.955, "Venezuela — Tasas BCV vs paralelo",
             color=TEXT, fontsize=15, fontweight="bold", ha="left", va="bottom")
    fig.text(0.07, 0.935, "  ·  ".join(subtitle_parts),
             color=MUTED, fontsize=10, ha="left", va="top")

    leg = ax1.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=TEXT)

    spread_clean = [(t, s) for t, s in zip(ts, spread) if s is not None]
    if spread_clean:
        xs_s, ys_s = zip(*spread_clean)
        s_min, s_max = min(ys_s), max(ys_s)
        y_lo = max(0, s_min - 4)
        y_hi = max(s_max + 6, SPREAD_ELEVATED + 4)
        ax2.set_ylim(y_lo, y_hi)
        x_lo = mdates.date2num(ts[0])
        x_hi = mdates.date2num(ts[-1])
        ax2.axhspan(0, SPREAD_ELEVATED, facecolor=NORMAL_BAND, alpha=0.35, zorder=0)
        ax2.axhspan(SPREAD_ELEVATED, SPREAD_CRITICAL, facecolor=ELEVATED_BAND, alpha=0.55, zorder=0)
        ax2.axhspan(SPREAD_CRITICAL, SPREAD_EMERGENCY, facecolor=CRITICAL_BAND, alpha=0.55, zorder=0)
        ax2.axhspan(SPREAD_EMERGENCY, 200, facecolor=EMERGENCY_BAND, alpha=0.55, zorder=0)
        for y, label, line_color in [
            (SPREAD_ELEVATED, "Elevada", "#d4a017"),
            (SPREAD_CRITICAL, "Crítica", "#d4631a"),
            (SPREAD_EMERGENCY, "Emergencia", "#d41a1a"),
        ]:
            if y_lo <= y <= y_hi:
                ax2.axhline(y=y, color=line_color, linestyle="--", linewidth=1.0, alpha=0.85, zorder=1)
                ax2.text(x_hi, y, f" {label} {y}%", color=line_color, fontsize=8,
                         fontweight="bold", va="center", ha="left", zorder=2)
        ax2.fill_between(xs_s, ys_s, y_lo, color=SPREAD_COLOR, alpha=0.18, zorder=2)
        ax2.plot(xs_s, ys_s, color=SPREAD_COLOR, linewidth=2.2, zorder=3)
        _annotate_last(ax2, xs_s[-1], ys_s[-1], f" {ys_s[-1]:.1f}%", SPREAD_COLOR)

    ax2.set_ylabel("Brecha %", color=TEXT, fontsize=10)
    ax2.set_xlabel("")

    ax2.xaxis.set_major_locator(locator)
    ax2.xaxis.set_major_formatter(date_fmt)
    fig.autofmt_xdate(rotation=0, ha="center")

    fig.text(0.99, 0.01,
             f"Generado {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
             color=MUTED, fontsize=7, ha="right", va="bottom")

    plt.subplots_adjust(left=0.07, right=0.92, top=0.88, bottom=0.08, hspace=0.18)
    plt.savefig(output_path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    return True
