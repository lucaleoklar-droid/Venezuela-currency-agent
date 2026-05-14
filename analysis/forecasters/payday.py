"""Payday-cycle features.

Venezuelan workers are typically paid on the 15th and on the last day of each
month (private and public sector both follow this cadence). FX demand spikes
predictably in the days approaching payday — people buy dollars to preserve
purchasing power. So payday proximity is a real leading indicator for spread
moves that no rate-history feature can capture.

Two features are derived from any date:
  - days_since_last_payday : 0..14ish, monotonic since most recent payday
  - days_until_next_payday : 0..14ish, monotonic to next payday

Together they encode "where in the bimonthly cycle you are" without needing
trigonometric (sin/cos) encoding. The kernel-NN can find historical days that
sit at the same cycle-phase as today and weight their outcomes accordingly.

Pure function of date — no DB lookups, no external calls, no state.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _paydays_around(d: date) -> list[date]:
    """All paydays in the month before, month of, and month after d.
    Six dates total. Enough to find the nearest past and future payday from any
    point in the middle month."""
    days: list[date] = []
    # Prev month
    prev = (d.replace(day=1) - timedelta(days=1))
    days.append(date(prev.year, prev.month, 15))
    days.append(date(prev.year, prev.month, _last_day_of_month(prev.year, prev.month)))
    # Current month
    days.append(date(d.year, d.month, 15))
    days.append(date(d.year, d.month, _last_day_of_month(d.year, d.month)))
    # Next month
    nxt_first = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    days.append(date(nxt_first.year, nxt_first.month, 15))
    days.append(date(nxt_first.year, nxt_first.month, _last_day_of_month(nxt_first.year, nxt_first.month)))
    return sorted(days)


def payday_features(ts) -> tuple[int, int]:
    """Return (days_since_last_payday, days_until_next_payday) for the given
    timestamp. `ts` accepts a datetime, a date, or an ISO string.

    On a payday itself, days_since=0 and days_until = days to the NEXT payday
    (not zero), because "today is payday" is its own state distinct from
    "approaching payday" or "post-payday."
    """
    if isinstance(ts, str):
        # tolerate trailing Z
        if ts.endswith("Z"):
            ts = ts[:-1]
        ts = datetime.fromisoformat(ts)
    if isinstance(ts, datetime):
        d = ts.date()
    else:
        d = ts  # assume date

    paydays = _paydays_around(d)

    past = [p for p in paydays if p <= d]
    future = [p for p in paydays if p > d]

    # Guaranteed non-empty by construction of _paydays_around — months on both sides.
    days_since = (d - past[-1]).days
    days_until = (future[0] - d).days
    return days_since, days_until
