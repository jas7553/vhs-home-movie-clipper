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

    def test_spaced_date_separators(self):
        # OCR sometimes reads "/" as " " — the pattern allows whitespace separators
        assert parse_timestamp("5:01 PM\n 1  4 90") == datetime(1990, 1, 4, 17, 1)


class TestNoonAndMidnight:
    def test_12pm_is_noon(self):
        # 12 PM → hour stays 12 (not 24)
        assert parse_timestamp("12:00 PM\n6/ 1/99") == datetime(1999, 6, 1, 12, 0)

    def test_12am_is_midnight(self):
        # 12 AM → hour becomes 0
        assert parse_timestamp("12:00 AM\n6/ 1/99") == datetime(1999, 6, 1, 0, 0)


class TestYearNormalization:
    """Two-digit years: >= 80 → 1900s, < 80 → 2000s (VHS era heuristic)."""

    def test_90s(self):
        assert parse_timestamp("1:00 PM\n1/ 1/90").year == 1990  # type: ignore[union-attr]

    def test_earliest_valid_85(self):
        assert parse_timestamp("1:00 PM\n1/ 1/85").year == 1985  # type: ignore[union-attr]

    def test_2000s_two_digit(self):
        assert parse_timestamp("1:00 PM\n1/ 1/01").year == 2001  # type: ignore[union-attr]

    def test_latest_valid_05(self):
        assert parse_timestamp("1:00 PM\n1/ 1/05").year == 2005  # type: ignore[union-attr]

    def test_four_digit_year_in_range(self):
        assert parse_timestamp("1:00 PM\n1/ 1/1990").year == 1990  # type: ignore[union-attr]


class TestRejectedInputs:
    def test_empty_string(self):
        assert parse_timestamp("") is None

    def test_noise_only(self):
        assert parse_timestamp("blurry static ~~~") is None

    def test_date_without_time(self):
        # Reject: defaulting to 00:00 would cause false forward jumps
        assert parse_timestamp("1/ 4/90") is None

    def test_time_without_date(self):
        assert parse_timestamp("5:01 PM") is None

    def test_month_gt_12(self):
        assert parse_timestamp("5:01 PM\n13/ 4/90") is None

    def test_day_gt_31(self):
        assert parse_timestamp("5:01 PM\n 1/32/90") is None

    def test_year_before_range_4digit(self):
        assert parse_timestamp("5:01 PM\n1/ 1/1984") is None

    def test_year_after_range_4digit(self):
        assert parse_timestamp("5:01 PM\n1/ 1/2006") is None

    def test_two_digit_year_79_maps_to_2079(self):
        # 79 → 2079, which is outside 1985–2005
        assert parse_timestamp("5:01 PM\n1/ 1/79") is None

    def test_two_digit_year_84_maps_to_1984(self):
        # 84 → 1984, just below the range floor
        assert parse_timestamp("5:01 PM\n1/ 1/84") is None
