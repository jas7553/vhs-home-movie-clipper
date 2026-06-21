"""
parse_timestamp() converts raw OCR text to a datetime.

Tape format: bottom line "M/ D/YY", top line "H:MM AM/PM", often split
across newlines with extra whitespace. The function must be robust to OCR
noise while still rejecting hallucinations (years outside 1985–2005,
month > 12, etc.).

Date-only readings are accepted (time falls back to midnight): the camcorder
overlay can be set to show the date without a time, producing long date-only
spans. Rejecting them made those spans invisible to boundary detection and
collapsed several real date changes into one clip. The "00:00 causes false
jumps" hazard is handled downstream (outlier filter + daily-mode date grouping).
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


class TestTimeWithSeconds:
    """H:MM:SS AM/PM — seconds tolerated, ignored downstream."""

    def test_seconds_pm(self):
        assert parse_timestamp("8:00:26 PM\n 1/ 4/90") == datetime(1990, 1, 4, 20, 0)

    def test_seconds_am(self):
        assert parse_timestamp("9:15:03 AM\n 3/15/95") == datetime(1995, 3, 15, 9, 15)

    def test_seconds_no_meridian_falls_back_to_midnight(self):
        # no AM/PM → ambiguous → keep date, midnight
        assert parse_timestamp("7:14:55\n 1/ 4/90") == datetime(1990, 1, 4, 0, 0)

    def test_seconds_do_not_corrupt_date(self):
        # ensure :26 is not mistaken for a date component
        result = parse_timestamp("8:00:26 PM\n11/26/92")
        assert result == datetime(1992, 11, 26, 20, 0)

    def test_existing_hhmm_still_works(self):
        assert parse_timestamp("5:01 PM\n 1/ 4/90") == datetime(1990, 1, 4, 17, 1)


class TestDateOnly:
    """Date-only readings (no time, or time without a meridian) fall back to
    midnight and keep the date — the camcorder overlay can show date-only."""

    def test_date_without_time(self):
        assert parse_timestamp("1/ 4/90") == datetime(1990, 1, 4, 0, 0)

    def test_date_only_compact(self):
        assert parse_timestamp("8/27/90") == datetime(1990, 8, 27, 0, 0)

    def test_time_without_meridian_keeps_date(self):
        # "7:14" with no AM/PM is ambiguous — drop the time, keep the date.
        assert parse_timestamp("7:14\n 1/ 4/90") == datetime(1990, 1, 4, 0, 0)

    def test_date_only_still_range_checks_year(self):
        assert parse_timestamp("1/ 1/79") is None    # 2079, out of range
        assert parse_timestamp("1/ 1/84") is None    # 1984, below floor


@pytest.mark.parametrize("text", [
    "",
    "blurry static ~~~",
    "5:01 PM",          # time without date
    "5:01 PM\n13/ 4/90",  # month > 12
    "5:01 PM\n 1/32/90",  # day > 31
    "5:01 PM\n1/ 1/1984", # 4-digit year below range
    "5:01 PM\n1/ 1/2006", # 4-digit year above range
    "5:01 PM\n1/ 1/79",   # 79 → 2079, outside 1985–2005
    "5:01 PM\n1/ 1/84",   # 84 → 1984, just below range floor
    "5:01 PM\n 1  4 90",  # space-only date separators (no "/") — misread, reject
    "11 5/90",            # month misread: space before first "/" only, no leading slash
])
def test_rejected(text: str):
    assert parse_timestamp(text) is None
