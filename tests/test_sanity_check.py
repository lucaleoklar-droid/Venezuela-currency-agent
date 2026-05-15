"""Sanity check: the validate_rate gate that protects the DB from scraper bugs.
Heavy use of monkeypatch to stub get_latest_rate without touching the DB."""
import math
import pytest

from scrapers import sanity_check
from scrapers.sanity_check import validate_rate


@pytest.fixture
def no_history(monkeypatch):
    """No previous reading at all — first scrape on a fresh DB."""
    monkeypatch.setattr(sanity_check, "get_latest_rate", lambda: None)


@pytest.fixture
def with_history(monkeypatch):
    """Recent history: BCV=500, parallel=650 (a calm baseline)."""
    monkeypatch.setattr(
        sanity_check, "get_latest_rate",
        lambda: {"bcv_rate": 500.0, "parallel_rate": 650.0},
    )


class TestInvalidValues:
    def test_none_rejected(self, no_history):
        ok, reason = validate_rate(None, "x", "parallel")
        assert ok is False
        assert "invalid" in reason

    def test_zero_rejected(self, no_history):
        ok, reason = validate_rate(0, "x", "parallel")
        assert ok is False

    def test_negative_rejected(self, no_history):
        ok, _ = validate_rate(-5.0, "x", "parallel")
        assert ok is False

    def test_nan_rejected(self, no_history):
        ok, _ = validate_rate(float("nan"), "x", "parallel")
        assert ok is False

    def test_infinity_rejected(self, no_history):
        ok, _ = validate_rate(math.inf, "x", "parallel")
        assert ok is False


class TestRangeBounds:
    def test_parallel_in_range_accepted(self, no_history):
        ok, _ = validate_rate(650.0, "x", "parallel")
        assert ok is True

    def test_parallel_too_high_rejected(self, no_history):
        ok, reason = validate_rate(300_000.0, "x", "parallel")
        assert ok is False
        assert "out of bounds" in reason

    def test_bcv_too_high_rejected(self, no_history):
        ok, reason = validate_rate(150_000.0, "x", "bcv")
        assert ok is False

    def test_unknown_rate_type_rejected(self, no_history):
        ok, reason = validate_rate(500.0, "x", "moon")
        assert ok is False
        assert "unknown" in reason


class TestRelativeChange:
    def test_small_move_accepted_clean(self, with_history):
        ok, reason = validate_rate(660.0, "x", "parallel")
        assert ok is True
        assert reason is None

    def test_suspect_move_accepted_with_flag(self, with_history):
        # 650 -> 1100 is ~69% move → above SUSPECT (50%) but below HARD (200%)
        ok, reason = validate_rate(1100.0, "x", "parallel")
        assert ok is True
        assert reason is not None
        assert "suspect" in reason

    def test_hard_move_rejected(self, with_history):
        # 650 -> 5000 is 7.7x → above HARD cap
        ok, reason = validate_rate(5000.0, "x", "parallel")
        assert ok is False
        assert "hard cap" in reason


class TestBcvVsParallelGuard:
    def test_bcv_above_parallel_by_more_than_5pct_rejected(self, with_history):
        # parallel=650 → BCV cap = 650 * 1.05 = 682.5; 720 exceeds
        ok, reason = validate_rate(720.0, "bcv.org.ve", "bcv")
        assert ok is False
        assert "exceeds parallel" in reason

    def test_bcv_within_5pct_of_parallel_accepted(self, with_history):
        # 670 is below 682.5 — fine
        ok, _ = validate_rate(670.0, "bcv.org.ve", "bcv")
        assert ok is True
