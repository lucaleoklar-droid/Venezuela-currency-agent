"""Parsing helpers — pure functions, easy targets, high-value coverage.

These functions are called on every scrape / every Telegram message, so a
silent regression here corrupts the entire dataset or the user interface."""
import pytest

from scrapers.parallel_scraper import parse_rate as parse_rate_parallel
from scrapers.bcv_scraper import parse_rate as parse_rate_bcv
from alerts.telegram_poller import _parse_amount
from reports.daily_brief import parse_and_enforce_action


# ---------------------------------------------------------------------------
# parse_rate — both scrapers share the same logic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("parse_rate", [parse_rate_parallel, parse_rate_bcv])
class TestParseRate:
    def test_simple_decimal(self, parse_rate):
        assert parse_rate("123.45") == 123.45

    def test_spanish_decimal_comma(self, parse_rate):
        # Lone comma → decimal separator
        assert parse_rate("123,45") == 123.45

    def test_spanish_thousands_and_decimal(self, parse_rate):
        # "1.234,56" → 1234.56 (period as thousands, comma as decimal)
        assert parse_rate("1.234,56") == 1234.56

    def test_strips_currency_symbol(self, parse_rate):
        assert parse_rate("Bs 1.234,56") == 1234.56

    def test_strips_whitespace(self, parse_rate):
        assert parse_rate("  500,00  ") == 500.0

    def test_rejects_empty(self, parse_rate):
        assert parse_rate("") is None
        assert parse_rate(None) is None

    def test_rejects_out_of_range_low(self, parse_rate):
        # MIN_RATE = 1.0 — values <= 1 should be rejected (likely parse error)
        assert parse_rate("0.5") is None

    def test_rejects_out_of_range_high(self, parse_rate):
        # MAX_RATE = 1_000_000 — anything beyond is implausible
        assert parse_rate("9999999.99") is None

    def test_handles_nbsp(self, parse_rate):
        # Source pages sometimes embed nbsp between digits
        assert parse_rate("1\xa0234,56") == 1234.56


# ---------------------------------------------------------------------------
# _parse_amount — user-typed numbers in Telegram messages
# ---------------------------------------------------------------------------

class TestParseAmount:
    def test_plain_int(self):
        assert _parse_amount("convertir 100000") == 100000.0

    def test_plain_decimal(self):
        assert _parse_amount("precio 50.5") == 50.5

    def test_spanish_thousands_dot(self):
        # "100.000" -> 100000 (Spanish thousands)
        assert _parse_amount("convertir 100.000") == 100000.0

    def test_spanish_thousands_comma_three_digits(self):
        # "50,000" with 3 trailing digits -> thousands
        assert _parse_amount("precio 50,000") == 50000.0

    def test_decimal_comma_two_digits(self):
        # "50,5" -> decimal
        assert _parse_amount("precio 50,5") == 50.5

    def test_mixed_dot_comma_european(self):
        # "1.234,56" -> 1234.56 (comma is decimal because rightmost)
        assert _parse_amount("convertir 1.234,56") == 1234.56

    def test_mixed_dot_comma_us(self):
        # "1,234.56" -> 1234.56 (dot is decimal because rightmost)
        assert _parse_amount("convertir 1,234.56") == 1234.56

    def test_no_number_in_text(self):
        assert _parse_amount("convertir") is None

    def test_picks_first_number(self):
        # The regex grabs the first numeric token
        assert _parse_amount("convertir 500 y luego 1000") == 500.0


# ---------------------------------------------------------------------------
# parse_and_enforce_action — daily brief action prefix extraction
# ---------------------------------------------------------------------------

class TestParseAndEnforceAction:
    def test_canonical_convertir(self):
        text = "Acción: CONVERTIR\nResumen del día."
        action, _ = parse_and_enforce_action(text)
        assert action == "CONVERTIR"

    def test_canonical_esperar(self):
        text = "Acción: ESPERAR\nLa brecha sigue ampliándose."
        action, _ = parse_and_enforce_action(text)
        assert action == "ESPERAR"

    def test_canonical_neutral(self):
        text = "Acción: NEUTRAL"
        action, _ = parse_and_enforce_action(text)
        assert action == "NEUTRAL"

    def test_lowercase_accepted(self):
        text = "acción: convertir\nresto..."
        action, _ = parse_and_enforce_action(text)
        assert action == "CONVERTIR"

    def test_missing_accent(self):
        text = "Accion: ESPERAR\nresto..."
        action, _ = parse_and_enforce_action(text)
        assert action == "ESPERAR"

    def test_disobedient_model_defaults_to_neutral(self):
        text = "El paralelo subió hoy. Recomiendo esperar."
        action, normalized = parse_and_enforce_action(text)
        assert action == "NEUTRAL"
        assert normalized.startswith("Acción: NEUTRAL")

    def test_empty_input(self):
        action, normalized = parse_and_enforce_action("")
        assert action == "NEUTRAL"
        assert "Acción: NEUTRAL" in normalized

    def test_none_input(self):
        action, normalized = parse_and_enforce_action(None)
        assert action == "NEUTRAL"

    def test_action_in_later_line_fallback(self):
        # Model puts a preamble before the action — fallback should still find it
        text = "Aquí el informe.\nAcción: CONVERTIR\nResto del análisis."
        action, _ = parse_and_enforce_action(text)
        assert action == "CONVERTIR"

    def test_invalid_action_word_defaults(self):
        text = "Acción: VENDER\n..."
        action, _ = parse_and_enforce_action(text)
        assert action == "NEUTRAL"
