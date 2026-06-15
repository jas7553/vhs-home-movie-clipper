"""
refine_split(): dense 1s scan to locate exact session boundary.
"""
import tempfile
import unittest.mock as mock
from datetime import datetime

from split_homevideo import refine_split

_CROP = "250:110:385:370"
_GAP = 300
_PREV_DT = datetime(1990, 1, 4, 17, 0)


def _run(coarse_t, prev_t, extract_side_effect, ocr_map):
    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch("split_homevideo.extract_frame", side_effect=extract_side_effect), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            return refine_split("vid.mp4", coarse_t, prev_t, _PREV_DT, _GAP, _CROP, tmpdir)


class TestRefineSplit:
    def test_empty_window_returns_coarse(self):
        # prev_t+1 >= coarse_t → window is empty
        with tempfile.TemporaryDirectory() as tmpdir:
            result = refine_split("vid.mp4", 10.0, 10.0, _PREV_DT, _GAP, _CROP, tmpdir)
        assert result == 10.0

    def test_adjacent_frames_empty_window(self):
        # window = range(int(9)+1, int(10)) = range(10, 10) = []
        with tempfile.TemporaryDirectory() as tmpdir:
            result = refine_split("vid.mp4", 10.0, 9.5, _PREV_DT, _GAP, _CROP, tmpdir)
        assert result == 10.0

    def test_returns_coarse_when_no_boundary_found(self):
        # All frames confirm old session (small cam advance) → return coarse_t
        path = "/tmp/frame_15.000.bmp"
        # cam_advance = (17:00:10 - 17:00:00).total_seconds() = 10; video_advance = 15-10=5; 10 < 5+300
        result = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "5:00 PM\n 1/ 4/90"},
        )
        assert result == 20.0

    def test_detects_forward_jump(self):
        # Frame at t=15 jumps; last_old_t still == prev_t (no confirmed old frames before it)
        # → return prev_t + 1 = 11.0
        path = "/tmp/frame_15.000.bmp"
        # cam_advance = 600s, video_advance = 5s; 600 > 5+300 → jump
        result = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "5:10 PM\n 1/ 4/90"},
        )
        assert result == 11.0  # last_old_t(10) + 1

    def test_old_session_frame_advances_last_old_t(self):
        # t=14 confirms old session; t=15 jumps → return last_old_t(14) + 1 = 15
        path14 = "/tmp/frame_14.000.bmp"
        path15 = "/tmp/frame_15.000.bmp"

        def extract(v, t, c, d):
            if t == 14:
                return path14
            if t == 15:
                return path15
            return None

        result = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=extract,
            # t=14: cam_advance=240s, video_advance=4s → 240 < 304, old session confirmed
            # t=15: cam_advance=600s, video_advance=5s → jump
            ocr_map={path14: "5:04 PM\n 1/ 4/90", path15: "5:10 PM\n 1/ 4/90"},
        )
        assert result == 15.0

    def test_detects_backward_jump(self):
        # cam_advance < -1800 → backward jump triggers split; last_old_t still prev_t
        path = "/tmp/frame_15.000.bmp"
        result = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "10:00 AM\n 1/ 4/90"},
        )
        assert result == 11.0  # last_old_t(10) + 1

    def test_skips_none_frames(self):
        # extract_frame returns None → frame skipped, no boundary found → coarse_t
        result = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
        )
        assert result == 20.0

    def test_skips_frames_with_unparseable_ocr(self):
        path = "/tmp/frame_15.000.bmp"
        result = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "garbage"},
        )
        assert result == 20.0
