"""
snap_to_keyframe() and snap_to_keyframe_forward(): ffprobe keyframe scanning.
"""
import unittest.mock as mock

from split_homevideo import snap_to_keyframe, snap_to_keyframe_forward


def _ffprobe_out(lines):
    return mock.Mock(stdout="\n".join(lines) + "\n")


class TestSnapToKeyframe:
    def test_returns_last_keyframe_before_t(self):
        # Two keyframes: 95.0 and 98.0, both <= 100.0; want 98.0
        stdout = _ffprobe_out([
            "packet,95.0,K_",
            "packet,98.0,K_",
            "packet,101.0,K_",  # > t, excluded
        ])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe("vid.mp4", 100.0)
        assert result == 98.0

    def test_returns_start_when_no_keyframe_found(self):
        # No K_ packets → returns start = max(0, t-30)
        stdout = _ffprobe_out(["packet,95.0,_"])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe("vid.mp4", 100.0)
        assert result == 70.0  # max(0, 100-30)

    def test_t_near_zero_clamps_start(self):
        stdout = _ffprobe_out([])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe("vid.mp4", 5.0)
        assert result == 0.0  # max(0, 5-30) = 0

    def test_skips_malformed_pts(self):
        stdout = _ffprobe_out(["packet,notafloat,K_", "packet,90.0,K_"])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe("vid.mp4", 100.0)
        assert result == 90.0

    def test_skips_short_lines(self):
        stdout = _ffprobe_out(["packet,K_", "packet,90.0,K_"])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe("vid.mp4", 100.0)
        assert result == 90.0


class TestSnapToKeyframeForward:
    def test_returns_first_keyframe_at_or_after_t(self):
        stdout = _ffprobe_out([
            "packet,99.0,K_",   # < t, skip
            "packet,100.5,K_",  # >= t, first match
            "packet,101.0,K_",
        ])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe_forward("vid.mp4", 100.0)
        assert result == 100.5

    def test_returns_t_when_no_keyframe_found(self):
        stdout = _ffprobe_out(["packet,95.0,_"])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe_forward("vid.mp4", 100.0)
        assert result == 100.0

    def test_skips_malformed_pts(self):
        stdout = _ffprobe_out(["packet,notafloat,K_", "packet,102.0,K_"])
        with mock.patch("subprocess.run", return_value=stdout):
            result = snap_to_keyframe_forward("vid.mp4", 100.0)
        assert result == 102.0
