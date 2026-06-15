"""
filter_ocr_outliers() removes isolated OCR misreads while preserving real
clip boundaries.

Core invariant: a reading is KEPT if it is consistent (within max_drift_s)
with EITHER its previous OR its next neighbor. This means:
  - Real clip boundary: fails "consistent with prev" (big jump) but passes
    "consistent with next" (subsequent frames confirm the new timestamp) → KEPT
  - Isolated OCR error: fails BOTH checks → REMOVED

Drift is measured as |camera_advance_seconds - video_advance_seconds|.
With this camcorder running ~2x real time, a 60s video interval produces
~120s camera advance → drift ≈ 60s, well within the 900s threshold.
"""
from datetime import datetime

from split_homevideo import filter_ocr_outliers


def dt(h: int, m: int) -> datetime:
    return datetime(1990, 6, 15, h, m)


class TestEdgeCases:
    def test_empty_list(self):
        assert filter_ocr_outliers([]) == []

    def test_all_none_skipped(self):
        assert filter_ocr_outliers([(0.0, None), (10.0, None)]) == []

    def test_single_valid_kept(self):
        assert filter_ocr_outliers([(0.0, dt(10, 0))]) == [(0.0, dt(10, 0))]

    def test_two_valid_both_kept(self):
        # Only two items: both are endpoints, always kept
        result = filter_ocr_outliers([(0.0, dt(10, 0)), (60.0, dt(10, 2))])
        assert result == [(0.0, dt(10, 0)), (60.0, dt(10, 2))]

    def test_none_samples_stripped_before_filtering(self):
        samples = [(0.0, dt(10, 0)), (10.0, None), (20.0, dt(10, 1)), (30.0, dt(10, 2))]
        result = filter_ocr_outliers(samples)
        assert all(dt_ is not None for _, dt_ in result)


class TestNormalSequence:
    def test_clean_sequence_kept_intact(self):
        # Camera advancing ~2x real time: 60s video → ~2 min camera advance
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(10, 4)),
            (180.0, dt(10, 6)),
        ]
        result = filter_ocr_outliers(samples)
        assert result == samples


class TestOutlierRemoval:
    def test_isolated_jump_removed(self):
        # Middle reading jumps 4 hours then falls back → inconsistent with both neighbors
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(14, 0)),   # 4-hour jump: drift with prev = |14280 - 60| >> 900
            (180.0, dt(10, 6)),   # drift with outlier also huge → outlier fails both
            (240.0, dt(10, 8)),
        ]
        result = filter_ocr_outliers(samples)
        assert (120.0, dt(14, 0)) not in result

    def test_neighbors_survive_outlier_removal(self):
        # The readings around the outlier must not be collateral damage
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(14, 0)),
            (180.0, dt(10, 6)),
            (240.0, dt(10, 8)),
        ]
        result = filter_ocr_outliers(samples)
        result_times = [t for t, _ in result]
        assert 60.0 in result_times
        assert 180.0 in result_times


class TestClipBoundaryPreservation:
    def test_confirmed_boundary_kept(self):
        # Large jump AND confirmed by the next frame → real new session, not noise
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(14, 0)),   # jump: fails "consistent with prev"
            (180.0, dt(14, 2)),   # confirms new session: drift(120→180) = |120-60| = 60 < 900
            (240.0, dt(14, 4)),
        ]
        result = filter_ocr_outliers(samples)
        assert (120.0, dt(14, 0)) in result

    def test_boundary_distinguished_from_noise_by_next_frame(self):
        # The only structural difference between an outlier and a real boundary
        # is whether the NEXT frame corroborates the jump.
        noise_samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(14, 0)),   # jump
            (180.0, dt(10, 6)),   # reverts: next does NOT corroborate → noise
            (240.0, dt(10, 8)),
        ]
        boundary_samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(14, 0)),   # jump
            (180.0, dt(14, 2)),   # holds: next DOES corroborate → real boundary
            (240.0, dt(14, 4)),
        ]
        noise_result = filter_ocr_outliers(noise_samples)
        boundary_result = filter_ocr_outliers(boundary_samples)

        assert (120.0, dt(14, 0)) not in noise_result
        assert (120.0, dt(14, 0)) in boundary_result


class TestConsecutiveBoundaries:
    def test_reading_between_two_consecutive_boundaries_kept(self):
        # A: 6:56 PM, B: 11:44 AM next day (fails prev: overnight jump, fails next: 96-min jump),
        # C: 1:20 PM same day as B, D: same session as C.
        # B should be KEPT because prev (A) and next (C) are also mutually inconsistent.
        a_t, a_dt = 0.0,   datetime(1990, 1, 4, 18, 56)
        b_t, b_dt = 60.0,  datetime(1990, 1, 5, 11, 44)
        c_t, c_dt = 120.0, datetime(1990, 1, 5, 13, 20)
        d_t, d_dt = 180.0, datetime(1990, 1, 5, 13, 22)
        samples = [(a_t, a_dt), (b_t, b_dt), (c_t, c_dt), (d_t, d_dt)]
        result = filter_ocr_outliers(samples)
        assert (b_t, b_dt) in result

    def test_isolated_outlier_still_dropped(self):
        # Middle reading jumps 4 hours then reverts — prev and next are mutually consistent
        # so the middle is noise and must be dropped.
        samples = [
            (0.0,   dt(10, 0)),
            (60.0,  dt(10, 2)),
            (120.0, dt(14, 0)),  # outlier: big jump both ways
            (180.0, dt(10, 6)),  # reverts; prev(60) and next(180) are consistent
            (240.0, dt(10, 8)),
        ]
        result = filter_ocr_outliers(samples)
        assert (120.0, dt(14, 0)) not in result
