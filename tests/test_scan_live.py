"""
scan() non-cache path: majority-vote grouping of frames per interval window.
"""
import unittest.mock as mock
from datetime import datetime

import pytest

from split_homevideo import FRAMES_PER_SAMPLE, scan

_CROP = "250:110:385:370"
_INTERVAL = 10

# Fake paths that frame_index() can parse: frame_NNNNNN.bmp
_P0 = "/tmp/frame_000000.bmp"
_P1 = "/tmp/frame_000001.bmp"
_P2 = "/tmp/frame_000002.bmp"
_P3 = "/tmp/frame_000003.bmp"

_DT = datetime(1990, 1, 4, 17, 1)
_TS = "5:01 PM\n 1/ 4/90"


class TestScanLive:
    def _run(self, paths, ocr):
        with mock.patch("split_homevideo.extract_all_frames", return_value=paths), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr):
            return scan("fake.mp4", _INTERVAL, _CROP, cache_path=None)

    def test_all_ocr_fail_returns_none(self):
        result = self._run([_P0], {})
        assert result == [(0.0, None)]

    def test_majority_vote_picks_most_common(self):
        # 3 frames in window 0: 2 agree on _DT, 1 fails → majority = _DT
        # t returned is t_last_frame of bucket: index 2 → 2*interval/FPS
        result = self._run(
            [_P0, _P1, _P2],
            {_P0: _TS, _P1: _TS, _P2: "garbage"},
        )
        assert len(result) == 1
        t, dt = result[0]
        assert t == pytest.approx(2 * _INTERVAL / FRAMES_PER_SAMPLE)
        assert dt == _DT

    def test_single_valid_reading_accepted(self):
        result = self._run([_P0, _P1, _P2], {_P0: _TS})
        assert result[0][1] == _DT

    def test_second_interval_window(self):
        # FRAMES_PER_SAMPLE=3: indices 0-2 → bucket 0, indices 3-5 → bucket 1
        # t returned is t_last_frame: bucket0 last=idx2 → 6.67s, bucket1 last=idx3 → 10.0s
        assert FRAMES_PER_SAMPLE == 3
        dt2 = datetime(1990, 1, 4, 17, 2)
        ts2 = "5:02 PM\n 1/ 4/90"
        result = self._run(
            [_P0, _P1, _P2, _P3],
            {_P0: _TS, _P3: ts2},
        )
        t_bucket0_last = 2 * _INTERVAL / FRAMES_PER_SAMPLE  # idx 2 → 6.67s
        t_bucket1_last = 3 * _INTERVAL / FRAMES_PER_SAMPLE  # idx 3 → 10.0s
        times = {round(t, 9): dt for t, dt in result}
        assert times[round(t_bucket0_last, 9)] == _DT
        assert times[round(t_bucket1_last, 9)] == dt2

    def test_result_sorted_by_time(self):
        result = self._run([_P0, _P3], {_P0: _TS, _P3: "5:02 PM\n 1/ 4/90"})
        assert result == sorted(result, key=lambda x: x[0])
