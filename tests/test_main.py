"""
main(): CLI entry point — arg parsing, file/binary checks, path construction,
and orchestrating run() + split_video(). Pipeline logic lives in test_run.py.
"""
import unittest.mock as mock
from datetime import datetime

import pytest

from split_homevideo import PipelineConfig, PipelineResult, main

_DT = datetime(1990, 1, 4, 17, 1)

_EMPTY_RESULT = PipelineResult(
    splits=[0.0],
    filtered=[(0.0, _DT)],
    boundary_map={},
    phase_times={"scan": 0.1, "visual": 0.0, "refine": 0.0},
)


def _mock_ocr_bin(exists=True):
    m = mock.Mock()
    m.exists.return_value = exists
    return m


class TestMainFileNotFound:
    def test_exits_when_input_missing(self):
        with mock.patch("sys.argv", ["prog", "/nonexistent/video.mp4"]):
            with pytest.raises(SystemExit):
                main()


class TestMainOcrBinMissing:
    def test_exits_when_ocr_binary_absent(self, tmp_path):
        video = tmp_path / "vid.mp4"
        video.touch()
        with mock.patch("sys.argv", ["prog", str(video)]), \
             mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin(exists=False)):
            with pytest.raises(SystemExit):
                main()


class TestMainDryRun:
    def test_does_not_call_split_video(self, tmp_path):
        video = tmp_path / "vid.mp4"
        video.touch()
        with mock.patch("sys.argv", ["prog", str(video), "--dry-run"]), \
             mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin()), \
             mock.patch("split_homevideo.run", return_value=_EMPTY_RESULT), \
             mock.patch("split_homevideo.split_video") as m_sv:
            main()
        m_sv.assert_not_called()


class TestMainFullRun:
    def test_calls_split_video(self, tmp_path):
        video = tmp_path / "vid.mp4"
        video.touch()
        with mock.patch("sys.argv", ["prog", str(video)]), \
             mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin()), \
             mock.patch("split_homevideo.run", return_value=_EMPTY_RESULT), \
             mock.patch("split_homevideo.split_video") as m_sv:
            main()
        m_sv.assert_called_once()

    def test_uses_default_cache_path(self, tmp_path):
        video = tmp_path / "myvid.mp4"
        video.touch()
        with mock.patch("sys.argv", ["prog", str(video)]), \
             mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin()), \
             mock.patch("split_homevideo.run", return_value=_EMPTY_RESULT) as m_run, \
             mock.patch("split_homevideo.split_video"):
            main()
        config: PipelineConfig = m_run.call_args[0][0]
        assert config.cache.endswith("myvid_ocr_cache.json")
