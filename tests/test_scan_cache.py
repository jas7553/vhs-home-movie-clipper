"""
scan() cache hit/miss/save behaviour.

The cache is a JSON file with keys: interval, crop, samples.
A hit requires both interval AND crop to match; either mismatch triggers a full re-scan.
"""
import json
import os
import unittest.mock as mock
from datetime import datetime

from split_homevideo import scan

_CROP = "250:110:385:370"
_INTERVAL = 10


def _write_cache(path: str, interval=_INTERVAL, crop=_CROP, samples=None):
    samples = samples or []
    with open(path, "w") as f:
        from split_homevideo import _VF_PREPROCESS, FRAMES_PER_SAMPLE
        json.dump({
            "interval": interval,
            "crop": crop,
            "vf_preprocess": _VF_PREPROCESS,
            "frames_per_sample": FRAMES_PER_SAMPLE,
            "samples": samples
        }, f)


class TestCacheHit:
    def test_returns_cached_data(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        _write_cache(cache_path, samples=[(0.0, "1990-01-04T17:01:00"), (10.0, None)])
        with mock.patch("split_homevideo.extract_all_frames") as ext:
            result = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext.assert_not_called()
        assert result[0] == (0.0, datetime(1990, 1, 4, 17, 1))
        assert result[1] == (10.0, None)

    def test_none_samples_preserved(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        _write_cache(cache_path, samples=[(0.0, None), (10.0, None)])
        with mock.patch("split_homevideo.extract_all_frames"):
            result = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        assert all(dt is None for _, dt in result)


class TestCacheMiss:
    def test_interval_mismatch_triggers_rescan(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        _write_cache(cache_path, interval=5)  # stale: different interval
        with mock.patch("split_homevideo.extract_all_frames", return_value=[]) as ext, \
             mock.patch("split_homevideo.ocr_batch", return_value={}):
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext.assert_called_once()

    def test_crop_mismatch_triggers_rescan(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        _write_cache(cache_path, crop="100:100:0:0")  # stale: different crop
        with mock.patch("split_homevideo.extract_all_frames", return_value=[]) as ext, \
             mock.patch("split_homevideo.ocr_batch", return_value={}):
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext.assert_called_once()

    def test_missing_cache_triggers_rescan(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")  # does not exist
        with mock.patch("split_homevideo.extract_all_frames", return_value=[]) as ext, \
             mock.patch("split_homevideo.ocr_batch", return_value={}):
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext.assert_called_once()


class TestCacheSave:
    def test_cache_written_after_scan(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        with mock.patch("split_homevideo.extract_all_frames", return_value=[]), \
             mock.patch("split_homevideo.ocr_batch", return_value={}):
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        assert os.path.exists(cache_path)
        data = json.loads(open(cache_path).read())
        assert data["interval"] == _INTERVAL
        assert data["crop"] == _CROP
        assert data["samples"] == []

    def test_no_cache_path_writes_nothing(self, tmp_path):
        with mock.patch("split_homevideo.extract_all_frames", return_value=[]), \
             mock.patch("split_homevideo.ocr_batch", return_value={}):
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=None)
        assert list(tmp_path.iterdir()) == []
