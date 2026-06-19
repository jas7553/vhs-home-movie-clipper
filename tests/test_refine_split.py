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
        # Frame at t=15 jumps; no confirmed old frames in window (last_old_t=prev_t=10).
        # Cut = max(last_old_t+1, t-1) = max(11, 14) = 14: just before first confirmed new.
        path = "/tmp/frame_15.000.bmp"
        # cam_advance = 600s, video_advance = 5s; 600 > 5+300 → jump
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "5:10 PM\n 1/ 4/90"},
        )
        assert t == 14.0  # max(last_old_t(10)+1, t(15)-1) = max(11, 14) = 14
        assert method == "ocr"

    def test_old_session_frame_advances_last_old_t(self):
        # t=14 confirms old session; t=15 jumps.
        # Cut = max(last_old_t+1, t-1) = max(15, 14) = 15: keeps 14 in old clip.
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
        assert t == 15.0  # max(14+1, 15-1) = max(15, 14) = 15
        assert method == "ocr"

    def test_garbled_old_frames_between_sessions(self):
        # t=13 confirms old session; t=14,t=16 extract but OCR fails (garbled old-session);
        # t=17 jumps (new session). Cut = max(last_old_t+1, t-1) = max(14, 16) = 16:
        # garbled frames 14-15 stay in old clip, new clip starts at 16 (just before clean new).
        path13 = "/tmp/frame_13.000.bmp"
        path14 = "/tmp/frame_14.000.bmp"
        path16 = "/tmp/frame_16.000.bmp"
        path17 = "/tmp/frame_17.000.bmp"

        def extract(v, t, c, d):
            return {13: path13, 14: path14, 16: path16, 17: path17}.get(t)

        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=extract,
            ocr_map={
                path13: "5:03 PM\n 1/ 4/90",  # old session confirmed
                path14: "5:03 PM 4/90",         # garbled (missing month) → None
                path16: "5:03 PM 4/90",         # garbled → None
                path17: "5:10 PM\n 1/ 4/90",   # cam_advance=420s > 7+300 → jump
            },
        )
        assert t == 16.0  # max(13+1, 17-1) = max(14, 16) = 16
        assert method == "ocr"

    def test_detects_backward_jump(self):
        # cam_advance < -1800 → backward jump triggers split; last_old_t still prev_t=10.
        # Cut = max(last_old_t+1, t-1) = max(11, 14) = 14.
        path = "/tmp/frame_15.000.bmp"
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "10:00 AM\n 1/ 4/90"},
        )
        assert t == 14.0  # max(10+1, 15-1) = max(11, 14) = 14
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
        # Old session confirmed at t=5; garbled frames extracted at t=18,19 (coarse_t=20).
        # Gap = coarse_t - last_old_t = 20-5 = 15 > 10 → substantial gap, fix fires.
        path5 = "/tmp/frame_5.000.bmp"
        path18 = "/tmp/frame_18.000.bmp"

        def extract(v, t, c, d):
            if t == 5:
                return path5
            if t == 18:
                return path18
            return None

        t, method = _run(
            coarse_t=20.0, prev_t=1.0,  # 20-1=19s < SPLICE_DEAD_ZONE_MAX_S
            extract_side_effect=extract,
            ocr_map={
                path5:  "5:04 PM\n 1/ 4/90",  # old session (cam_advance=240s < 4+300)
                path18: "11:43 AM 5/90",        # garbled new-session (missing day)
            },
        )
        assert t == 6.0    # last_old_t(5) + 1; gap=15 > 10 → fix fires
        assert method == "ocr"

    def test_garbled_small_gap_falls_back_to_coarse(self):
        # Old session confirmed at t=17; garbled frame at t=18 (coarse_t=20).
        # Gap = 20-17 = 3 ≤ 10 → normal end-of-window sparseness, fix must NOT fire.
        path17 = "/tmp/frame_17.000.bmp"
        path18 = "/tmp/frame_18.000.bmp"

        def extract(v, t, c, d):
            if t == 17:
                return path17
            if t == 18:
                return path18
            return None

        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=extract,
            ocr_map={
                path17: "5:04 PM\n 1/ 4/90",
                path18: "11:43 AM 5/90",
            },
        )
        assert t == 20.0   # gap=3 ≤ 10 → coarse_t
        assert method == "coarse"

    def test_garbled_new_session_long_dead_zone_falls_back_to_coarse(self):
        # Window >= SPLICE_DEAD_ZONE_MAX_S (120s) with substantial post-old gap.
        # Long Dead Zone: fix must NOT fire; fall back to coarse_t.
        path5 = "/tmp/frame_5.000.bmp"
        path18 = "/tmp/frame_18.000.bmp"

        def extract(v, t, c, d):
            if t == 5:
                return path5
            if t == 18:
                return path18
            return None

        t, method = _run(
            coarse_t=200.0, prev_t=1.0,  # 200-1=199s >= SPLICE_DEAD_ZONE_MAX_S
            extract_side_effect=extract,
            ocr_map={
                path5:  "5:04 PM\n 1/ 4/90",
                path18: "11:43 AM 5/90",
            },
        )
        assert t == 200.0  # Long Dead Zone → coarse_t
        assert method == "coarse"

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
        # Cut = max(last_old_t+1, t-1) = max(11, 14) = 14.
        path = "/tmp/frame_15.000.bmp"
        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=lambda v, t, c, d: path if t == 15 else None,
            ocr_map={path: "5:10 PM\n 1/ 4/90"},
            visual_times=[13.0],
        )
        assert t == 14.0  # OCR wins over visual; max(11, 14) = 14
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
