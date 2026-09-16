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

from split_homevideo import (
    drop_bounce_runs,
    drop_date_islands,
    drop_day_confusion_runs,
    drop_digit_drop_runs,
    drop_month_confusion_runs,
    drop_out_of_order_twin_runs,
    drop_short_bracketed_runs,
    drop_year_misread_runs,
)


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


# ---------------------------------------------------------------------------
# drop_month_confusion_runs — catches multi-window month-digit confusion
# misreads where OCR swaps visually similar month digits (1↔5, 8↔9) for ≥2
# consecutive windows, forming a bounce run that survives drop_date_islands.
# ---------------------------------------------------------------------------

def mk_mo(*specs):
    """Build (t, datetime) samples for month-confusion tests.

    Each spec is (month, day, year) or None.
    """
    out = []
    for i, s in enumerate(specs):
        if s is None:
            out.append((float(i * 10), None))
        else:
            m, d, y = s
            out.append((float(i * 10), datetime(y, m, d, 12, 0)))
    return out


def mdy(samples):
    """Extract (month, day, year) from samples, skipping None."""
    return [(dt.month, dt.day, dt.year) for _, dt in samples if dt is not None]


class TestDropMonthConfusionRuns:
    def test_jan_vs_may_confusion_dropped(self):
        # 1990-01-06 ×3, 1990-05-06 ×2, 1990-01-06 ×3 — Converse 1990 clip05 pattern
        s = mk_mo((1,6,1990),(1,6,1990),(1,6,1990),(5,6,1990),(5,6,1990),(1,6,1990),(1,6,1990),(1,6,1990))
        result = mdy(drop_month_confusion_runs(s))
        assert result == [(1,6,1990)] * 6

    def test_sep_vs_aug_confusion_dropped(self):
        # 1992-09-25 ×3, 1992-08-25 ×2, 1992-09-25 ×3 — Converse 1992 clip18 pattern
        s = mk_mo((9,25,1992),(9,25,1992),(9,25,1992),(8,25,1992),(8,25,1992),(9,25,1992),(9,25,1992),(9,25,1992))
        result = mdy(drop_month_confusion_runs(s))
        assert result == [(9,25,1992)] * 6

    def test_month_not_in_confusable_pair_kept(self):
        # 1990-03-06 ×2, 1990-06-06 ×2, 1990-03-06 ×2 — 3 and 6 not confusable
        s = mk_mo((3,6,1990),(3,6,1990),(6,6,1990),(6,6,1990),(3,6,1990),(3,6,1990))
        result = mdy(drop_month_confusion_runs(s))
        assert result == [(3,6,1990),(3,6,1990),(6,6,1990),(6,6,1990),(3,6,1990),(3,6,1990)]

    def test_different_day_genuine_outoforder_kept(self):
        # 1990-09-01 between 1990-03-25 and 1990-04-08 — different day, keep
        s = mk_mo((3,25,1990),(3,25,1990),(9,1,1990),(9,1,1990),(4,8,1990),(4,8,1990))
        result = mdy(drop_month_confusion_runs(s))
        assert result == [(3,25,1990),(3,25,1990),(9,1,1990),(9,1,1990),(4,8,1990),(4,8,1990)]

    def test_none_gaps_around_confusion_run_still_caught(self):
        # None gaps don't protect the confusion run
        s = mk_mo((1,6,1990),(1,6,1990),None,(5,6,1990),(5,6,1990),None,(1,6,1990),(1,6,1990))
        result = mdy(drop_month_confusion_runs(s))
        assert result == [(1,6,1990)] * 4

    def test_outer_sides_differ_not_dropped(self):
        # 1990-01-06 → 1990-05-06 → 1990-09-06: outer sides differ, keep all
        s = mk_mo((1,6,1990),(1,6,1990),(5,6,1990),(5,6,1990),(9,6,1990),(9,6,1990))
        result = mdy(drop_month_confusion_runs(s))
        assert result == [(1,6,1990),(1,6,1990),(5,6,1990),(5,6,1990),(9,6,1990),(9,6,1990)]

    def test_same_outer_different_day_in_confusable_pair_kept(self):
        # Outer dates same (1990-01-06), middle run same year+confusable month BUT different day
        # → day mismatch means genuine session, must not drop
        s = mk_mo((1,6,1990),(1,6,1990),(5,7,1990),(5,7,1990),(1,6,1990),(1,6,1990))
        result = mdy(drop_month_confusion_runs(s))
        assert result == [(1,6,1990),(1,6,1990),(5,7,1990),(5,7,1990),(1,6,1990),(1,6,1990)]

    def test_passthrough_too_short(self):
        s = mk_mo((1,6,1990),(5,6,1990))
        assert drop_month_confusion_runs(s) == s

    def test_passthrough_all_same(self):
        s = mk_mo((1,6,1990),(1,6,1990),(1,6,1990),(1,6,1990))
        assert drop_month_confusion_runs(s) == s


# ---------------------------------------------------------------------------
# drop_day_confusion_runs — catches multi-window day-digit confusion misreads
# where OCR swaps visually similar day digits (6↔8) for ≥2 consecutive
# windows, forming a bounce run that survives drop_date_islands (Converse
# 1990 clip44, 5/26/90 misread as 5/28/90).
# ---------------------------------------------------------------------------

class TestDropDayConfusionRuns:
    def test_day26_vs_day28_confusion_dropped(self):
        # 1990-05-26 ×2, 1990-05-28 ×2, 1990-05-26 ×2 — Converse 1990 clip44 pattern
        s = mk_mo((5,26,1990),(5,26,1990),(5,28,1990),(5,28,1990),(5,26,1990),(5,26,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,26,1990)] * 4

    def test_interval1_multiwindow_misread_dropped(self):
        # At interval 1 a ~3s physical misread spans 4 readings and
        # sailed past the OLD reading-count cap (_CONFUSION_RUN_MAX=3). The
        # seconds-denominated cap (span 3s <= 10s) drops it. t spaced 1s.
        seq = ([(5,26,1990)]*3 + [(5,28,1990)]*4 + [(5,26,1990)]*3)
        s = [(float(i), datetime(y, m, d, 12, 0)) for i, (m, d, y) in enumerate(seq)]
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,26,1990)] * 6

    def test_interval1_long_real_run_kept(self):
        # Same 1s spacing, but a genuine 15-reading (14s > 10s) session is kept.
        seq = ([(5,26,1990)]*3 + [(5,28,1990)]*15 + [(5,26,1990)]*3)
        s = [(float(i), datetime(y, m, d, 12, 0)) for i, (m, d, y) in enumerate(seq)]
        result = mdy(drop_day_confusion_runs(s))
        assert result == seq

    def test_single_digit_day_6_vs_8_confusion_dropped(self):
        # 1990-05-06 ×2, 1990-05-08 ×2, 1990-05-06 ×2 — same confusable pair, single digit
        s = mk_mo((5,6,1990),(5,6,1990),(5,8,1990),(5,8,1990),(5,6,1990),(5,6,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,6,1990)] * 4

    def test_day_not_in_confusable_pair_kept(self):
        # 1990-05-26 ×2, 1990-05-27 ×2, 1990-05-26 ×2 — 6 and 7 not confusable
        s = mk_mo((5,26,1990),(5,26,1990),(5,27,1990),(5,27,1990),(5,26,1990),(5,26,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,26,1990),(5,26,1990),(5,27,1990),(5,27,1990),(5,26,1990),(5,26,1990)]

    def test_month_differs_kept(self):
        # Outer dates same (1990-05-26), middle run same year + confusable-shaped day
        # BUT different month → genuine session, must not drop
        s = mk_mo((5,26,1990),(5,26,1990),(6,28,1990),(6,28,1990),(5,26,1990),(5,26,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,26,1990),(5,26,1990),(6,28,1990),(6,28,1990),(5,26,1990),(5,26,1990)]

    def test_different_day_genuine_outoforder_kept(self):
        # 1990-09-01 between 1990-03-25 and 1990-04-08 — different month, keep
        s = mk_mo((3,25,1990),(3,25,1990),(9,1,1990),(9,1,1990),(4,8,1990),(4,8,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(3,25,1990),(3,25,1990),(9,1,1990),(9,1,1990),(4,8,1990),(4,8,1990)]

    def test_none_gaps_around_confusion_run_still_caught(self):
        # None gaps don't protect the confusion run
        s = mk_mo((5,26,1990),(5,26,1990),None,(5,28,1990),(5,28,1990),None,(5,26,1990),(5,26,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,26,1990)] * 4

    def test_outer_sides_differ_not_dropped(self):
        # 1990-05-26 → 1990-05-28 → 1990-05-30: outer sides differ, keep all
        s = mk_mo((5,26,1990),(5,26,1990),(5,28,1990),(5,28,1990),(5,30,1990),(5,30,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,26,1990),(5,26,1990),(5,28,1990),(5,28,1990),(5,30,1990),(5,30,1990)]

    def test_two_digit_positions_differ_kept(self):
        # day 16 vs day 28: both tens (1 vs 2) and ones (6 vs 8) differ — not a
        # single-digit-position confusion, must not drop even though 6/8 appear
        s = mk_mo((5,16,1990),(5,16,1990),(5,28,1990),(5,28,1990),(5,16,1990),(5,16,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,16,1990),(5,16,1990),(5,28,1990),(5,28,1990),(5,16,1990),(5,16,1990)]

    def test_digit_count_mismatch_not_confusable_pair_kept(self):
        # day 6 (1 digit) vs day 19 (2 digits): 9 vs 6 not a confusable pair and
        # ones-digit != outer, so not a space-insertion match either — keep.
        s = mk_mo((5,6,1990),(5,6,1990),(5,19,1990),(5,19,1990),(5,6,1990),(5,6,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,6,1990),(5,6,1990),(5,19,1990),(5,19,1990),(5,6,1990),(5,6,1990)]

    def test_space_insertion_exact_ones_match_dropped(self):
        # outer day 6 (rendered "1/ 6"), run day 16 — pure space misread as
        # tens-digit '1', ones digit matches outer exactly.
        s = mk_mo((1,6,1990),(1,6,1990),(1,16,1990),(1,16,1990),(1,6,1990),(1,6,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(1,6,1990)] * 4

    def test_space_insertion_plus_confusable_ones_dropped(self):
        # outer day 6, run day 18 — space->'1' tens digit PLUS 6<->8
        # ones-digit confusion (Converse 1990 clip05 phantom).
        s = mk_mo((1,6,1990),(1,6,1990),(1,18,1990),(1,18,1990),(1,6,1990),(1,6,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(1,6,1990)] * 4

    def test_space_insertion_interval1_span_cap_repro(self):
        # 1s spacing: 2-reading 1/18 misread bracketed by 1/6 runs, spans
        # ~1s <= 10s cap -> dropped.
        seq = ([(1,6,1990)]*3 + [(1,18,1990)]*2 + [(1,6,1990)]*3)
        s = [(float(i), datetime(y, m, d, 12, 0)) for i, (m, d, y) in enumerate(seq)]
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(1,6,1990)] * 6

    def test_space_insertion_long_real_session_kept(self):
        # A genuine long 01-18 session bracketed by 01-06 sessions must survive
        # the span cap exactly like real confusion-run neighbours do.
        seq = ([(1,6,1990)]*3 + [(1,18,1990)]*15 + [(1,6,1990)]*3)
        s = [(float(i), datetime(y, m, d, 12, 0)) for i, (m, d, y) in enumerate(seq)]
        result = mdy(drop_day_confusion_runs(s))
        assert result == seq

    def test_space_insertion_outer_day_out_of_range_kept(self):
        # outer day 16 is not a single space-rendered digit (1-9) -> no match
        s = mk_mo((5,16,1990),(5,16,1990),(5,26,1990),(5,26,1990),(5,16,1990),(5,16,1990))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(5,16,1990),(5,16,1990),(5,26,1990),(5,26,1990),(5,16,1990),(5,16,1990)]

    def test_single_reading_island_does_not_crash(self):
        # A lone differing reading (drop_date_islands' job, not this filter's) —
        # must pass through without error.
        s = mk_mo((5,26,1990),(5,26,1990),(5,28,1990),(5,26,1990),(5,26,1990))
        result = drop_day_confusion_runs(s)
        assert isinstance(result, list)

    def test_passthrough_too_short(self):
        s = mk_mo((5,26,1990),(5,28,1990))
        assert drop_day_confusion_runs(s) == s

    def test_passthrough_all_same(self):
        s = mk_mo((5,26,1990),(5,26,1990),(5,26,1990),(5,26,1990))
        assert drop_day_confusion_runs(s) == s

    def test_day26_vs_day25_confusion_dropped(self):
        # 1992 tape phantom at ~1301s: 11/26 read as 11/25 for 2 windows
        # (day ones-digit 6 <-> 5), bracketed by 11/26 runs - created a phantom
        # boundary pair boxing mislabeled 11-26 content
        s = mk_mo((11,26,1992),(11,26,1992),(11,25,1992),(11,25,1992),(11,26,1992),(11,26,1992))
        result = mdy(drop_day_confusion_runs(s))
        assert result == [(11,26,1992)] * 4
    def test_long_real_run_between_confusable_neighbours_kept(self):
        # Alternation hazard (1992 tape ~1139-1301s): a REAL long 11-26 session
        # sits between a real 11-25 session and a 2-window 11-25 misread. The
        # interior 11-26 run is bracketed by identical confusable dates but is
        # far too long to be a misread - must be kept (_CONFUSION_RUN_MAX).
        s = mk_mo(*([(11,25,1992)]*3 + [(11,26,1992)]*8 + [(11,25,1992)]*2 + [(11,26,1992)]*4))
        result = mdy(drop_day_confusion_runs(s))
        # the 2-window 11-25 misread drops; the 8-reading real 11-26 run stays
        assert result == [(11,25,1992)]*3 + [(11,26,1992)]*8 + [(11,26,1992)]*4


class TestMonthDigitDropRuns:
    """Numeric overlays drop the leading '1' of a two-digit month for >= 2 windows."""

    def test_12_to_2_run_dropped(self):
        # 12/22 12/22 [2/22 2/22] 12/22 12/22
        s = mk((12, 22), (12, 22), (2, 22), (2, 22), (12, 22), (12, 22))
        assert days(drop_digit_drop_runs(s)) == [(12, 22)] * 4

    def test_11_to_1_run_dropped(self):
        s = mk((11, 3), (11, 3), (1, 3), (1, 3), (11, 3), (11, 3))
        assert days(drop_digit_drop_runs(s)) == [(11, 3)] * 4

    def test_alternation_resolves_to_full_month(self):
        # A B A B A: each 2/22 run is bracketed by 12/22 -> dropped; 12/22 never is.
        s = mk((12, 22), (12, 22), (2, 22), (2, 22), (12, 22), (2, 22), (2, 22), (12, 22))
        assert days(drop_digit_drop_runs(s)) == [(12, 22)] * 4

    def test_added_digit_direction_not_dropped(self):
        # 2/22 2/22 [12/22 12/22] 2/22 2/22 — 12 is not a digit-drop of 2.
        s = mk((2, 22), (2, 22), (12, 22), (12, 22), (2, 22), (2, 22))
        assert days(drop_digit_drop_runs(s)) == days(s)

    def test_different_day_not_dropped(self):
        # 12/22 [2/23 2/23] 12/22 — both fields differ: a genuine other date.
        s = mk((12, 22), (12, 22), (2, 23), (2, 23), (12, 22), (12, 22))
        assert days(drop_digit_drop_runs(s)) == days(s)

    def test_day_rule_unchanged(self):
        s = mk((11, 26), (11, 26), (11, 6), (11, 6), (11, 26), (11, 26))
        assert days(drop_digit_drop_runs(s)) == [(11, 26)] * 4


def _with_hole(s, idx, reads=()):
    """Surround samples[idx] with 1s None probes (+/-4s) as island verification
    would, plus optional legible probes {offset: (month, day)}."""
    t = s[idx][0]
    extra = [(t + k, None) for k in (-4, -3, -2, -1, 1, 2, 3, 4)]
    for off, (m, d) in dict(reads).items():
        extra.append((t + off, datetime(1990, m, d, 12, 0)))
    return sorted(s + extra, key=lambda x: x[0])


class TestDeadZoneIsland:
    """A lone reading inside a probed-but-unreadable span, chronologically between
    two DIFFERENT non-island neighbour dates, is a faint-overlay session."""

    def test_between_differing_neighbours_in_none_hole_kept(self):
        s = _with_hole(mk((9, 25), (9, 25), (9, 27), (10, 4), (10, 4)), 2)
        assert days(drop_date_islands(s)) == [(9, 25), (9, 25), (9, 27), (10, 4), (10, 4)]

    def test_same_neighbours_dropped_even_in_hole(self):
        s = _with_hole(mk((9, 25), (9, 25), (9, 27), (9, 25), (9, 25)), 2)
        assert days(drop_date_islands(s)) == [(9, 25)] * 4

    def test_not_chronological_dropped(self):
        s = _with_hole(mk((9, 25), (9, 25), (9, 28), (9, 27), (9, 27)), 2)
        assert days(drop_date_islands(s)) == [(9, 25), (9, 25), (9, 27), (9, 27)]

    def test_legible_neighbour_within_hole_drops(self):
        s = _with_hole(mk((9, 25), (9, 25), (9, 27), (10, 4), (10, 4)), 2, {-3: (9, 25)})
        assert (9, 27) not in days(drop_date_islands(s))

    def test_no_probe_evidence_drops(self):
        # never probed (no None entries within 5s): the plain island rule applies
        s = mk((1, 5), (1, 5), (1, 19), (1, 25), (1, 25))
        assert days(drop_date_islands(s)) == [5, 5, 25, 25]

    def test_adjacent_island_cannot_vouch(self):
        # 3/06 | 3/03 3/05 | 3/07: 3/05 is "between" its island neighbour 3/03 and
        # 3/07, but not between the real neighbours 3/06 and 3/07 -> both dropped
        s = mk((3, 6), (3, 6), (3, 3), (3, 5), (3, 7), (3, 7))
        s = _with_hole(s, 3)
        assert days(drop_date_islands(s)) == [(3, 6), (3, 6), (3, 7), (3, 7)]


class TestBounceRuns:
    """X Y X Y alternation between single-field twin dates is resolved by which of
    the two lies between the dates on either side of the bounce."""

    def test_persistent_month_misread_dropped_by_chronology(self):
        # 4/27 | 1/28 4/28 1/28 1/28 4/28 | 4/29 -> 4/28 fits [4/27, 4/29], 1/28 does not
        s = mk((4, 27), (4, 27), (1, 28), (1, 28), (4, 28), (4, 28), (1, 28), (1, 28), (1, 28),
               (4, 28), (4, 28), (4, 29), (4, 29))
        out = days(drop_bounce_runs(s))
        assert (1, 28) not in out
        assert out.count((4, 28)) == 4

    def test_bracketed_single_run_resolved(self):
        # 3/06 | 3/07 8/07 3/07 | 3/08 : 8/07 is out of range even though 16s long
        s = mk((3, 6), (3, 6), (3, 7), (3, 7), (8, 7), (8, 7), (8, 7), (3, 7), (3, 7), (3, 8), (3, 8))
        assert (8, 7) not in days(drop_bounce_runs(s))

    def test_both_in_range_undecided(self):
        # real adjacent-day sessions with a misread bounce: 11/25 11/26 11/25 11/26 between 11/24 and 11/27
        s = mk((11, 24), (11, 24), (11, 25), (11, 25), (11, 26), (11, 26), (11, 25), (11, 25),
               (11, 26), (11, 26), (11, 27), (11, 27))
        assert drop_bounce_runs(s) == s

    def test_two_runs_is_a_boundary_not_a_bounce(self):
        s = mk((5, 25), (5, 25), (5, 26), (5, 26), (5, 28), (5, 28), (5, 30), (5, 30))
        assert drop_bounce_runs(s) == s

    def test_non_twin_alternation_untouched(self):
        # 3/25 9/01 3/25 is a genuine re-recording, not a one-glyph misread
        s = mk((3, 24), (3, 24), (3, 25), (3, 25), (9, 1), (9, 1), (3, 25), (3, 25), (4, 8), (4, 8))
        assert drop_bounce_runs(s) == s

    def test_out_of_order_neighbours_undecided(self):
        s = mk((4, 29), (4, 29), (4, 23), (4, 23), (1, 23), (1, 23), (4, 23), (4, 23), (4, 22), (4, 22))
        assert drop_bounce_runs(s) == s

    def test_tape_edge_undecided(self):
        s = mk((4, 23), (4, 23), (1, 23), (1, 23), (4, 23), (4, 23), (4, 26), (4, 26))
        assert drop_bounce_runs(s) == s


class TestOutOfOrderTwinRuns:
    def test_twin_run_outside_neighbour_range_dropped(self):
        # 3/06 | 3/03 3/03 | 3/07 : 3/03 is a day-twin of 3/06 and not in [3/06, 3/07]
        s = mk((3, 6), (3, 6), (3, 3), (3, 3), (3, 7), (3, 7))
        assert days(drop_out_of_order_twin_runs(s)) == [(3, 6), (3, 6), (3, 7), (3, 7)]

    def test_accomplice_misreads_judged_against_anchors(self):
        # 3/06 (long) | 3/03 3/05 | 3/07 (long): both short runs are outside [3/06, 3/07]
        long6 = [(float(t), datetime(1990, 3, 6, 12, 0)) for t in range(0, 80, 10)]
        long7 = [(float(t), datetime(1990, 3, 7, 12, 0)) for t in range(200, 280, 10)]
        s = long6 + [(100.0, datetime(1990, 3, 3, 12, 0)), (110.0, datetime(1990, 3, 3, 12, 0)),
                     (120.0, datetime(1990, 3, 5, 12, 0)), (130.0, datetime(1990, 3, 5, 12, 0))] + long7
        assert set(days(drop_out_of_order_twin_runs(s))) == {(3, 6), (3, 7)}

    def test_in_range_run_kept(self):
        s = mk((3, 6), (3, 6), (3, 7), (3, 7), (3, 8), (3, 8))
        assert drop_out_of_order_twin_runs(s) == s

    def test_non_twin_out_of_order_kept(self):
        # genuine 9/01 re-recording between 3/25 and 4/08
        s = mk((3, 25), (3, 25), (9, 1), (9, 1), (4, 8), (4, 8))
        assert drop_out_of_order_twin_runs(s) == s

    def test_long_run_kept(self):
        # a 100s run is a real session however odd its date
        s = mk((3, 6), (3, 6)) + [(float(t), datetime(1990, 3, 3, 12, 0)) for t in range(20, 130, 10)] \
            + [(140.0, datetime(1990, 3, 7, 12, 0)), (150.0, datetime(1990, 3, 7, 12, 0))]
        assert drop_out_of_order_twin_runs(s) == s



class TestShortBracketedRuns:
    def test_short_third_date_inside_one_session_dropped(self):
        s = mk((8, 8), (8, 8), (3, 18), (3, 18), (8, 8), (8, 8))  # 10s span
        assert (3, 18) not in days(drop_short_bracketed_runs(s))

    def test_long_run_kept(self):
        s = mk((8, 8), (8, 8), (3, 18), (3, 18), (3, 18), (8, 8), (8, 8))  # 20s span
        assert drop_short_bracketed_runs(s) == s

    def test_different_brackets_kept(self):
        s = mk((8, 8), (8, 8), (3, 18), (3, 18), (8, 9), (8, 9))
        assert drop_short_bracketed_runs(s) == s
