"""
parse_timestamp() converts raw OCR text to a datetime.

Tape format: bottom line "M/ D/YY", top line "H:MM AM/PM", often split
across newlines with extra whitespace. The function must be robust to OCR
noise while still rejecting hallucinations (years outside 1985–2005,
month > 12, missing time, etc.).

No-time rejection is intentional: defaulting to 00:00 would produce huge
false time-jumps that trigger spurious clip splits.
"""
from datetime import datetime

import pytest

from split_homevideo import parse_timestamp


class TestCanonicalFormats:
    def test_multiline_pm(self):
        assert parse_timestamp("5:01 PM\n 1/ 4/90") == datetime(1990, 1, 4, 17, 1)

    def test_multiline_am(self):
        assert parse_timestamp("9:30 AM\n 3/15/95") == datetime(1995, 3, 15, 9, 30)

    def test_single_line(self):
        assert parse_timestamp("2:30 PM 5/10/01") == datetime(2001, 5, 10, 14, 30)

    def test_lowercase_ampm(self):
        assert parse_timestamp("3:15 pm\n6/ 7/88") == datetime(1988, 6, 7, 15, 15)

    def test_spaced_date_separators_with_slashes(self):
        # spaces around "/" are fine: "1 / 4/90" still has the required slash
        assert parse_timestamp("5:01 PM\n 1 / 4/90") == datetime(1990, 1, 4, 17, 1)


class TestNoonAndMidnight:
    def test_12pm_is_noon(self):
        # 12 PM → hour stays 12 (not 24)
        assert parse_timestamp("12:00 PM\n6/ 1/99") == datetime(1999, 6, 1, 12, 0)

    def test_12am_is_midnight(self):
        # 12 AM → hour becomes 0
        assert parse_timestamp("12:00 AM\n6/ 1/99") == datetime(1999, 6, 1, 0, 0)


class TestYearNormalization:
    """Two-digit years: >= 80 → 1900s, < 80 → 2000s (VHS era heuristic)."""

    @pytest.mark.parametrize("text,expected_year", [
        ("1:00 PM\n1/ 1/90", 1990),
        ("1:00 PM\n1/ 1/85", 1985),   # floor of valid range
        ("1:00 PM\n1/ 1/01", 2001),
        ("1:00 PM\n1/ 1/05", 2005),   # ceiling of valid range
        ("1:00 PM\n1/ 1/1990", 1990), # four-digit passthrough
    ])
    def test_year(self, text: str, expected_year: int):
        assert parse_timestamp(text).year == expected_year  # type: ignore[union-attr]


class TestInvalidDateCombination:
    def test_feb_30_returns_none(self):
        # Passes month/day range checks but datetime() raises ValueError
        assert parse_timestamp("5:01 PM\n2/30/90") is None


@pytest.mark.parametrize("text", [
    "",
    "blurry static ~~~",
    "1/ 4/90",          # date without time — 00:00 default would cause false jumps
    "5:01 PM",          # time without date
    "5:01 PM\n13/ 4/90",  # month > 12
    "5:01 PM\n 1/32/90",  # day > 31
    "5:01 PM\n1/ 1/1984", # 4-digit year below range
    "5:01 PM\n1/ 1/2006", # 4-digit year above range
    "5:01 PM\n1/ 1/79",   # 79 → 2079, outside 1985–2005
    "5:01 PM\n1/ 1/84",   # 84 → 1984, just below range floor
    "5:01 PM\n 1  4 90",  # space-only date separators (no "/") — misread, reject
    "7:14\n 1/ 4/90",     # missing AM/PM — ambiguous, reject
    "11 5/90",            # month misread: space before first "/" only, no leading slash
])
def test_rejected(text: str):
    assert parse_timestamp(text) is None
