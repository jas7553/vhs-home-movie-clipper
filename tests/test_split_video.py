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
        assert _label_for(filtered, 5.0, mode="session") == "1990-01-04_1701"

    def test_returns_first_reading_at_or_after_start(self):
        dt2 = datetime(1990, 2, 14, 9, 0)
        filtered = [(5.0, _DT), (100.0, dt2)]
        assert _label_for(filtered, 80.0, mode="session") == "1990-02-14_0900"

    def test_fallback_when_no_reading_after_start(self):
        filtered = [(5.0, _DT)]
        assert _label_for(filtered, 200.0) == "00200s"

    def test_empty_filtered_returns_fallback(self):
        assert _label_for([], 42.0) == "00042s"

    def test_skips_isolated_misread_at_clip_start(self):
        # 1040: hallucinated "1999-05-19" read, immediately reverted by the next
        # sample — same pattern merge_short_clips collapses at the boundary level.
        # The label must skip it and use the next (real) confirmed reading.
        misread = datetime(1999, 5, 19, 10, 14)
        real = datetime(1990, 1, 6, 10, 12)
        filtered = [
            (1020.0, datetime(1990, 1, 6, 10, 11)),
            (1040.0, misread),
            (1090.0, real),
        ]
        assert _label_for(filtered, 1040.0, mode="session") == "1990-01-06_1012"

    def test_daily_mode_skips_isolated_misread(self):
        misread = datetime(1999, 5, 19, 10, 14)
        real = datetime(1990, 1, 6, 10, 12)
        filtered = [
            (1020.0, datetime(1990, 1, 6, 10, 11)),
            (1040.0, misread),
            (1090.0, real),
        ]
        assert _label_for(filtered, 1040.0, mode="daily") == "1990-01-06"

    def test_falls_back_to_unconfirmed_reading_if_nothing_else(self):
        # Only candidate at/after start is unconfirmed (no next sample to check) —
        # must still return something rather than the position-based fallback.
        filtered = [(5.0, _DT)]
        assert _label_for(filtered, 5.0, mode="session") == "1990-01-04_1701"

    def test_first_reading_forward_jump_trusted_by_default(self):
        # No "prev" exists for the first reading, so a forward jump (the common
        # case: camera was paused/off, a real session boundary) is trusted —
        # there's nothing to sandwich-check it against.
        early = datetime(1990, 1, 1, 10, 0)
        later = datetime(1990, 1, 1, 22, 0)  # 12hr forward jump in 10 video seconds
        filtered = [(10.0, early), (20.0, later)]
        assert _label_for(filtered, 10.0, mode="session") == early.strftime("%Y-%m-%d_%H%M")

    def test_last_candidate_has_no_next_to_check_so_is_trusted(self):
        # A backward jump at the very first reading has no prev to confirm it
        # was a real session boundary either — clocks don't normally run
        # backward, so it's treated as suspect even without sandwich context.
        a = datetime(1999, 1, 1, 0, 0)
        b = datetime(1990, 1, 1, 0, 0)
        filtered = [(10.0, a), (20.0, b)]
        assert _label_for(filtered, 10.0, mode="session") == b.strftime("%Y-%m-%d_%H%M")

    def test_misread_with_real_sandwich_is_skipped(self):
        # A misread flanked by a real "prev" reading too, so the sandwich check
        # (jumped-in, prev/next mutually consistent) can fire — the misread is
        # skipped in favor of the next entry.
        prev = datetime(1990, 1, 6, 10, 10)
        misread = datetime(1999, 1, 1, 0, 0)
        nxt = datetime(1990, 1, 6, 10, 11)
        filtered = [(0.0, prev), (10.0, misread), (20.0, nxt)]
        assert _label_for(filtered, 10.0, mode="session") == nxt.strftime("%Y-%m-%d_%H%M")

    def test_stable_consecutive_confirmed_by_next(self):
        # Both readings advance only ~1 min in 60s of video — not a jump.
        # _reading_confirmed returns True via the "not jumped" branch (line 1074).
        a = datetime(1990, 1, 4, 17, 0)
        b = datetime(1990, 1, 4, 17, 1)
        filtered = [(0.0, a), (60.0, b)]
        assert _label_for(filtered, 0.0, mode="session") == a.strftime("%Y-%m-%d_%H%M")

    def test_skips_reading_matching_cam_before_date(self):
        # line 1400: when boundary.cam_before.date() == dt.date() the reading is
        # skipped; the next confirmed reading (different date) becomes the label.
        from split_homevideo import Boundary
        old_dt = datetime(1990, 1, 4, 17, 0)
        new_dt = datetime(1990, 1, 5, 9, 0)
        boundary = Boundary(
            video_t=50.0, type="large_gap",
            cam_before=old_dt, cam_after=new_dt,
            cam_jump_s=57600.0, prev_t=40.0, prev_dt=old_dt,
        )
        filtered = [
            (50.0, old_dt),   # date matches cam_before → skip
            (60.0, new_dt),   # new date, confirmed by next
            (70.0, new_dt),
        ]
        result = _label_for(filtered, 50.0, mode="daily", boundary_map={50.0: boundary})
        assert result == "1990-01-05"


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
            split_video("myvid.mp4", [0.0, 50.0], out_dir, filtered, mode="session")
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
