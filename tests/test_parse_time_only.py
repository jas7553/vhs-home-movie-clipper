"""
_parse_time_only() — extracts (hour24, minute) from time-only OCR text.

Only fires when the text has a meridian (AM/PM) time but NO parseable date.
Returns None when a date is also present (parse_timestamp handles those), when
no meridian is present (ambiguous hour), or when the text is unreadable.
"""
from split_homevideo import _parse_time_only


class TestTimeOnlyParsing:
    def test_pm_basic(self):
        assert _parse_time_only("8:00 PM") == (20, 0)

    def test_am_basic(self):
        assert _parse_time_only("9:30 AM") == (9, 30)

    def test_12pm_is_noon(self):
        assert _parse_time_only("12:00 PM") == (12, 0)

    def test_12am_is_midnight(self):
        assert _parse_time_only("12:00 AM") == (0, 0)

    def test_with_seconds(self):
        assert _parse_time_only("8:00:26 PM") == (20, 0)

    def test_lowercase_ampm(self):
        assert _parse_time_only("3:15 pm") == (15, 15)

    def test_multiline_timeonly(self):
        assert _parse_time_only("8:00:26 PM\n") == (20, 0)


class TestRejected:
    def test_time_with_numeric_date_returns_none(self):
        # parse_timestamp handles these; _parse_time_only must not duplicate
        assert _parse_time_only("5:01 PM\n 1/ 4/90") is None

    def test_time_with_word_month_date_returns_none(self):
        assert _parse_time_only("8:00 PM\nNOV. 26 1992") is None

    def test_no_meridian_returns_none(self):
        assert _parse_time_only("8:00") is None

    def test_no_meridian_with_seconds_returns_none(self):
        assert _parse_time_only("8:00:26") is None

    def test_empty_returns_none(self):
        assert _parse_time_only("") is None

    def test_garbage_returns_none(self):
        assert _parse_time_only("blurry static ~~~") is None

    def test_date_only_no_time_returns_none(self):
        assert _parse_time_only("1/ 4/90") is None
