"""
group_clips() — mode filtering of boundaries into cut points.
"""
from datetime import datetime

from split_homevideo import Boundary, group_clips

_DT = datetime(1990, 1, 4, 17, 1)


def _boundary(video_t, btype="large_gap", cam_before=None, cam_after=None, cam_jump_s=10000.0):
    return Boundary(
        video_t=video_t, type=btype,
        cam_before=cam_before if cam_before is not None else _DT,
        cam_after=cam_after if cam_after is not None else _DT,
        cam_jump_s=cam_jump_s, prev_t=video_t - 10, prev_dt=_DT,
    )


class TestModeFiltering:
    def test_scene_includes_all_types(self):
        boundaries = [_boundary(100, "gap"), _boundary(200, "large_gap")]
        assert group_clips(boundaries, "scene") == [0.0, 100, 200]

    def test_session_only_large_gap(self):
        boundaries = [_boundary(100, "gap"), _boundary(200, "large_gap")]
        assert group_clips(boundaries, "session") == [0.0, 200]

    def test_daily_cuts_on_date_change(self):
        b = _boundary(100, cam_before=datetime(1990, 1, 4), cam_after=datetime(1990, 1, 5))
        assert group_clips([b], "daily") == [0.0, 100]

    def test_daily_no_cut_same_date(self):
        b = _boundary(100, cam_before=datetime(1990, 1, 4, 9), cam_after=datetime(1990, 1, 4, 18))
        assert group_clips([b], "daily") == [0.0]
