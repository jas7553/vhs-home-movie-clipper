"""
scan() cache hit/miss/save behaviour.

The cache is a JSON file with keys: interval, crop, samples.
A hit requires both interval AND crop to match; either mismatch triggers a full re-scan.
t values in samples are t_last_frame (actual video time of the last extracted frame in
the bucket), not the bucket-start label.
"""
import json
import os
import unittest.mock as mock
from datetime import datetime

import pytest

from split_homevideo import FRAMES_PER_SAMPLE, scan

_CROP = "250:110:385:370"
_INTERVAL = 10


def _write_cache(path: str, interval=_INTERVAL, crop=_CROP, samples=None):
    samples = samples or []
    with open(path, "w") as f:
        from split_homevideo import _CACHE_FORMAT, _VF_PREPROCESS, FRAMES_PER_SAMPLE
        json.dump({
            "cache_format": _CACHE_FORMAT,
            "interval": interval,
            "crop": crop,
            "vf_preprocess": _VF_PREPROCESS,
            "frames_per_sample": FRAMES_PER_SAMPLE,
            "samples": samples
        }, f)


class TestCacheHit:
    def test_returns_cached_data(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        _write_cache(cache_path, samples=[(0.0, "5:01 PM 1/ 4/90"), (10.0, None)])
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

    def test_old_cache_format_triggers_rescan(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        from split_homevideo import _CACHE_FORMAT, _VF_PREPROCESS
        with open(cache_path, "w") as f:
            json.dump({
                "cache_format": _CACHE_FORMAT - 1,  # old format
                "interval": _INTERVAL,
                "crop": _CROP,
                "vf_preprocess": _VF_PREPROCESS,
                "frames_per_sample": FRAMES_PER_SAMPLE,
                "samples": [],
            }, f)
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

    def test_cache_stores_t_last_frame_not_bucket_start(self, tmp_path):
        # Bucket 0: frame indices 0,1,2 → times 0, 3.33, 6.67; last = 6.67
        # Bucket 1: frame indices 3,4,5 → times 10, 13.33, 16.67; last = 16.67
        cache_path = str(tmp_path / "cache.json")
        frame_paths = [
            str(tmp_path / f"frame_{i:06d}.bmp")
            for i in range(6)
        ]
        for p in frame_paths:
            open(p, "w").close()
        ocr_map = {p: "5:01 PM\n 1/ 4/90" for p in frame_paths}
        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        data = json.loads(open(cache_path).read())
        t_values = [t for t, _ in data["samples"]]
        t_last_bucket0 = 2 * _INTERVAL / FRAMES_PER_SAMPLE   # index 2 → 6.666...
        t_last_bucket1 = 5 * _INTERVAL / FRAMES_PER_SAMPLE   # index 5 → 16.666...
        assert t_values == [pytest.approx(t_last_bucket0), pytest.approx(t_last_bucket1)]


class TestScanReturnsTLastFrame:
    def _make_frame_paths(self, tmp_path, count):
        paths = [str(tmp_path / f"frame_{i:06d}.bmp") for i in range(count)]
        for p in paths:
            open(p, "w").close()
        return paths

    def test_t_is_last_frame_time_not_bucket_start(self, tmp_path):
        # 2 full buckets of FRAMES_PER_SAMPLE frames each.
        # scan() must return t_last_frame for each bucket, not bucket-start label.
        frame_paths = self._make_frame_paths(tmp_path, 2 * FRAMES_PER_SAMPLE)
        ocr_map = {p: "5:01 PM\n 1/ 4/90" for p in frame_paths}
        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            result = scan("fake.mp4", _INTERVAL, _CROP)
        t0, _ = result[0]
        t1, _ = result[1]
        # Last frame of bucket 0: index (FPS-1), time = (FPS-1)*interval/FPS
        expected_t0 = (FRAMES_PER_SAMPLE - 1) * _INTERVAL / FRAMES_PER_SAMPLE
        # Last frame of bucket 1: index (2*FPS-1), time = (2*FPS-1)*interval/FPS
        expected_t1 = (2 * FRAMES_PER_SAMPLE - 1) * _INTERVAL / FRAMES_PER_SAMPLE
        assert t0 == pytest.approx(expected_t0)
        assert t1 == pytest.approx(expected_t1)

    def test_none_bucket_still_uses_last_frame_time(self, tmp_path):
        # A bucket where all OCR fails → (t_last_frame, None)
        frame_paths = self._make_frame_paths(tmp_path, FRAMES_PER_SAMPLE)
        # OCR returns nothing parseable
        ocr_map = {p: "garbage" for p in frame_paths}
        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            result = scan("fake.mp4", _INTERVAL, _CROP)
        t0, dt0 = result[0]
        assert dt0 is None
        expected_t0 = (FRAMES_PER_SAMPLE - 1) * _INTERVAL / FRAMES_PER_SAMPLE
        assert t0 == pytest.approx(expected_t0)

    def test_cache_round_trips_t_last_frame(self, tmp_path):
        # Write cache with t_last_frame values; reload must return same t values.
        cache_path = str(tmp_path / "cache.json")
        frame_paths = self._make_frame_paths(tmp_path, FRAMES_PER_SAMPLE)
        ocr_map = {p: "5:01 PM\n 1/ 4/90" for p in frame_paths}
        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            result1 = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        with mock.patch("split_homevideo.extract_all_frames") as ext:
            result2 = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext.assert_not_called()
        assert result1[0][0] == pytest.approx(result2[0][0])
