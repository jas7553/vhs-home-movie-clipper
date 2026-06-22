"""
ocr_refinement(): dense 1s scan to locate exact session boundary.
"""
import tempfile
import unittest.mock as mock
from datetime import datetime

from split_homevideo import (
    SPLICE_DEAD_ZONE_MAX_S,
    Boundary,
    Reading,
    _gap_date_class,
    _lenient_months,
    _place_content_aware,
    ocr_refinement,
)

_CROP = "250:110:385:370"
_GAP = 300
_PREV_DT = datetime(1990, 1, 4, 17, 0)


def _boundary(coarse_t, prev_t, prev_dt=_PREV_DT, cam_after=None):
    return Boundary(
        video_t=coarse_t, type="large_gap",
        cam_before=prev_dt, cam_after=cam_after, cam_jump_s=0.0,
        prev_t=prev_t, prev_dt=prev_dt,
    )


def _run(coarse_t, prev_t, extract_side_effect, ocr_map, visual_times=None, interval=10,
         prev_dt=_PREV_DT, cam_after=None):
    b = _boundary(coarse_t, prev_t, prev_dt, cam_after=cam_after)
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

    def test_false_positive_new_session_rejected_by_revert(self):
        # Frame t=14 reads new-session (5/19) but t=15 reverts to old (5/12) → false
        # positive; frame t=18 is the sustained new-session start.
        # Fix 1: _scan_for_transition resets candidate_new_t on revert, so
        # first_new_t=18, last_old_t=15.  Fix 2: pure-noise gap [16,17] → last_old+1=16.
        prev_dt = datetime(1990, 5, 12, 15, 0)
        _OLD = "3:00 PM\n 5/12/90"
        _NEW = "3:00 PM\n 5/19/90"
        paths = {13: "/tmp/fp_f13.bmp", 14: "/tmp/fp_f14.bmp",
                 15: "/tmp/fp_f15.bmp", 18: "/tmp/fp_f18.bmp"}

        def extract(v, t, c, d):
            return paths.get(t)

        t, method = _run(
            coarse_t=20.0, prev_t=10.0, prev_dt=prev_dt,
            extract_side_effect=extract,
            ocr_map={paths[13]: _OLD, paths[14]: _NEW, paths[15]: _OLD, paths[18]: _NEW},
            interval=10,
        )
        # Without fix 1: first_new_t=14 (stops at first jump), cut=last_old(10)+1=11
        # or max(11,13)=13 depending on gap classification.
        # With fix 1: first_new_t=18, last_old_t=15.  Gap [16,17] pure noise → 16.
        assert t == 16.0
        assert method == "ocr"

    def test_intermediate_date_kept_with_outgoing_clip(self):
        # Mirrors the real 5/09→5/19 head-leak: 4 frames of 5/12 appear in the
        # dense scan between the 5/09 and 5/19 sessions.  cam_after=5/19 means
        # expected_new_date=5/19; the 5/12 frames look 'new' (big cam jump from
        # 5/09) but don't match → treated as intermediate content kept with the
        # outgoing clip.  last_old_t advances through 5/12 frames; first_new_t is
        # the first 5/19 frame.  Gap between last-5/12 (t=16) and first-5/19
        # (t=18) is pure noise (t=17 → None) → cut = last_old_t+1 = 17.
        prev_dt = datetime(1990, 5, 9, 14, 59)
        cam_after = datetime(1990, 5, 19, 13, 36)
        _OLD = "2:59 PM\n 5/ 9/90"
        _MID = "2:36 PM\n 5/12/90"   # intermediate: looks new vs 5/09 but ≠ cam_after date
        _NEW = "1:37 PM\n 5/19/90"
        paths = {13: "/tmp/im_f13.bmp", 15: "/tmp/im_f15.bmp",
                 16: "/tmp/im_f16.bmp", 18: "/tmp/im_f18.bmp"}

        def extract(v, t, c, d):
            return paths.get(t)

        t, method = _run(
            coarse_t=20.0, prev_t=10.0, prev_dt=prev_dt, cam_after=cam_after,
            extract_side_effect=extract,
            ocr_map={paths[13]: _OLD, paths[15]: _MID, paths[16]: _MID, paths[18]: _NEW},
            interval=10,
        )
        # Without fix: 5/12 frames at t=15,16 trigger first_new_t=15; cut=14
        #   → 5/12 frames appear at 1-2s into 5/19 clip (head leak).
        # With fix: 5/12 kept as intermediate → last_old_t=16, first_new_t=18;
        #   gap [17] is pure noise → cut = 16+1 = 17.
        assert t == 17.0
        assert method == "ocr"

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

    def test_two_pass_false_positive_in_coarse_rejected(self):
        # Coarse scan: t=101 new (false positive) then t=105 old (revert) then t=161 new (true).
        # Fix 1 resets candidate in coarse, so last_old_c=105, first_new_c=161.
        # Dense scan on [105..161] finds last_old=159, first_new=161 → cut=max(160,160)=160.
        # step=4, coarse_times=[1,5,9,...,197]. t=101,105,161 are coarse samples only if
        # 101 % 4 == 1, i.e., (101-1) % 4 == 0 → yes (t=1,5,...,101,105,...).
        paths = {t: f"/tmp/ldz_fp_{t}.bmp" for t in range(1, 201)}

        def extract(v, t, c, d):
            return paths.get(int(t))

        # False positive at 101, revert at 105, true new from 161 onward.
        ocr_map = {
            paths[t]: (_OLD if t != 101 and t < 161 else _NEW)
            for t in range(1, 201)
        }
        t, method = self._run_ldz(extract, ocr_map)
        # last_old_t ≥ 159 (dense scan within [105,161]); first_new_t ≤ 161.
        # cut = max(last_old+1, first_new-1) or last_old+1 depending on gap.
        # last_old_t=160 (or wherever), first_new_t=161 → cut=161 or 160+1=161.
        assert t >= 159.0
        assert t <= 161.0
        assert method == "ocr"

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


# ---------------------------------------------------------------------------
# Content-aware gap placement: classify garbled gap frames as old/new/noise so
# the cut keeps new-date content out of the outgoing clip (REQUIREMENTS L23) but
# still keeps old-date / noise garble with it (ADR-0001). Strings below are real
# OCR captured from Converse 1990.mp4 at the named boundaries (.scratch/probe_gaps.py).
# ---------------------------------------------------------------------------

class TestGapDateClass:
    def test_clip71_garbled_new_classified_new(self):
        old, new = datetime(1990, 9, 29), datetime(1990, 10, 4)
        for raw in ("4/90", "074/90", "07 4/90", "0/ 4/90", "102 4/90", "107 4/90"):
            assert _gap_date_class(raw, old, new) == "new", raw

    def test_b5717_garbled_old_classified_old(self):
        old, new = datetime(1990, 2, 17), datetime(1990, 2, 21)
        assert _gap_date_class("6:15-P 2717/90", old, new) == "old"

    def test_b5717_no_frame_misclassified_new(self):
        # An all-old garble gap must never produce a 'new' classification (would
        # leak old footage forward). 2/21 day '21' appears nowhere in the gap.
        old, new = datetime(1990, 2, 17), datetime(1990, 2, 21)
        for raw in ("6:15- 271 730", ":15 PM 7290,", "6:15-B 27 790", "6:15 A 27 1130"):
            assert _gap_date_class(raw, old, new) != "new", raw

    def test_b457_new_via_day_before_year(self):
        old, new = datetime(1990, 1, 4), datetime(1990, 1, 5)
        assert _gap_date_class("11:40 AM 5/90", old, new) == "new"
        assert _gap_date_class("", old, new) == "noise"

    def test_b10567_new_via_leading_month(self):
        # day 6 is hard to recover mid-gap; the leading month '5/' carries it.
        old, new = datetime(1990, 4, 29), datetime(1990, 5, 6)
        assert _gap_date_class("15 -5/ A 790", old, new) == "new"

    def test_same_field_shared_is_ignored(self):
        # old 1/4, new 1/4 (time-only jump): no field discriminates → never 'new'.
        old = new = datetime(1990, 1, 4)
        assert _gap_date_class("5:03 PM 4/90", old, new) == "noise"


class TestPlaceContentAware:
    def _readings(self, mapping):
        from split_homevideo import parse_timestamp
        return {t: Reading(parse_timestamp(raw), raw) for t, raw in mapping.items()}

    def test_garbled_new_cuts_at_content_start(self):
        # clip71: gap 6..14 is garbled 10/4; cut must land at gap start (6), not first_new-1 (14).
        old, new = datetime(1990, 9, 29), datetime(1990, 10, 4)
        raws = {5: "9/29/90", 6: "4/90", 7: "074/90", 8: "07 4/90", 9: "06",
                10: "4/90 10", 11: "0/ 4/90", 12: "102 4/90", 13: "107 4/90",
                14: "4/90", 15: "10/ 4/90"}
        window = list(range(2, 30))
        cut = _place_content_aware(window, self._readings(raws), 5.0, 15.0, old, new)
        assert cut == 6.0

    def test_garbled_old_falls_back_to_end_of_gap(self):
        # b_5717: gap is garbled 2/17 (old); keep it with old clip → first_new-1.
        old, new = datetime(1990, 2, 17), datetime(1990, 2, 21)
        raws = {5: "6:15 PM 2/17/90", 6: "6:15- 271 730", 7: "6:15-P 2717/90",
                8: "6:15-B 27 790", 15: "6:51 PM 2/21/90"}
        window = list(range(2, 30))
        cut = _place_content_aware(window, self._readings(raws), 5.0, 15.0, old, new)
        assert cut == 14.0  # max(5+1, 15-1)

    def test_old_then_noise_then_new_cuts_at_new_run(self):
        # b_18577 shape: garbled-old, then a noise burst, then garbled-new at the end.
        old, new = datetime(1990, 8, 4), datetime(1990, 8, 9)
        raws = {5: "38 PM 8/ 4 /90", 6: "87-4790", 7: "·4790", 8: "", 9: "",
                12: "8/ 34 PM 9/90", 15: "84 PM 8/ 9/90"}
        window = list(range(2, 30))
        cut = _place_content_aware(window, self._readings(raws), 5.0, 15.0, old, new)
        assert cut == 12.0  # first 'new'-classified frame after the old garble

    def test_same_date_jump_uses_fallback(self):
        # new_dt date == old_dt date (time-only jump) → no content reasoning, fallback.
        old = new = datetime(1990, 1, 4)
        raws = {5: "5:00 PM 1/ 4/90", 10: "5:10 PM 1/ 4/90"}
        window = list(range(2, 30))
        cut = _place_content_aware(window, self._readings(raws), 5.0, 10.0, old, new)
        assert cut == 9.0  # max(5+1, 10-1)

    def test_pure_noise_gap_cuts_at_last_old_plus_one(self):
        # Gap [6..14] all empty strings → all "noise" → no old/new garble detected.
        # Old fallback would be max(6, 14)=14; new L23 rule: last_old_t+1=6.
        old, new = datetime(1990, 5, 12), datetime(1990, 5, 19)
        raws = {5: "5:00 PM\n 5/12/90", 6: "", 7: "", 8: "", 9: "", 15: "5:00 PM\n 5/19/90"}
        window = list(range(2, 30))
        cut = _place_content_aware(window, self._readings(raws), 5.0, 15.0, old, new)
        assert cut == 6.0  # last_old_t(5) + 1; no date content in gap

    def test_pure_noise_gap_visual_anchor_used(self):
        # Pure noise gap with a visual event: anchor to end of noise burst.
        old, new = datetime(1990, 5, 12), datetime(1990, 5, 19)
        raws = {5: "5:00 PM\n 5/12/90", 6: "", 7: "", 8: "", 9: "", 15: "5:00 PM\n 5/19/90"}
        window = list(range(2, 30))
        cut = _place_content_aware(window, self._readings(raws), 5.0, 15.0, old, new,
                                   visual_times=[8.5])
        assert cut == 8.5  # visual anchor inside gap; end of noise burst

    def test_pure_noise_gap_visual_anchor_outside_gap_ignored(self):
        # Visual event outside (last_old_t, first_new_t) must not be used.
        old, new = datetime(1990, 5, 12), datetime(1990, 5, 19)
        raws = {5: "5:00 PM\n 5/12/90", 6: "", 7: "", 8: "", 9: "", 15: "5:00 PM\n 5/19/90"}
        window = list(range(2, 30))
        cut = _place_content_aware(window, self._readings(raws), 5.0, 15.0, old, new,
                                   visual_times=[4.0, 16.0])
        assert cut == 6.0  # no anchor in gap → last_old_t+1


class TestContentAwareEndToEnd:
    def test_clip71_short_span_policy_cuts_early(self):
        # Full ShortSpanPolicy path with real garbled-new gap strings: the cut must
        # move from the old max() result (14) to the new content start (6).
        # Footage here is date-only (overlay shows no time) → prev_dt is midnight,
        # matching the date-only "9/29/90" old frames (which parse to midnight).
        prev_dt = datetime(1990, 9, 29)
        raws = {5: "9/29/90", 6: "4/90", 7: "074/90", 8: "07 4/90", 9: "06",
                10: "4/90 10", 11: "0/ 4/90", 12: "102 4/90", 13: "107 4/90",
                14: "4/90", 15: "10/ 4/90"}
        paths = {t: f"/tmp/f{t}.bmp" for t in raws}

        def extract(v, t, c, d):
            return paths.get(int(t))

        ocr_map = {paths[t]: raws[t] for t in raws}
        t, method = _run(
            coarse_t=20.0, prev_t=1.0, extract_side_effect=extract,
            ocr_map=ocr_map, interval=10, prev_dt=prev_dt,
        )
        assert t == 6.0
        assert method == "ocr"


class TestLenientMonths:
    def test_month_over_12_retries_trailing_digit(self):
        # "15/" → group(1)="15" > 12 → line 994: retry trailing digit → 5
        assert _lenient_months("15/90") == {5}

    def test_normal_month_not_retried(self):
        assert _lenient_months("4/90") == {4}

    def test_empty_string_returns_empty(self):
        assert _lenient_months("") == set()


class TestOcrRefinementNoPrev:
    def test_boundary_with_no_prev_t_returns_coarse(self):
        # line 1182: prev_t=None → immediate coarse fallback with reason "no-prev"
        b = Boundary(
            video_t=100.0, type="large_gap",
            cam_before=None, cam_after=None, cam_jump_s=0.0,
            prev_t=None, prev_dt=None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ocr_refinement(_GAP, _CROP, tmpdir, 10, None)
            result = strategy("vid.mp4", b)
        assert result.t == 100.0
        assert result.method == "coarse"
        assert result.detail == "no-prev"

    def test_boundary_with_no_prev_dt_returns_coarse(self):
        b = Boundary(
            video_t=100.0, type="large_gap",
            cam_before=None, cam_after=None, cam_jump_s=0.0,
            prev_t=90.0, prev_dt=None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ocr_refinement(_GAP, _CROP, tmpdir, 10, None)
            result = strategy("vid.mp4", b)
        assert result.t == 100.0
        assert result.method == "coarse"
        assert result.detail == "no-prev"
