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


def _run(coarse_t, prev_t, extract_side_effect, ocr_map, visual_times=None):
    with tempfile.TemporaryDirectory() as tmpdir:
        with mock.patch("split_homevideo.extract_frame", side_effect=extract_side_effect), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            return refine_split(
                "vid.mp4", coarse_t, prev_t, _PREV_DT, _GAP, _CROP, tmpdir, visual_times
            )


class TestRefineSplit:
    def test_empty_window_returns_coarse(self):
        # prev_t+1 >= coarse_t → window is empty
        with tempfile.TemporaryDirectory() as tmpdir:
            t, method = refine_split("vid.mp4", 10.0, 10.0, _PREV_DT, _GAP, _CROP, tmpdir)
        assert t == 10.0
        assert method == "coarse"

    def test_adjacent_frames_empty_window(self):
        # window = range(int(9)+1, int(10)) = range(10, 10) = []
        with tempfile.TemporaryDirectory() as tmpdir:
            t, method = refine_split("vid.mp4", 10.0, 9.5, _PREV_DT, _GAP, _CROP, tmpdir)
        assert t == 10.0
        assert method == "coarse"

    def test_returns_coarse_when_no_boundary_found(self):
        # All frames confirm old session (small cam advance) → return coarse_t
        path = "/tmp/frame_15.000.bmp"
        # cam_advance = (17:00:10 - 17:00:00).total_seconds() = 10; video_advance = 15-10=5; 10 < 5+300
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "5:00 PM\n 1/ 4/90"},
        )
        assert t == 20.0
        assert method == "coarse"

    def test_detects_forward_jump(self):
        # Frame at t=15 jumps; last_old_t still == prev_t (no confirmed old frames before it)
        # → return prev_t + 1 = 11.0
        path = "/tmp/frame_15.000.bmp"
        # cam_advance = 600s, video_advance = 5s; 600 > 5+300 → jump
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "5:10 PM\n 1/ 4/90"},
        )
        assert t == 11.0  # last_old_t(10) + 1
        assert method == "ocr"

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

        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=extract,
            # t=14: cam_advance=240s, video_advance=4s → 240 < 304, old session confirmed
            # t=15: cam_advance=600s, video_advance=5s → jump
            ocr_map={path14: "5:04 PM\n 1/ 4/90", path15: "5:10 PM\n 1/ 4/90"},
        )
        assert t == 15.0
        assert method == "ocr"

    def test_detects_backward_jump(self):
        # cam_advance < -1800 → backward jump triggers split; last_old_t still prev_t
        path = "/tmp/frame_15.000.bmp"
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "10:00 AM\n 1/ 4/90"},
        )
        assert t == 11.0  # last_old_t(10) + 1
        assert method == "ocr"

    def test_skips_none_frames(self):
        # extract_frame returns None → frame skipped, no boundary found → coarse_t
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
        )
        assert t == 20.0
        assert method == "coarse"

    def test_skips_frames_with_unparseable_ocr(self):
        path = "/tmp/frame_15.000.bmp"
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "garbage"},
        )
        assert t == 20.0
        assert method == "coarse"

    def test_garbled_new_session_after_confirmed_old(self):
        # Old session confirmed at t=14; frames after t=14 extract but OCR gives garbled
        # new-session text (missing day field, like '11:43 AM 5/90'). parse_timestamp
        # rejects them → no cam_advance check fires. But because frames were extracted
        # after last_old_t, we know the transition is there: cut at last_old_t+1=15.
        path14 = "/tmp/frame_14.000.bmp"
        path16 = "/tmp/frame_16.000.bmp"

        def extract(v, t, c, d):
            if t == 14:
                return path14
            if t == 16:
                return path16
            return None

        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=extract,
            ocr_map={
                path14: "5:04 PM\n 1/ 4/90",  # old session, cam_advance=240s < 5+300
                path16: "11:43 AM 5/90",       # garbled new-session (missing day)
            },
        )
        assert t == 15.0   # last_old_t(14) + 1
        assert method == "ocr"

    # --- visual anchor fallback (OCR dead zone) ---

    def test_visual_anchor_used_when_ocr_dead_zone(self):
        # All frames return None (dead zone). Visual signal at 15s → anchor there.
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
            visual_times=[15.0],
        )
        assert t == 15.0
        assert method == "visual"

    def test_visual_anchor_picks_last_in_window(self):
        # Two visual signals in window: pick the last (end of noise burst).
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
            visual_times=[17.0, 13.0, 25.0],
        )
        assert t == 17.0
        assert method == "visual"

    def test_visual_anchor_outside_window_ignored(self):
        # Visual signal at 25s is beyond coarse_t=20s → falls back to coarse.
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
            visual_times=[25.0],
        )
        assert t == 20.0
        assert method == "coarse"

    def test_ocr_result_takes_priority_over_visual(self):
        # OCR finds a jump at t=15; visual at 13s should be ignored.
        path = "/tmp/frame_15.000.bmp"
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "5:10 PM\n 1/ 4/90"},
            visual_times=[13.0],
        )
        assert t == 11.0
        assert method == "ocr"

    def test_visual_times_none_falls_back_to_coarse(self):
        # visual_times=None (visual detection off) → coarse as before.
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
            visual_times=None,
        )
        assert t == 20.0
        assert method == "coarse"
