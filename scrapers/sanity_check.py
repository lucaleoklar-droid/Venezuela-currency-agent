import math
import logging
from db.db import get_latest_rate

logger = logging.getLogger(__name__)

BCV_MIN, BCV_MAX = 1, 100_000
PARALLEL_MIN, PARALLEL_MAX = 1, 200_000
MAX_REL_CHANGE = 0.50  # ±50% vs last known good
BCV_OVER_PARALLEL_TOL = 0.05  # BCV may not exceed parallel by >5%


def validate_rate(new_rate: float, source: str, rate_type: str) -> tuple[bool, str | None]:
    """Validate a freshly-scraped rate before storage. Returns (is_valid, reason)."""
    if new_rate is None or not isinstance(new_rate, (int, float)) or not math.isfinite(float(new_rate)) or new_rate <= 0:
        reason = f"invalid value: {new_rate!r}"
        logger.warning("reject %s/%s: %s", source, rate_type, reason)
        return False, reason

    new_rate = float(new_rate)

    if rate_type == "bcv":
        lo, hi = BCV_MIN, BCV_MAX
    elif rate_type == "parallel":
        lo, hi = PARALLEL_MIN, PARALLEL_MAX
    else:
        reason = f"unknown rate_type: {rate_type!r}"
        logger.warning("reject %s/%s: %s", source, rate_type, reason)
        return False, reason

    if not (lo <= new_rate <= hi):
        reason = f"out of bounds [{lo}, {hi}]: {new_rate}"
        logger.warning("reject %s/%s: %s", source, rate_type, reason)
        return False, reason

    latest = get_latest_rate()

    if latest:
        prev = latest.get(f"{rate_type}_rate")
        if prev and prev > 0:
            change = abs(new_rate - prev) / prev
            if change > MAX_REL_CHANGE:
                reason = f"±{int(MAX_REL_CHANGE*100)}% deviation from last {prev}: {new_rate} ({change*100:.1f}%)"
                logger.warning("reject %s/%s: %s", source, rate_type, reason)
                return False, reason

    if rate_type == "bcv" and latest:
        parallel = latest.get("parallel_rate")
        if parallel and parallel > 0 and new_rate > parallel * (1 + BCV_OVER_PARALLEL_TOL):
            reason = f"BCV {new_rate} exceeds parallel {parallel} by >5%"
            logger.warning("reject %s/%s: %s", source, rate_type, reason)
            return False, reason

    logger.info("accept %s/%s: %s", source, rate_type, new_rate)
    return True, None


if __name__ == "__main__":
    import sys
    from unittest.mock import patch

    cases = [
        ("valid bcv near last", 510.0, "bcv.org.ve", "bcv", {"bcv_rate": 500.0, "parallel_rate": 650.0}, True),
        ("None", None, "x", "parallel", None, False),
        ("zero", 0, "x", "parallel", None, False),
        ("negative", -5.0, "x", "parallel", None, False),
        ("NaN", float("nan"), "x", "parallel", None, False),
        ("far outside last known", 5000.0, "x", "parallel", {"bcv_rate": 500.0, "parallel_rate": 650.0}, False),
        ("BCV > parallel + 5%", 720.0, "bcv.org.ve", "bcv", {"bcv_rate": 700.0, "parallel_rate": 650.0}, False),
    ]

    logging.basicConfig(level=logging.WARNING)
    fails = 0
    for name, val, src, rtype, latest_stub, expected_valid in cases:
        with patch(f"{__name__}.get_latest_rate", return_value=latest_stub):
            ok, reason = validate_rate(val, src, rtype)
        passed = ok == expected_valid
        if not passed:
            fails += 1
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: valid={ok} reason={reason}")

    sys.exit(1 if fails else 0)
