"""
find_splits() detects where one recording session ends and another begins.

Two triggers:
  1. Forward jump: camera time advanced more than (video_advance + gap_s).
     This means the camera was paused or powered off between recordings.
  2. Backward jump: camera time went backward by > 30 min (1800s).
     This means a new tape was loaded, or a tape was rewound.

Small backward drifts (< 30 min) are ignored as OCR noise.

The first split is always (0.0, None, None) — the start of the video.
Each subsequent split tuple is (split_t, prev_t, prev_dt), carrying enough
context for refine_split() to do a dense 1-second scan of the boundary window.
"""
from datetime import datetime

from split_homevideo import find_splits


def dt(h: int, m: int) -> datetime:
    return datetime(1990, 6, 15, h, m)


class TestSingleClip:
    def test_continuous_recording_no_splits(self):
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(10, 4)),
        ]
        assert find_splits(samples, gap_s=300) == [(0.0, None, None)]

    def test_empty_samples(self):
        assert find_splits([], gap_s=300) == [(0.0, None, None)]

    def test_all_none_samples(self):
        samples = [(0.0, None), (10.0, None), (20.0, None)]
        assert find_splits(samples, gap_s=300) == [(0.0, None, None)]

    def test_first_split_is_always_origin(self):
        result = find_splits([], gap_s=300)
        assert result[0] == (0.0, None, None)


class TestForwardJump:
    def test_camera_pause_detected(self):
        # 60s video, but camera jumped ~598 min → camera was off between sessions
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(20, 0)),   # cam_adv=35880s, vid_adv=60, 35880 > 60+300 → split
            (180.0, dt(20, 2)),
        ]
        split_times = [t for t, _, _ in find_splits(samples, gap_s=300)]
        assert 120.0 in split_times

    def test_gap_threshold_is_additive_with_video_advance(self):
        # cam_adv=480s, vid_adv=300s, gap_s=300: 480 > 300+300=600? No → no split
        samples = [
            (0.0, dt(10, 0)),
            (300.0, dt(10, 8)),
        ]
        assert find_splits(samples, gap_s=300) == [(0.0, None, None)]

    def test_gap_threshold_just_exceeded(self):
        # cam_adv=780s, vid_adv=60, gap_s=300: 780 > 360 → split
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(10, 15)),  # 13-min camera jump in 60s video
        ]
        split_times = [t for t, _, _ in find_splits(samples, gap_s=300)]
        assert 120.0 in split_times


class TestBackwardJump:
    def test_tape_change_detected(self):
        # Camera time went backward 62 min → new tape loaded
        samples = [
            (0.0, dt(14, 0)),
            (60.0, dt(14, 2)),
            (120.0, dt(13, 0)),   # cam_adv = -62min = -3720s < -1800 → split
            (180.0, dt(13, 2)),
        ]
        split_times = [t for t, _, _ in find_splits(samples, gap_s=300)]
        assert 120.0 in split_times

    def test_small_backward_drift_ignored(self):
        # 1-minute backward: OCR misread. Backward check (-60s > -1800s) passes.
        # Recovery step is cam_adv=120s in 60s video → 120 < gap_threshold(360) → no split.
        # (A 12-minute backward drift would work here too, except its recovery of 840s
        # exceeds the 360s forward-jump threshold and fires a spurious split.)
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(10, 1)),   # -1 min: cam_adv=-60s, not < -1800 → no backward split
            (180.0, dt(10, 3)),   # recovery: cam_adv=120s, vid_adv=60, 120 < 360 → no forward split
        ]
        assert find_splits(samples, gap_s=300) == [(0.0, None, None)]

    def test_exactly_30min_backward_not_split(self):
        # Boundary: exactly -1800s should NOT trigger (condition is < -1800)
        samples = [
            (0.0, dt(10, 30)),
            (60.0, dt(10, 32)),
            (120.0, dt(10, 2)),   # cam_adv = -30min = -1800s, NOT < -1800 → no split
        ]
        assert find_splits(samples, gap_s=300) == [(0.0, None, None)]


class TestMultipleSplits:
    def test_two_pauses_detected(self):
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(20, 0)),   # pause 1
            (180.0, dt(20, 2)),
            (240.0, dt(20, 4)),
            (300.0, dt(23, 0)),   # pause 2
            (360.0, dt(23, 2)),
        ]
        result = find_splits(samples, gap_s=300)
        split_times = [t for t, _, _ in result]
        assert 0.0 in split_times
        assert 120.0 in split_times
        assert 300.0 in split_times
        assert len(result) == 3


class TestSplitMetadata:
    def test_split_tuple_carries_prev_context(self):
        # refine_split() needs prev_t and prev_dt to build its scan window
        samples = [
            (0.0, dt(10, 0)),
            (60.0, dt(10, 2)),
            (120.0, dt(20, 0)),
            (180.0, dt(20, 2)),
        ]
        result = find_splits(samples, gap_s=300)
        assert len(result) == 2
        split_t, prev_t, prev_dt = result[1]
        assert split_t == 120.0
        assert prev_t == 60.0
        assert prev_dt == dt(10, 2)
