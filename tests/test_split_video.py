"""
_label_for() and split_video().
"""
import os
import unittest.mock as mock
from datetime import datetime

from split_homevideo import _label_for, split_video

_DT = datetime(1990, 1, 4, 17, 1)


class TestLabelFor:
    def test_returns_formatted_datetime(self):
        filtered = [(5.0, _DT)]
        assert _label_for(filtered, 5.0) == "1990-01-04_1701"

    def test_returns_first_reading_at_or_after_start(self):
        dt2 = datetime(1990, 2, 14, 9, 0)
        filtered = [(5.0, _DT), (100.0, dt2)]
        assert _label_for(filtered, 80.0) == "1990-02-14_0900"

    def test_fallback_when_no_reading_after_start(self):
        filtered = [(5.0, _DT)]
        assert _label_for(filtered, 200.0) == "00200s"

    def test_empty_filtered_returns_fallback(self):
        assert _label_for([], 42.0) == "00042s"


class TestSplitVideo:
    def test_creates_output_dir(self, tmp_path):
        out_dir = str(tmp_path / "clips")
        filtered = [(0.0, _DT)]
        with mock.patch("split_homevideo.get_duration", return_value=100.0), \
             mock.patch("split_homevideo.cut_clip_with_boundary_encode"):
            split_video("myvid.mp4", [0.0], out_dir, filtered)
        assert os.path.isdir(out_dir)

    def test_removes_stale_clips(self, tmp_path):
        out_dir = str(tmp_path)
        stale = tmp_path / "myvid_clip01_1990-01-01_0000.mp4"
        stale.touch()
        filtered = [(0.0, _DT)]
        with mock.patch("split_homevideo.get_duration", return_value=100.0), \
             mock.patch("split_homevideo.cut_clip_with_boundary_encode"):
            split_video("myvid.mp4", [0.0], out_dir, filtered)
        assert not stale.exists()

    def test_produces_correct_output_filenames(self, tmp_path):
        out_dir = str(tmp_path)
        filtered = [(0.0, _DT), (50.0, datetime(1990, 6, 1, 10, 0))]
        calls = []
        with mock.patch("split_homevideo.get_duration", return_value=100.0), \
             mock.patch("split_homevideo.cut_clip_with_boundary_encode",
                        side_effect=lambda *a, **kw: calls.append(a)):
            split_video("myvid.mp4", [0.0, 50.0], out_dir, filtered)
        assert len(calls) == 2
        assert "myvid_clip01_1990-01-04_1701.mp4" in calls[0][5]
        assert "myvid_clip02_1990-06-01_1000.mp4" in calls[1][5]

    def test_first_clip_has_no_exact_start(self, tmp_path):
        out_dir = str(tmp_path)
        filtered = [(0.0, _DT)]
        calls = []
        with mock.patch("split_homevideo.get_duration", return_value=100.0), \
             mock.patch("split_homevideo.cut_clip_with_boundary_encode",
                        side_effect=lambda *a, **kw: calls.append(a)):
            split_video("myvid.mp4", [0.0], out_dir, filtered)
        # exact_start (arg 3) should be None for first clip
        assert calls[0][3] is None

    def test_last_clip_has_no_exact_end(self, tmp_path):
        out_dir = str(tmp_path)
        dt2 = datetime(1990, 6, 1, 10, 0)
        filtered = [(0.0, _DT), (50.0, dt2)]
        calls = []
        with mock.patch("split_homevideo.get_duration", return_value=100.0), \
             mock.patch("split_homevideo.cut_clip_with_boundary_encode",
                        side_effect=lambda *a, **kw: calls.append(a)):
            split_video("myvid.mp4", [0.0, 50.0], out_dir, filtered)
        # exact_end (arg 4) should be None for last clip
        assert calls[1][4] is None
