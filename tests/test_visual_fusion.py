"""
detect_visual_boundaries() and fuse_boundaries() — corroborate OCR jump detection
with independent scene-cut/black-frame signals to reject isolated OCR misreads.
"""
import json
import os
import unittest.mock as mock
from datetime import datetime

from split_homevideo import _VISUAL_CACHE_FORMAT, Boundary, detect_visual_boundaries, fuse_boundaries

_DT = datetime(1990, 1, 4, 17, 1)


def _boundary(video_t: float, prev_t: float | None = None) -> Boundary:
    return Boundary(
        video_t=video_t, type="large_gap", cam_before=_DT, cam_after=_DT,
        cam_jump_s=10000.0, prev_t=prev_t if prev_t is not None else video_t - 10, prev_dt=_DT,
    )


def _ffmpeg_stderr(scene_pts: list[float], black_starts: list[float]) -> str:
    lines = []
    for t in scene_pts:
        lines.append(f"[Parsed_showinfo_1 @ 0x0] n: 0 pts: 1 pts_time:{t} pos: 0")
    for t in black_starts:
        lines.append(f"[Parsed_blackdetect_0 @ 0x0] black_start:{t} black_end:{t+0.5} black_duration:0.5")
    return "\n".join(lines)


class TestDetectVisualBoundaries:
    def test_parses_scene_cuts_and_black_frames(self, tmp_path):
        stderr = _ffmpeg_stderr(scene_pts=[12.5, 48.0], black_starts=[12.4])
        with mock.patch("subprocess.run", return_value=mock.Mock(stderr=stderr)):
            scene_cuts, black_frames = detect_visual_boundaries("fake.mp4")
        assert scene_cuts == [12.5, 48.0]
        assert black_frames == [12.4]

    def test_no_matches_returns_empty_lists(self, tmp_path):
        with mock.patch("subprocess.run", return_value=mock.Mock(stderr="")):
            scene_cuts, black_frames = detect_visual_boundaries("fake.mp4")
        assert scene_cuts == []
        assert black_frames == []

    def test_cache_hit_skips_subprocess(self, tmp_path):
        cache_path = str(tmp_path / "visual_cache.json")
        with open(cache_path, "w") as f:
            json.dump({
                "cache_format": _VISUAL_CACHE_FORMAT,
                "scene_threshold": 0.4,
                "black_min_duration": 0.1,
                "scene_cuts": [1.0, 2.0],
                "black_frames": [3.0],
            }, f)
        with mock.patch("subprocess.run") as m_run:
            scene_cuts, black_frames = detect_visual_boundaries(
                "fake.mp4", scene_threshold=0.4, black_min_duration=0.1, cache_path=cache_path
            )
        m_run.assert_not_called()
        assert scene_cuts == [1.0, 2.0]
        assert black_frames == [3.0]

    def test_threshold_mismatch_triggers_rescan(self, tmp_path):
        cache_path = str(tmp_path / "visual_cache.json")
        with open(cache_path, "w") as f:
            json.dump({
                "cache_format": _VISUAL_CACHE_FORMAT,
                "scene_threshold": 0.9,  # different from requested 0.4
                "black_min_duration": 0.1,
                "scene_cuts": [],
                "black_frames": [],
            }, f)
        with mock.patch("subprocess.run", return_value=mock.Mock(stderr="")) as m_run:
            detect_visual_boundaries("fake.mp4", scene_threshold=0.4, cache_path=cache_path)
        m_run.assert_called_once()

    def test_cache_written_after_detection(self, tmp_path):
        cache_path = str(tmp_path / "visual_cache.json")
        stderr = _ffmpeg_stderr(scene_pts=[5.0], black_starts=[])
        with mock.patch("subprocess.run", return_value=mock.Mock(stderr=stderr)):
            detect_visual_boundaries("fake.mp4", cache_path=cache_path)
        assert os.path.exists(cache_path)
        data = json.loads(open(cache_path).read())
        assert data["scene_cuts"] == [5.0]


class TestFuseBoundaries:
    def test_corroborated_by_scene_cut_kept(self):
        b = _boundary(100.0)
        result = fuse_boundaries([b], scene_cuts=[102.0], black_frames=[], window_s=5.0)
        assert result == [b]

    def test_corroborated_by_black_frame_kept(self):
        b = _boundary(100.0)
        result = fuse_boundaries([b], scene_cuts=[], black_frames=[97.0], window_s=5.0)
        assert result == [b]

    def test_uncorroborated_boundary_dropped(self):
        b = _boundary(100.0)
        result = fuse_boundaries([b], scene_cuts=[500.0], black_frames=[], window_s=5.0)
        assert result == []

    def test_no_visual_signals_drops_all(self):
        b = _boundary(100.0)
        result = fuse_boundaries([b], scene_cuts=[], black_frames=[], window_s=5.0)
        assert result == []

    def test_outside_window_dropped(self):
        b = _boundary(100.0)
        result = fuse_boundaries([b], scene_cuts=[106.0], black_frames=[], window_s=5.0)
        assert result == []

    def test_exactly_at_window_edge_kept(self):
        b = _boundary(100.0)
        result = fuse_boundaries([b], scene_cuts=[105.0], black_frames=[], window_s=5.0)
        assert result == [b]

    def test_multiple_boundaries_filtered_independently(self):
        b1 = _boundary(100.0)
        b2 = _boundary(500.0)
        result = fuse_boundaries([b1, b2], scene_cuts=[101.0], black_frames=[], window_s=5.0)
        assert result == [b1]


class TestDetectVisualBoundariesStaleCacheReason:
    """Stale-cache diagnostic messages — each mismatch field logs its own reason."""

    def _write_cache(self, path, scene_threshold=0.4, black_min_duration=0.1):
        with open(path, "w") as f:
            json.dump({
                "cache_format": _VISUAL_CACHE_FORMAT,
                "scene_threshold": scene_threshold,
                "black_min_duration": black_min_duration,
                "scene_cuts": [],
                "black_frames": [],
            }, f)

    def test_black_min_duration_mismatch_triggers_rescan(self, tmp_path):
        # Covers line 861: black_min_duration mismatch prints stale-cache reason.
        cache_path = str(tmp_path / "vcache.json")
        self._write_cache(cache_path, black_min_duration=0.5)  # stored: 0.5, requested: 0.1
        with mock.patch("subprocess.run", return_value=mock.Mock(stderr="")) as m_run:
            detect_visual_boundaries(
                "fake.mp4", scene_threshold=0.4, black_min_duration=0.1,
                cache_path=cache_path,
            )
        m_run.assert_called_once()

    def test_cache_format_mismatch_triggers_rescan(self, tmp_path):
        # Covers line 857: wrong cache_format adds to the why list.
        from split_homevideo import _VISUAL_CACHE_FORMAT
        cache_path = str(tmp_path / "vcache.json")
        with open(cache_path, "w") as f:
            json.dump({
                "cache_format": _VISUAL_CACHE_FORMAT - 1,  # stale format
                "scene_threshold": 0.4,
                "black_min_duration": 0.1,
                "scene_cuts": [],
                "black_frames": [],
            }, f)
        with mock.patch("subprocess.run", return_value=mock.Mock(stderr="")) as m_run:
            detect_visual_boundaries(
                "fake.mp4", scene_threshold=0.4, black_min_duration=0.1,
                cache_path=cache_path,
            )
        m_run.assert_called_once()

    def test_scene_threshold_mismatch_reason_logged(self, tmp_path, capsys):
        # Covers line 857: scene_threshold mismatch generates a stale-cache message.
        cache_path = str(tmp_path / "vcache.json")
        self._write_cache(cache_path, scene_threshold=0.9)
        with mock.patch("subprocess.run", return_value=mock.Mock(stderr="")):
            detect_visual_boundaries(
                "fake.mp4", scene_threshold=0.4, cache_path=cache_path,
            )
        captured = capsys.readouterr()
        assert "scene_threshold" in captured.out
