"""
main(): CLI entry point — arg parsing, file/binary checks, dry-run vs full run.
"""
import unittest.mock as mock
from datetime import datetime

import pytest

from split_homevideo import main

_DT = datetime(1990, 1, 4, 17, 1)


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


def _base_patches(video_path, splits=None, duration=200.0):
    splits = splits or [(0.0, None, None)]
    dt = _DT
    return [
        mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin(exists=True)),
        mock.patch("split_homevideo.scan", return_value=[(0.0, dt), (10.0, None)]),
        mock.patch("split_homevideo.find_splits", return_value=splits),
        mock.patch("split_homevideo.refine_split", return_value=99.0),
        mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, dt)]),
        mock.patch("split_homevideo.get_duration", return_value=duration),
        mock.patch("split_homevideo.split_video"),
        mock.patch("split_homevideo.snap_to_keyframe", return_value=95.0),
        mock.patch("split_homevideo.snap_to_keyframe_forward", return_value=100.5),
    ]


class TestMainDryRun:
    def test_dry_run_does_not_call_split_video(self, tmp_path):
        video = tmp_path / "vid.mp4"
        video.touch()
        with mock.patch("sys.argv", ["prog", str(video), "--dry-run"]):
            with mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin()), \
                 mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
                 mock.patch("split_homevideo.find_splits", return_value=[(0.0, None, None)]), \
                 mock.patch("split_homevideo.refine_split", return_value=99.0), \
                 mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
                 mock.patch("split_homevideo.get_duration", return_value=200.0), \
                 mock.patch("split_homevideo.split_video") as m_sv, \
                 mock.patch("split_homevideo.snap_to_keyframe", return_value=95.0), \
                 mock.patch("split_homevideo.snap_to_keyframe_forward", return_value=100.5):
                main()
        m_sv.assert_not_called()

    def test_dry_run_with_splits_calls_snap(self, tmp_path):
        video = tmp_path / "vid.mp4"
        video.touch()
        two_splits = [(0.0, None, None), (100.0, 90.0, _DT)]
        with mock.patch("sys.argv", ["prog", str(video), "--dry-run"]), \
             mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin()), \
             mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_splits", return_value=two_splits), \
             mock.patch("split_homevideo.refine_split", return_value=99.0), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.split_video"), \
             mock.patch("split_homevideo.snap_to_keyframe", return_value=95.0) as m_kf, \
             mock.patch("split_homevideo.snap_to_keyframe_forward", return_value=100.5) as m_kff:
            main()
        m_kf.assert_called()
        m_kff.assert_called()


class TestMainFullRun:
    def test_calls_split_video(self, tmp_path):
        video = tmp_path / "vid.mp4"
        video.touch()
        with mock.patch("sys.argv", ["prog", str(video)]), \
             mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin()), \
             mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_splits", return_value=[(0.0, None, None)]), \
             mock.patch("split_homevideo.refine_split", return_value=99.0), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.split_video") as m_sv, \
             mock.patch("split_homevideo.snap_to_keyframe", return_value=95.0), \
             mock.patch("split_homevideo.snap_to_keyframe_forward", return_value=100.5):
            main()
        m_sv.assert_called_once()

    def test_uses_default_cache_path(self, tmp_path):
        video = tmp_path / "myvid.mp4"
        video.touch()
        captured = {}
        def fake_scan(v, interval, crop, cache_path=None):
            captured["cache"] = cache_path
            return [(0.0, _DT)]
        with mock.patch("sys.argv", ["prog", str(video)]), \
             mock.patch("split_homevideo.OCR_BIN", _mock_ocr_bin()), \
             mock.patch("split_homevideo.scan", side_effect=fake_scan), \
             mock.patch("split_homevideo.find_splits", return_value=[(0.0, None, None)]), \
             mock.patch("split_homevideo.refine_split", return_value=0.0), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.get_duration", return_value=100.0), \
             mock.patch("split_homevideo.split_video"), \
             mock.patch("split_homevideo.snap_to_keyframe", return_value=0.0), \
             mock.patch("split_homevideo.snap_to_keyframe_forward", return_value=0.0):
            main()
        assert captured["cache"].endswith("myvid_ocr_cache.json")
