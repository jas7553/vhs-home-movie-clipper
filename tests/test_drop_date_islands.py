"""
drop_date_islands() — remove single isolated misreads (a date differing from both
nearest dated neighbours) before boundary detection. Replaces _collapse_revert_phantoms.

Key property: a real session is a contiguous run of >= 2 same-date readings and is
never dropped — even a short out-of-order date run on a re-recorded tape. Only a
date appearing for exactly one isolated reading (an OCR misread) is removed.

None entries (failed OCR windows) are skipped when searching for neighbours, so
a misread surrounded only by None gaps is still detectable as an island.
"""
from datetime import datetime

from split_homevideo import drop_date_islands


def mk(*days):
    """Build (t, datetime) readings from day-of-month ints (or (month, day) tuples).

    t is i*10 so order is preserved; only the date matters to the function.
    Use None in the sequence to insert a failed OCR window.
    """
    out = []
    for i, dd in enumerate(days):
        if dd is None:
            out.append((float(i * 10), None))
        else:
            month, day = dd if isinstance(dd, tuple) else (1, dd)
            out.append((float(i * 10), datetime(1990, month, day, 12, 0)))
    return out


def days(samples):
    """Extract the day-of-month sequence, skipping None entries."""
    return [
        (dt.month, dt.day) if dt.month != 1 else dt.day
        for _, dt in samples
        if dt is not None
    ]


class TestPassthrough:
    def test_empty(self):
        assert drop_date_islands([]) == []

    def test_too_short_to_have_interior(self):
        s = mk(1, 2)
        assert drop_date_islands(s) == s

    def test_all_same_date_unchanged(self):
        s = mk(1, 1, 1, 1)
        assert drop_date_islands(s) == s

    def test_all_none_unchanged(self):
        s = mk(None, None, None)
        assert drop_date_islands(s) == s

    def test_fewer_than_three_dated_unchanged(self):
        # Two dated readings with Nones in between — not enough context to drop either
        s = mk(None, 1, None, 2, None)
        assert drop_date_islands(s) == s


class TestDropsIslands:
    def test_single_island_dropped(self):
        # 1,1,[2],1,1 — the lone 2 differs from both neighbours → misread → drop
        assert days(drop_date_islands(mk(1, 1, 2, 1, 1))) == [1, 1, 1, 1]

    def test_misread_on_real_boundary_preserves_boundary(self):
        # 1,1,[9],3,3 — misread 9 sits exactly on the real 1->3 change.
        # Dropping it must keep a single 1->3 boundary (NOT merge 1 and 3).
        assert days(drop_date_islands(mk(1, 1, 9, 3, 3))) == [1, 1, 3, 3]

    def test_consecutive_distinct_islands_both_dropped(self):
        # 1,[8],[9],1 — interior 8 and 9 are each isolated → both dropped
        assert days(drop_date_islands(mk(1, 8, 9, 1))) == [1, 1]

    def test_year_misread_dropped(self):
        # 1990 run with one stray 1999 reading → island → dropped
        s = mk(1, 1, (1, 1), 1, 1)  # all jan-1 except we inject a year below
        s[2] = (20.0, datetime(1999, 5, 19, 12, 0))
        assert days(drop_date_islands(s)) == [1, 1, 1, 1]

    def test_island_with_none_neighbors_dropped(self):
        # Sparse OCR: dated A runs, then many None gaps, then misread X, then None
        # gaps, then dated B runs. X is an island when skipping the Nones.
        # Before this fix, filter_ocr_outliers stripped the Nones and X was isolated
        # with no neighbours — never detected.
        s = mk(1, 1, None, None, None, 9, None, None, None, 2, 2)
        assert days(drop_date_islands(s)) == [1, 1, 2, 2]

    def test_island_between_none_gaps_and_boundary(self):
        # 1->3 real boundary; misread 9 sits in None-flanked gap before the 3-run.
        s = mk(1, 1, None, 9, None, 3, 3)
        assert days(drop_date_islands(s)) == [1, 1, 3, 3]

    def test_nones_within_run_do_not_break_run(self):
        # A run of same-date readings with Nones interspersed is still a run — not islands.
        s = mk(1, None, 1, None, 9, None, 1, None, 1)
        # 9 is isolated between 1 and 1 → dropped; the 1s with Nones form the run
        assert days(drop_date_islands(s)) == [1, 1, 1, 1]


class TestKeepsRealSessions:
    def test_short_out_of_order_run_kept(self):
        # 1,1,[9,9],1,1 — the 9s form a run of 2 = a real short session, kept.
        assert days(drop_date_islands(mk(1, 1, 9, 9, 1, 1))) == [1, 1, 9, 9, 1, 1]

    def test_recurring_date_not_regrouped(self):
        # User's spec: A B C D E B F G (each a 2-run) — both B occurrences survive
        # as separate runs; nothing is dropped or merged.
        seq = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 2, 2, 6, 6, 7, 7]
        assert days(drop_date_islands(mk(*seq))) == seq

    def test_first_and_last_never_dropped(self):
        # Edge readings have only one neighbour; keep them by definition.
        s = mk(9, 1, 1, 1, 8)
        out = days(drop_date_islands(s))
        assert out[0] == 9 and out[-1] == 8

    def test_short_run_with_none_gaps_not_dropped(self):
        # Two same-date readings with None gaps around them — real session, not an island.
        s = mk(1, 1, None, 9, 9, None, 2, 2)
        assert days(drop_date_islands(s)) == [1, 1, 9, 9, 2, 2]
