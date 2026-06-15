"""
get_duration() wraps ffprobe; parses float from stdout.
"""
import unittest.mock as mock

from split_homevideo import get_duration


class TestGetDuration:
    def test_parses_ffprobe_stdout(self):
        proc = mock.Mock(stdout="21345.67\n")
        with mock.patch("subprocess.run", return_value=proc) as m:
            result = get_duration("vid.mp4")
        assert result == pytest.approx(21345.67)
        cmd = m.call_args[0][0]
        assert "ffprobe" in cmd[0]
        assert "vid.mp4" in cmd

    def test_strips_whitespace(self):
        proc = mock.Mock(stdout="  100.0  \n")
        with mock.patch("subprocess.run", return_value=proc):
            result = get_duration("vid.mp4")
        assert result == 100.0


import pytest
