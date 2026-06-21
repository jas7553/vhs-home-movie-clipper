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

from split_homevideo import drop_date_islands, drop_digit_drop_runs, drop_year_misread_runs


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


# ---------------------------------------------------------------------------
# drop_digit_drop_runs — catches multi-window digit-drop misreads that survive
# the single-island filter (e.g. NOV. 26 → NOV 6 persisting 2+ windows).
# ---------------------------------------------------------------------------

def mk92(*specs):
    """Build (t, datetime) samples for 1992-style word-month (Style B) tests.

    Each spec is one of:
      int           → 1992-11-<day>
      (month, day)  → 1992-<month>-<day>
      None          → failed OCR window
    """
    out = []
    for i, s in enumerate(specs):
        if s is None:
            out.append((float(i * 10), None))
        else:
            month, day = s if isinstance(s, tuple) else (11, s)
            out.append((float(i * 10), datetime(1992, month, day, 12, 0)))
    return out


def days92(samples):
    """Extract (month, day) pairs from samples, skipping None."""
    return [(dt.month, dt.day) for _, dt in samples if dt is not None]


class TestDropDigitDropRuns:
    def test_two_window_nov6_inside_nov26_dropped(self):
        # NOV26 NOV26 NOV6 NOV6 NOV26 NOV26 — verified 1992-tape misread pattern
        s = mk92(26, 26, 6, 6, 26, 26)
        result = days92(drop_digit_drop_runs(s))
        assert result == [(11, 26)] * 4

    def test_two_window_nov2_inside_nov27_dropped(self):
        # NOV27 NOV27 NOV2 NOV2 NOV27 NOV27
        s = mk92(27, 27, 2, 2, 27, 27)
        result = days92(drop_digit_drop_runs(s))
        assert result == [(11, 27)] * 4

    def test_single_island_already_handled_by_other_filter(self):
        # Single-window misread — drop_digit_drop_runs should also catch it
        # (the outer runs on both sides match and digit-drop holds).
        s = mk92(26, 26, 6, 26, 26)
        result = days92(drop_digit_drop_runs(s))
        assert result == [(11, 26)] * 4

    def test_none_gaps_around_digit_drop_run_still_caught(self):
        # None gaps between runs don't protect the misread.
        s = mk92(26, 26, None, 6, 6, None, 26, 26)
        result = days92(drop_digit_drop_runs(s))
        assert result == [(11, 26)] * 4

    def test_genuine_outoforder_different_month_kept(self):
        # SEP 1 between MAR 25 and APR 8 — different months, not a digit drop.
        s = mk92((3, 25), (3, 25), (9, 1), (9, 1), (4, 8), (4, 8))
        result = days92(drop_digit_drop_runs(s))
        assert result == [(3, 25), (3, 25), (9, 1), (9, 1), (4, 8), (4, 8)]

    def test_genuine_same_month_different_ones_digit_kept(self):
        # NOV26 → NOV13 → NOV26: 26%10=6 ≠ 13 — not a digit drop, keep.
        s = mk92(26, 26, 13, 13, 26, 26)
        result = days92(drop_digit_drop_runs(s))
        assert result == [(11, 26), (11, 26), (11, 13), (11, 13), (11, 26), (11, 26)]

    def test_outer_context_must_match_both_sides(self):
        # NOV26 → NOV6 → NOV27: outer sides differ, not a digit-drop bracket.
        s = mk92(26, 26, 6, 6, 27, 27)
        result = days92(drop_digit_drop_runs(s))
        assert result == [(11, 26), (11, 26), (11, 6), (11, 6), (11, 27), (11, 27)]

    def test_passthrough_too_short(self):
        s = mk92(26, 6)
        assert drop_digit_drop_runs(s) == s

    def test_passthrough_all_same(self):
        s = mk92(26, 26, 26, 26)
        assert drop_digit_drop_runs(s) == s


# ---------------------------------------------------------------------------
# drop_year_misread_runs — catches in-range year misreads that form ≥2-reading
# runs (e.g. 1990-04-29 → 1999-04-29 ×2 → 1990-04-29), which survive
# drop_date_islands because a run of 2 looks like a genuine short session.
# ---------------------------------------------------------------------------

def mk_yr(year_day_pairs):
    """Build (t, datetime) from [(year, month, day), ...] or None."""
    out = []
    for i, v in enumerate(year_day_pairs):
        if v is None:
            out.append((float(i * 10), None))
        else:
            y, m, d = v
            out.append((float(i * 10), datetime(y, m, d, 12, 0)))
    return out


def ymd(samples):
    """Extract (year, month, day) from samples, skipping None."""
    return [(dt.year, dt.month, dt.day) for _, dt in samples if dt is not None]


class TestDropYearMisreadRuns:
    def test_two_reading_year_misread_dropped(self):
        # 1990-04-29 ×2, 1999-04-29 ×2, 1990-04-29 ×2 — same month, outer year matches
        s = mk_yr([(1990,4,29),(1990,4,29),(1999,4,29),(1999,4,29),(1990,4,29),(1990,4,29)])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,4,29)] * 4

    def test_single_reading_year_misread_dropped(self):
        # Even a single reading is dropped (also caught by drop_date_islands, but verify)
        s = mk_yr([(1990,7,8),(1990,7,8),(1998,7,8),(1990,7,8),(1990,7,8)])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,7,8)] * 4

    def test_day_off_by_one_still_dropped(self):
        # 1991-04-28 between 1990-04-29 runs — year and day differ, same month → drop
        s = mk_yr([(1990,4,29),(1990,4,29),(1991,4,28),(1991,4,28),(1990,4,29),(1990,4,29)])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,4,29)] * 4

    def test_genuine_new_year_boundary_kept(self):
        # 1990-12-31 → 1991-01-01: year changes AND month changes → not dropped
        s = mk_yr([(1990,12,31),(1990,12,31),(1991,1,1),(1991,1,1),(1991,1,2),(1991,1,2)])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,12,31),(1990,12,31),(1991,1,1),(1991,1,1),(1991,1,2),(1991,1,2)]

    def test_outer_years_differ_not_dropped(self):
        # 1990-Apr → 1991-Apr → 1992-Apr: outer years differ (1990 ≠ 1992) → keep all
        s = mk_yr([(1990,4,29),(1990,4,29),(1991,4,29),(1991,4,29),(1992,4,29),(1992,4,29)])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,4,29),(1990,4,29),(1991,4,29),(1991,4,29),(1992,4,29),(1992,4,29)]

    def test_none_gaps_around_misread_run_still_caught(self):
        # None gaps around the year-misread run don't protect it
        s = mk_yr([(1990,4,29),(1990,4,29),None,(1999,4,29),(1999,4,29),None,(1990,4,29),(1990,4,29)])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,4,29)] * 4

    def test_two_adjacent_phantom_runs_collapsed(self):
        # Real 4/29 case: [1990-04-29, 1991-04-29, 1991-04-28, 1990-04-29]
        # Both phantom runs have year≠1990 and month==4 — collapse as one block
        s = mk_yr([
            (1990,4,29),(1990,4,29),
            (1991,4,29),(1991,4,29),
            (1991,4,28),(1991,4,28),
            (1990,4,29),(1990,4,29),
        ])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,4,29)] * 4

    def test_three_adjacent_phantom_runs_collapsed(self):
        # Three inner phantom runs — all same-month, different year → drop whole block
        s = mk_yr([
            (1990,4,29),(1990,4,29),
            (1991,4,29),(1991,4,29),
            (1991,4,28),(1991,4,28),
            (1991,4,27),(1991,4,27),
            (1990,4,29),(1990,4,29),
        ])
        result = ymd(drop_year_misread_runs(s))
        assert result == [(1990,4,29)] * 4

    def test_passthrough_too_short(self):
        s = mk_yr([(1990,4,29),(1999,4,29)])
        assert drop_year_misread_runs(s) == s

    def test_passthrough_all_same_year(self):
        s = mk_yr([(1990,4,29),(1990,4,29),(1990,4,29),(1990,4,29)])
        assert drop_year_misread_runs(s) == s
