"""
_ffmpeg_copy_seg() and _ffmpeg_encode_seg(): thin ffmpeg wrappers.
"""
import unittest.mock as mock

from split_homevideo import _ffmpeg_copy_seg, _ffmpeg_encode_seg


class TestFfmpegCopySeg:
    def test_calls_ffmpeg_stream_copy(self):
        with mock.patch("subprocess.run") as m:
            _ffmpeg_copy_seg("vid.mp4", 10.0, 50.0, "/out/seg.mp4")
        m.assert_called_once()
        cmd = m.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert "10.000" in cmd
        assert "40.000" in cmd  # duration = 50-10
        assert "-c" in cmd
        assert "copy" in cmd
        assert "/out/seg.mp4" in cmd

    def test_passes_check_true(self):
        with mock.patch("subprocess.run") as m:
            _ffmpeg_copy_seg("vid.mp4", 0.0, 10.0, "/out/seg.mp4")
        assert m.call_args[1].get("check") is True


class TestFfmpegEncodeSeg:
    def test_calls_ffmpeg_with_libx264(self):
        with mock.patch("subprocess.run") as m:
            _ffmpeg_encode_seg("vid.mp4", 10.0, 15.0, "/out/seg.mp4", crf=18)
        cmd = m.call_args[0][0]
        assert "libx264" in cmd
        assert "18" in cmd
        assert "aac" in cmd

    def test_no_b_frames(self):
        with mock.patch("subprocess.run") as m:
            _ffmpeg_encode_seg("vid.mp4", 10.0, 15.0, "/out/seg.mp4", crf=18)
        cmd = m.call_args[0][0]
        assert "-bf" in cmd
        bf_idx = cmd.index("-bf")
        assert cmd[bf_idx + 1] == "0"

    def test_duration_computed_correctly(self):
        with mock.patch("subprocess.run") as m:
            _ffmpeg_encode_seg("vid.mp4", 10.0, 13.5, "/out/seg.mp4", crf=18)
        cmd = m.call_args[0][0]
        assert "3.500" in cmd
