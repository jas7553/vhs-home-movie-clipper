"""
ocr_refinement(): dense 1s scan to locate exact session boundary.
"""
import tempfile
import unittest.mock as mock
from datetime import datetime

from split_homevideo import SPLICE_DEAD_ZONE_MAX_S, Boundary, ocr_refinement

_CROP = "250:110:385:370"
_GAP = 300
_PREV_DT = datetime(1990, 1, 4, 17, 0)


def _boundary(coarse_t, prev_t):
    return Boundary(
        video_t=coarse_t, type="large_gap",
        cam_before=_PREV_DT, cam_after=None, cam_jump_s=0.0,
        prev_t=prev_t, prev_dt=_PREV_DT,
    )


def _run(coarse_t, prev_t, extract_side_effect, ocr_map, visual_times=None, interval=10):
    b = _boundary(coarse_t, prev_t)
    with tempfile.TemporaryDirectory() as tmpdir:
        strategy = ocr_refinement(_GAP, _CROP, tmpdir, interval, visual_times)
        with mock.patch("split_homevideo.extract_frame", side_effect=extract_side_effect), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            result = strategy("vid.mp4", b)
            return result.t, result.method


class TestRefineSplit:
    def test_empty_window_returns_coarse(self):
        # interval=1: range(int(10)+1, int(10)+1) = range(11,11) = []
        with tempfile.TemporaryDirectory() as tmpdir:
            result = ocr_refinement(_GAP, _CROP, tmpdir, 1, None)("vid.mp4", _boundary(10.0, 10.0))
        assert result.t == 10.0
        assert result.method == "coarse"

    def test_adjacent_frames_empty_window(self):
        # interval=1: range(int(9.5)+1, int(9.5)+1) = range(10,10) = []
        with tempfile.TemporaryDirectory() as tmpdir:
            result = ocr_refinement(_GAP, _CROP, tmpdir, 1, None)("vid.mp4", _boundary(9.5, 9.5))
        assert result.t == 9.5
        assert result.method == "coarse"

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

    # --- coarse bucket tail coverage (regression: session change past coarse_t) ---

    def test_change_inside_coarse_bucket_tail_found(self):
        # The coarse scan majority-votes 3 frames per 10s bucket: the bucket at t=20
        # samples the video at 20, 23.3, 26.7s. If 2/3 frames are new-session the bucket
        # is labelled new at t=20, but the actual change is at 26s (inside the bucket tail).
        # Old window [11,19] was all-old → old code returned coarse=20, leaking 6s of
        # old footage into the new clip. Extended window [11,29] must find the change.
        _OLD = "5:00 PM\n 1/ 4/90"   # cam_advance ≈ 0 relative to prev_dt → old session
        _NEW = "5:10 PM\n 1/ 4/90"   # cam_advance = 600s → jump > GAP(300) → new session

        paths = {t: f"/tmp/f{t}.bmp" for t in range(11, 30)}

        def extract(v, t, c, d):
            return paths.get(t)

        ocr_map = {paths[t]: (_OLD if t < 26 else _NEW) for t in range(11, 30)}

        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=extract,
            ocr_map=ocr_map,
            interval=10,
        )
        # last_old_t=25 (t=25 reads old), first new=26 → cut = max(26, 25) = 26
        assert t == 26.0
        assert method == "ocr"

    def test_change_at_coarse_t_itself_found(self):
        # Session change at exactly coarse_t=20. Old window excluded coarse_t;
        # extended window includes it and must find the change there.
        _OLD = "5:00 PM\n 1/ 4/90"
        _NEW = "5:10 PM\n 1/ 4/90"

        paths = {t: f"/tmp/f{t}.bmp" for t in range(11, 30)}

        def extract(v, t, c, d):
            return paths.get(t)

        ocr_map = {paths[t]: (_OLD if t < 20 else _NEW) for t in range(11, 30)}

        t, method = _run(
            coarse_t=20.0, prev_t=10.0,
            extract_side_effect=extract,
            ocr_map=ocr_map,
            interval=10,
        )
        # last_old_t=19, first new=20 → cut = max(20, 19) = 20
        assert t == 20.0
        assert method == "ocr"


# ---------------------------------------------------------------------------
# Two-pass hierarchical scan (LDZ-sized windows, span >= SPLICE_DEAD_ZONE_MAX_S)
# ---------------------------------------------------------------------------

# Window parameters: prev_t=0, coarse_t=200, interval=1 → span=200 >= 120 → two-pass.
# window = range(1, 201) → 200 elements; step = max(2, 200//50) = 4.
# coarse_times = [1, 5, 9, ..., 197] (50 elements); tail = [198, 199, 200].
_LDZ_PREV_T = 0.0
_LDZ_COARSE_T = 200.0
_LDZ_INTERVAL = 1

_OLD = "5:00 PM\n 1/ 4/90"   # cam_advance ≈ 0 < gap → old session
_NEW = "5:10 PM\n 1/ 4/90"   # cam_advance = 600s > GAP(300) → new session


class TestRefineSplitTwoPass:
    """Hierarchical coarse→dense scan for windows wider than SPLICE_DEAD_ZONE_MAX_S."""

    def _run_ldz(self, extract_side_effect, ocr_map, visual_times=None):
        return _run(
            coarse_t=_LDZ_COARSE_T, prev_t=_LDZ_PREV_T,
            extract_side_effect=extract_side_effect,
            ocr_map=ocr_map,
            visual_times=visual_times,
            interval=_LDZ_INTERVAL,
        )

    def _run_ldz_with_call_count(self, extract_side_effect, ocr_map, visual_times=None):
        """Like _run_ldz but also returns how many times extract_frame was called."""
        b = _boundary(_LDZ_COARSE_T, _LDZ_PREV_T)
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ocr_refinement(_GAP, _CROP, tmpdir, _LDZ_INTERVAL, visual_times)
            with mock.patch("split_homevideo.extract_frame", side_effect=extract_side_effect) as m_ex, \
                 mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
                result = strategy("vid.mp4", b)
                return result.t, result.method, m_ex.call_count

    def test_two_pass_transition_in_middle_correct_result(self):
        # Transition at t=100: frames [1..99] = old, [100..200] = new.
        # Coarse sub-sample (step=4) finds last_old_c=97, first_new_c=101 → dense [97..101].
        # Walk produces: last_old_t=99, first_new_t=100 → cut = max(100, 99) = 100.
        paths = {t: f"/tmp/f{t}.bmp" for t in range(1, 201)}

        def extract(v, t, c, d):
            return paths.get(int(t))

        ocr_map = {paths[t]: (_OLD if t < 100 else _NEW) for t in range(1, 201)}
        t, method, calls = self._run_ldz_with_call_count(extract, ocr_map)
        assert t == 100.0
        assert method == "ocr"
        # Coarse: 50 samples; dense: at most step+2 = 6 frames. Total << 200.
        assert calls < 80

    def test_two_pass_fewer_extractions_than_full_scan(self):
        # Verify that a large window uses far fewer extract_frame calls than its length.
        # Transition at t=50.
        paths = {t: f"/tmp/f{t}.bmp" for t in range(1, 201)}

        def extract(v, t, c, d):
            return paths.get(int(t))

        ocr_map = {paths[t]: (_OLD if t < 50 else _NEW) for t in range(1, 201)}
        _, _, calls = self._run_ldz_with_call_count(extract, ocr_map)
        assert calls < 80  # well under 200

    def test_two_pass_all_none_coarse_skips_dense(self):
        # All extractions fail → coarse is all-None → skip dense scan entirely.
        # Only the ~50 coarse samples should be attempted.
        _, _, calls = self._run_ldz_with_call_count(
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
        )
        assert calls <= 52  # coarse only (50 + possible rounding = ≤52)

    def test_two_pass_all_none_returns_coarse(self):
        # All-None coarse (LDZ): no visual → fallback to coarse_t.
        t, method = self._run_ldz(
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
        )
        assert t == _LDZ_COARSE_T
        assert method == "coarse"

    def test_two_pass_all_none_visual_anchor_not_used(self):
        # LDZ (span >= 120s): visual anchor must NOT fire even with visual times present.
        t, method = self._run_ldz(
            extract_side_effect=lambda v, t, c, d: None,
            ocr_map={},
            visual_times=[50.0],
        )
        assert t == _LDZ_COARSE_T
        assert method == "coarse"

    def test_two_pass_transition_at_start(self):
        # Transition at very first window frame t=1 → cut = coarse_t = max(0+1, 1-1)=max(1,0)=1.
        path1 = "/tmp/f1.bmp"

        def extract(v, t, c, d):
            return path1 if int(t) == 1 else None

        t, method, calls = self._run_ldz_with_call_count(
            extract_side_effect=extract,
            ocr_map={path1: _NEW},
        )
        assert t == 1.0  # max(prev_t+1, t-1) = max(1, 0) = 1
        assert method == "ocr"
        assert calls <= 52  # coarse only (t=1 is first coarse sample, dense = empty)

    def test_two_pass_all_old_in_coarse_scans_tail(self):
        # All coarse samples confirm old session; transition at t=199 (in tail).
        # step=4, coarse_times[-1]=197, tail=[198,199,200]; transition at 199.
        paths = {t: f"/tmp/f{t}.bmp" for t in range(1, 201)}

        def extract(v, t, c, d):
            return paths.get(int(t))

        ocr_map = {paths[t]: (_OLD if t < 199 else _NEW) for t in range(1, 201)}
        t, method, calls = self._run_ldz_with_call_count(extract, ocr_map)
        # last_old_t=198, first_new_t=199 → cut = max(199, 198) = 199
        assert t == 199.0
        assert method == "ocr"
        # Coarse (50) + tail (3) = 53 calls — not the full 200.
        assert calls <= 60

    def test_two_pass_span_below_threshold_uses_full_scan(self):
        # span = SPLICE_DEAD_ZONE_MAX_S - 1 < threshold → full dense scan (SDZ path).
        # Verify all window frames are attempted.
        sdz_coarse_t = _LDZ_PREV_T + SPLICE_DEAD_ZONE_MAX_S - 1  # span=119 < 120
        window_len = int(sdz_coarse_t - _LDZ_PREV_T) + _LDZ_INTERVAL - 1  # ≈119

        b = _boundary(sdz_coarse_t, _LDZ_PREV_T)
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ocr_refinement(_GAP, _CROP, tmpdir, _LDZ_INTERVAL, None)
            with mock.patch("split_homevideo.extract_frame", return_value=None) as m_ex, \
                 mock.patch("split_homevideo.ocr_batch", return_value={}):
                strategy("vid.mp4", b)
                assert m_ex.call_count == window_len
