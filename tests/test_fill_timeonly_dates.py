"""
fill_timeonly_dates() — assigns dates to time-only OCR readings.

Applies the chronological-tape assumption: a time-only reading (AM/PM time, no
date) inherits the nearest dated predecessor's date.  When the camera clock
wraps backward by more than 12 hours the running date increments (midnight
crossing).  Successor date is used when there is no predecessor.  Unresolvable
entries (no dated neighbor on either side) remain None.
"""
from datetime import datetime

from split_homevideo import fill_timeonly_dates


def _d(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute)


class TestForwardFill:
    """Time-only entries after a dated reading inherit the predecessor's date."""

    def test_single_timeonly_after_dated(self):
        raw = [
            (100.0, _d(1992, 9, 25, 11, 28), "11:28 AM\n9/25/92"),
            (200.0, None, "8:00 PM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, _d(1992, 9, 25, 20, 0))

    def test_time_with_seconds(self):
        raw = [
            (100.0, _d(1992, 9, 25, 11, 28), "11:28 AM\n9/25/92"),
            (200.0, None, "8:00:26 PM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, _d(1992, 9, 25, 20, 0))

    def test_multiple_timeonly_entries_same_date(self):
        raw = [
            (100.0, _d(1992, 9, 25, 10, 0), "10:00 AM\n9/25/92"),
            (200.0, None, "2:00 PM"),
            (300.0, None, "5:00 PM"),
            (400.0, None, "8:00 PM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, _d(1992, 9, 25, 14, 0))
        assert result[2] == (300.0, _d(1992, 9, 25, 17, 0))
        assert result[3] == (400.0, _d(1992, 9, 25, 20, 0))

    def test_none_gaps_between_timeonly_entries(self):
        """None entries (unreadable windows) within a time-only span don't break fill."""
        raw = [
            (100.0, _d(1992, 9, 25, 10, 0), "10:00 AM\n9/25/92"),
            (200.0, None, "2:00 PM"),
            (300.0, None, None),
            (400.0, None, "8:00 PM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, _d(1992, 9, 25, 14, 0))
        assert result[2] == (300.0, None)   # unreadable window stays None
        assert result[3] == (400.0, _d(1992, 9, 25, 20, 0))

    def test_dated_entry_not_modified(self):
        raw = [
            (100.0, _d(1992, 9, 25, 11, 28), "11:28 AM\n9/25/92"),
            (200.0, None, "8:00 PM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[0] == (100.0, _d(1992, 9, 25, 11, 28))


class TestMidnightCrossing:
    """When camera clock wraps backward by > 12 hours, date increments."""

    def test_midnight_crossing_advances_date(self):
        raw = [
            (100.0, _d(1992, 9, 25, 23, 30), "11:30 PM\n9/25/92"),
            (200.0, None, "12:01 AM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, _d(1992, 9, 26, 0, 1))

    def test_small_backward_no_increment(self):
        """Going back 2 hours does not trigger a date increment (within 12-hr threshold)."""
        raw = [
            (100.0, _d(1992, 9, 25, 11, 0), "11:00 AM\n9/25/92"),
            (200.0, None, "9:00 AM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, _d(1992, 9, 25, 9, 0))

    def test_midnight_crossing_chains_across_span(self):
        """A post-midnight entry sets last_effective; subsequent entries build on it."""
        raw = [
            (100.0, _d(1992, 9, 25, 23, 30), "11:30 PM\n9/25/92"),
            (200.0, None, "12:01 AM"),
            (300.0, None, "1:00 AM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, _d(1992, 9, 26, 0, 1))
        assert result[2] == (300.0, _d(1992, 9, 26, 1, 0))


class TestSuccessorFallback:
    """When no dated predecessor exists, use the nearest dated successor."""

    def test_timeonly_before_any_dated(self):
        raw = [
            (100.0, None, "8:00 PM"),
            (200.0, _d(1992, 9, 25, 11, 28), "11:28 AM\n9/25/92"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[0] == (100.0, _d(1992, 9, 25, 20, 0))

    def test_only_timeonly_no_dated_stays_none(self):
        raw = [
            (100.0, None, "8:00 PM"),
            (200.0, None, "9:00 PM"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[0] == (100.0, None)
        assert result[1] == (200.0, None)


class TestEdgeCases:
    def test_empty_input(self):
        assert fill_timeonly_dates([]) == []

    def test_all_none_no_text(self):
        raw = [(10.0, None, None), (20.0, None, None)]
        result = fill_timeonly_dates(raw)
        assert result == [(10.0, None), (20.0, None)]

    def test_no_meridian_text_stays_none(self):
        """Time without AM/PM is ambiguous — leave as None."""
        raw = [
            (100.0, _d(1992, 9, 25, 10, 0), "10:00 AM\n9/25/92"),
            (200.0, None, "8:00"),
        ]
        result = fill_timeonly_dates(raw)
        assert result[1] == (200.0, None)

    def test_dated_entries_unchanged(self):
        dt = _d(1992, 9, 25, 11, 28)
        raw = [(100.0, dt, "11:28 AM\n9/25/92")]
        result = fill_timeonly_dates(raw)
        assert result == [(100.0, dt)]
