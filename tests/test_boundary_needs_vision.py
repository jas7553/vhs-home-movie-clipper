"""
_boundary_needs_vision(): None-span-width pre-filter deciding which large_gap
boundaries get 1s vision frames exported.

A large_gap window is all-None by construction (prev_t = last OLD sample,
coarse_t = first NEW sample). The only signal is the width of that None-span:
Splice Dead Zone (narrow) → export; Long Dead Zone (wide) → skip (ADR 0001).
"""
from datetime import datetime

from split_homevideo import SPLICE_DEAD_ZONE_MAX_S, Boundary, _boundary_needs_vision

_OLD = datetime(1990, 1, 4, 17, 0)
_NEW = datetime(1990, 1, 7, 9, 0)


def _boundary(prev_t, coarse_t, btype="large_gap"):
    return Boundary(
        video_t=coarse_t, type=btype, cam_before=_OLD, cam_after=_NEW,
        cam_jump_s=(_NEW - _OLD).total_seconds(), prev_t=prev_t, prev_dt=_OLD,
    )


class TestBoundaryNeedsVision:
    def test_narrow_splice_dead_zone_exports(self):
        b = _boundary(prev_t=1000.0, coarse_t=1050.0)   # 50s span
        needs, reason = _boundary_needs_vision(b)
        assert needs is True
        assert "Splice Dead Zone" in reason

    def test_wide_long_dead_zone_skips(self):
        b = _boundary(prev_t=1000.0, coarse_t=1000.0 + 520.0)
        needs, reason = _boundary_needs_vision(b)
        assert needs is False
        assert "Long Dead Zone" in reason

    def test_threshold_boundary_is_long_dead_zone(self):
        # Exactly at the threshold counts as Long Dead Zone (>= is out of scope).
        b = _boundary(prev_t=1000.0, coarse_t=1000.0 + SPLICE_DEAD_ZONE_MAX_S)
        needs, _ = _boundary_needs_vision(b)
        assert needs is False

    def test_just_under_threshold_exports(self):
        b = _boundary(prev_t=1000.0, coarse_t=1000.0 + SPLICE_DEAD_ZONE_MAX_S - 1)
        needs, _ = _boundary_needs_vision(b)
        assert needs is True

    def test_non_large_gap_not_refinable(self):
        b = _boundary(prev_t=1000.0, coarse_t=1050.0, btype="gap")
        needs, reason = _boundary_needs_vision(b)
        assert needs is False
        assert "not a refinable" in reason

    def test_missing_prev_t_not_refinable(self):
        b = Boundary(
            video_t=1050.0, type="large_gap", cam_before=_OLD, cam_after=_NEW,
            cam_jump_s=1.0, prev_t=None, prev_dt=None,
        )
        needs, _ = _boundary_needs_vision(b)
        assert needs is False
