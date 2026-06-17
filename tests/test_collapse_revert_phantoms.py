"""
_collapse_revert_phantoms() — remove OCR-misread phantoms bracketed by opposite-sign
cam_jump_s values with short clip duration.
"""
from datetime import datetime

from split_homevideo import Boundary, _collapse_revert_phantoms

_DT = datetime(1990, 1, 4, 17, 1)


def _b(video_t, cam_jump_s):
    return Boundary(
        video_t=float(video_t), type="large_gap",
        cam_before=_DT, cam_after=_DT,
        cam_jump_s=cam_jump_s, prev_t=float(video_t) - 10, prev_dt=_DT,
    )


def _bmap(*boundaries):
    return {b.video_t: b for b in boundaries}


class TestNoCollapse:
    def test_empty_cuts(self):
        assert _collapse_revert_phantoms([0.0], {}) == [0.0]

    def test_single_cut_no_change(self):
        b = _b(100, cam_jump_s=5000)
        assert _collapse_revert_phantoms([0.0, 100.0], _bmap(b)) == [0.0, 100.0]

    def test_same_sign_jumps_not_collapsed(self):
        # Both positive jumps: real session boundaries, not phantom
        b1 = _b(100, cam_jump_s=5000)
        b2 = _b(200, cam_jump_s=4000)
        result = _collapse_revert_phantoms([0.0, 100.0, 200.0], _bmap(b1, b2))
        assert result == [0.0, 100.0, 200.0]

    def test_opposite_sign_but_long_clip_not_collapsed(self):
        # Clip is 200s > DEFAULT_MIN_CLIP_S=120s → keep even with opposite signs
        b1 = _b(100, cam_jump_s=5000)
        b2 = _b(300, cam_jump_s=-5000)  # 200s gap
        result = _collapse_revert_phantoms([0.0, 100.0, 300.0], _bmap(b1, b2))
        assert result == [0.0, 100.0, 300.0]

    def test_boundary_not_in_map_not_collapsed(self):
        # b2 missing from map — cannot confirm phantom
        b1 = _b(100, cam_jump_s=5000)
        result = _collapse_revert_phantoms([0.0, 100.0, 110.0], _bmap(b1))
        assert result == [0.0, 100.0, 110.0]


class TestCollapsePhantom:
    def test_forward_then_backward_short_collapsed(self):
        # A→B→A pattern: misread year jumps +9yr then reverts -9yr in 10s
        b1 = _b(100, cam_jump_s=283_000_000)   # +9 years (forward)
        b2 = _b(110, cam_jump_s=-283_000_000)  # -9 years (backward)
        result = _collapse_revert_phantoms([0.0, 100.0, 110.0], _bmap(b1, b2), min_phantom_s=120)
        assert result == [0.0]

    def test_backward_then_forward_short_collapsed(self):
        # Misread month: jumps back 6 months, then forward 6 months in 10s
        b1 = _b(200, cam_jump_s=-15_000_000)  # ~6 months backward
        b2 = _b(210, cam_jump_s=15_000_000)   # ~6 months forward
        result = _collapse_revert_phantoms([0.0, 200.0, 210.0], _bmap(b1, b2), min_phantom_s=120)
        assert result == [0.0]

    def test_phantom_skips_both_cuts(self):
        # Real cut at 50, phantom at 100-110, real cut at 300
        b_real1 = _b(50, cam_jump_s=5000)
        b_phantom1 = _b(100, cam_jump_s=283_000_000)
        b_phantom2 = _b(110, cam_jump_s=-283_000_000)
        b_real2 = _b(300, cam_jump_s=5000)
        bmap = _bmap(b_real1, b_phantom1, b_phantom2, b_real2)
        result = _collapse_revert_phantoms(
            [0.0, 50.0, 100.0, 110.0, 300.0], bmap, min_phantom_s=120
        )
        assert result == [0.0, 50.0, 300.0]

    def test_consecutive_phantoms_both_collapsed(self):
        # Two back-to-back phantom pairs
        b1 = _b(100, cam_jump_s=5000)
        b2 = _b(110, cam_jump_s=-5000)
        b3 = _b(200, cam_jump_s=5000)
        b4 = _b(210, cam_jump_s=-5000)
        bmap = _bmap(b1, b2, b3, b4)
        result = _collapse_revert_phantoms(
            [0.0, 100.0, 110.0, 200.0, 210.0], bmap, min_phantom_s=120
        )
        assert result == [0.0]

    def test_custom_min_phantom_s(self):
        # Clip is 90s — under 120s default but above 60s custom threshold
        b1 = _b(100, cam_jump_s=5000)
        b2 = _b(190, cam_jump_s=-5000)
        bmap = _bmap(b1, b2)
        # with 120s threshold: 90s < 120s → collapse
        assert _collapse_revert_phantoms([0.0, 100.0, 190.0], bmap, min_phantom_s=120) == [0.0]
        # with 60s threshold: 90s >= 60s → keep
        assert _collapse_revert_phantoms([0.0, 100.0, 190.0], bmap, min_phantom_s=60) == [0.0, 100.0, 190.0]
