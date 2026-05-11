"""Generates a PNG chart of recent rates and spread."""
import os
import logging
from datetime import datetime
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from db.db import get_connection

logger = logging.getLogger(__name__)


def _fetch_rates_for_chart(days: int = 30) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct "
        "FROM rates "
        "WHERE timestamp >= datetime('now', ?) "
        "ORDER BY timestamp ASC",
        (f"-{days} days",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def generate_chart(output_path: str, days: int = 30) -> bool:
    rows = _fetch_rates_for_chart(days)
    if len(rows) < 2:
        logger.info("Not enough data for chart yet")
        return False

    timestamps = []
    bcv, parallel, spread = [], [], []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (ValueError, TypeError):
            continue
        timestamps.append(ts)
        bcv.append(r["bcv_rate"])
        parallel.append(r["parallel_rate"])
        spread.append(r["spread_pct"])

    if not timestamps:
        return False

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                    gridspec_kw={"height_ratios": [2, 1]})

    fig.patch.set_facecolor("#1e1e1e")
    for ax in (ax1, ax2):
        ax.set_facecolor("#2d2d2d")
        ax.tick_params(colors="#cccccc")
        for spine in ax.spines.values():
            spine.set_color("#555555")
        ax.yaxis.label.set_color("#cccccc")
        ax.xaxis.label.set_color("#cccccc")
        ax.title.set_color("#ffffff")

    # Top: Rates
    ax1.plot(timestamps, bcv, label="BCV oficial", color="#4a9eff", linewidth=2)
    ax1.plot(timestamps, parallel, label="Paralelo", color="#ff6b6b", linewidth=2)
    ax1.set_ylabel("VES por USD")
    ax1.set_title(f"Venezuela — Tasas y brecha (últimos {days} días)", fontsize=14, fontweight="bold")
    leg = ax1.legend(loc="upper left", facecolor="#2d2d2d", edgecolor="#555555")
    for text in leg.get_texts():
        text.set_color("#ffffff")
    ax1.grid(True, alpha=0.15, color="#888888")

    # Bottom: Spread with threshold lines
    spread_clean = [s if s is not None else 0 for s in spread]
    ax2.fill_between(timestamps, spread_clean, color="#ffa500", alpha=0.3)
    ax2.plot(timestamps, spread_clean, color="#ffa500", linewidth=2)
    ax2.axhline(y=35, color="#ffd700", linestyle="--", alpha=0.7, label="Elevada (35%)")
    ax2.axhline(y=50, color="#ff6b35", linestyle="--", alpha=0.7, label="Crítica (50%)")
    ax2.axhline(y=75, color="#ff0033", linestyle="--", alpha=0.7, label="Emergencia (75%)")
    ax2.set_ylabel("Brecha %")
    ax2.set_xlabel("Fecha")
    leg2 = ax2.legend(loc="upper left", facecolor="#2d2d2d", edgecolor="#555555", fontsize=9)
    for text in leg2.get_texts():
        text.set_color("#ffffff")
    ax2.grid(True, alpha=0.15, color="#888888")

    # Format x-axis dates
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.savefig(output_path, dpi=80, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return True
