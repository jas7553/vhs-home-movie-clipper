"""
extract_frame(), extract_all_frames(), and frame_index().
"""
import unittest.mock as mock

import pytest

from split_homevideo import extract_all_frames, extract_frame, frame_index


class TestExtractFrame:
    def test_returns_path_on_success(self, tmp_path):
        frame_path = str(tmp_path / "frame_1.000.bmp")
        proc = mock.Mock(returncode=0)
        with mock.patch("subprocess.run", return_value=proc) as m:
            # create the file so the existence check passes
            open(frame_path, "w").close()
            result = extract_frame("vid.mp4", 1.0, "250:110:385:370", str(tmp_path))
        assert result == frame_path
        m.assert_called_once()
        cmd = m.call_args[0][0]
        assert "ffmpeg" in cmd[0]
        assert "1.0" in cmd

    def test_returns_none_on_nonzero_returncode(self, tmp_path):
        proc = mock.Mock(returncode=1)
        with mock.patch("subprocess.run", return_value=proc):
            result = extract_frame("vid.mp4", 5.0, "250:110:385:370", str(tmp_path))
        assert result is None

    def test_returns_none_when_file_not_created(self, tmp_path):
        proc = mock.Mock(returncode=0)
        with mock.patch("subprocess.run", return_value=proc):
            # no file created → existence check fails
            result = extract_frame("vid.mp4", 5.0, "250:110:385:370", str(tmp_path))
        assert result is None


class TestExtractAllFrames:
    def test_calls_ffmpeg_and_returns_sorted_paths(self, tmp_path):
        bmp_a = tmp_path / "frame_000000.bmp"
        bmp_b = tmp_path / "frame_000001.bmp"
        bmp_a.touch()
        bmp_b.touch()
        with mock.patch("subprocess.run") as m:
            result = extract_all_frames("vid.mp4", 10, "250:110:385:370", str(tmp_path))
        m.assert_called_once()
        cmd = m.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert "fps=3/10" in " ".join(cmd)
        assert result == sorted([str(bmp_a), str(bmp_b)])

    def test_preprocess_flag_appends_vf_preprocess(self, tmp_path):
        from split_homevideo import _VF_PREPROCESS
        with mock.patch("subprocess.run") as m:
            extract_all_frames("vid.mp4", 10, "250:110:385:370", str(tmp_path), preprocess=True)
        cmd = m.call_args[0][0]
        vf_arg = cmd[cmd.index("-vf") + 1]
        assert _VF_PREPROCESS in vf_arg


class TestFrameIndex:
    def test_parses_six_digit_index(self):
        assert frame_index("/tmp/frame_000042.bmp") == 42

    def test_parses_zero(self):
        assert frame_index("/some/dir/frame_000000.bmp") == 0

    def test_asserts_on_unexpected_name(self):
        with pytest.raises(AssertionError):
            frame_index("/tmp/notaframe.bmp")
