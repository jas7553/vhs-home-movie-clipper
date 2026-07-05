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

from split_homevideo import FRAMES_PER_SAMPLE, _fallback_window_frame_times, frame_index, scan

_CROP = "250:110:385:370"
_INTERVAL = 10


def _write_cache(path: str, interval=_INTERVAL, crop=_CROP, samples=None, fallback_done=None):
    samples = samples or []
    from split_homevideo import _CACHE_FORMAT, _VF_PREPROCESS, FRAMES_PER_SAMPLE
    payload = {
        "cache_format": _CACHE_FORMAT,
        "interval": interval,
        "crop": crop,
        "vf_preprocess": _VF_PREPROCESS,
        "frames_per_sample": FRAMES_PER_SAMPLE,
        "samples": samples,
    }
    # fallback_done omitted entirely by default, matching every cache written before
    # the checkpoint scheme existed — scan() must treat that as a complete cache
    # (see TestScanCheckpointCache).
    if fallback_done is not None:
        payload["fallback_done"] = fallback_done
    with open(path, "w") as f:
        json.dump(payload, f)


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
        # A bucket where all OCR fails → (t_last_frame, None). The targeted fallback
        # (phase 2) also fails to extract anything, so the bucket stays unsolved.
        frame_paths = self._make_frame_paths(tmp_path, FRAMES_PER_SAMPLE)
        # OCR returns nothing parseable
        ocr_map = {p: "garbage" for p in frame_paths}
        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.extract_frame", return_value=None), \
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


class TestScanPreprocessingFallback:
    """Phase-2 preprocessing path: unsolved buckets get a targeted seek-based fallback
    — extract_frame is called only for the unread windows' timestamps, instead of
    re-decoding the whole tape a second time. See _run_targeted_fallback."""

    def _make_frames(self, base_dir, count, subdir="."):
        import os
        d = os.path.join(str(base_dir), subdir)
        os.makedirs(d, exist_ok=True)
        paths = [os.path.join(d, f"frame_{i:06d}.bmp") for i in range(count)]
        for p in paths:
            open(p, "w").close()
        return paths

    def _fake_extract_frame(self, base_dir, subdir="p2"):
        """extract_frame replacement: writes a uniquely-named file per (t) and returns it,
        mimicking the real seek-based extraction without touching ffmpeg."""
        import os
        d = os.path.join(str(base_dir), subdir)
        os.makedirs(d, exist_ok=True)

        def fake(video, t, crop, tmpdir, preprocess=False):
            p = os.path.join(d, f"frame_{t:.3f}.bmp")
            open(p, "w").close()
            return p
        return fake

    def test_phase1_timeonly_reading_stored_and_returned(self, tmp_path):
        # Phase 1: all frames return time-only text ("8:00 PM") → no dated reading,
        # but text is parseable as time-only → goes into timeonly_map, not `unsolved`.
        # The fallback (phase 2) never runs for this bucket.
        frame_paths = self._make_frames(tmp_path, FRAMES_PER_SAMPLE)
        ocr_map = {p: "8:00 PM" for p in frame_paths}
        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            result = scan("fake.mp4", _INTERVAL, _CROP)
        # fill_timeonly_dates has no predecessor date → stays None
        assert result[0][1] is None

    def test_phase2_fallback_recovers_dated_reading(self, tmp_path):
        # Phase 1: garbage OCR → bucket unsolved.
        # Phase 2 (targeted seek): extract_frame is called for the unread window's own
        # timestamps; OCR on those frames returns dated text → the window is recovered.
        phase1_paths = self._make_frames(tmp_path, FRAMES_PER_SAMPLE, "p1")
        dated_ocr = "5:01 PM\n 1/ 4/90"

        def fake_ocr(paths):
            return {p: ("garbage" if "p1" in p else dated_ocr) for p in paths}

        with mock.patch("split_homevideo.extract_all_frames", return_value=phase1_paths), \
             mock.patch("split_homevideo.extract_frame", side_effect=self._fake_extract_frame(tmp_path)), \
             mock.patch("split_homevideo.ocr_batch", side_effect=fake_ocr):
            result = scan("fake.mp4", _INTERVAL, _CROP)
        assert result[0][1] == datetime(1990, 1, 4, 17, 1)

    def test_phase2_fallback_stores_timeonly_when_no_date(self, tmp_path):
        # Phase 1: garbage OCR → bucket unsolved.
        # Phase 2 (targeted seek): recovered frames read as time-only ("8:00 PM").
        # Result: fill_timeonly_dates has no predecessor → stays None.
        phase1_paths = self._make_frames(tmp_path, FRAMES_PER_SAMPLE, "p1")

        def fake_ocr(paths):
            return {p: ("garbage" if "p1" in p else "8:00 PM") for p in paths}

        with mock.patch("split_homevideo.extract_all_frames", return_value=phase1_paths), \
             mock.patch("split_homevideo.extract_frame", side_effect=self._fake_extract_frame(tmp_path)), \
             mock.patch("split_homevideo.ocr_batch", side_effect=fake_ocr):
            result = scan("fake.mp4", _INTERVAL, _CROP)
        assert result[0][1] is None

    def test_phase2_fallback_requests_only_unread_windows(self, tmp_path):
        # Two windows: bucket 0 unread by phase 1, bucket 1 read cleanly. The targeted
        # fallback must call extract_frame only for bucket 0's FRAMES_PER_SAMPLE
        # timestamps — never for bucket 1's, which phase 1 already solved.
        dated_ocr = "5:01 PM\n 1/ 4/90"
        phase1_paths = self._make_frames(tmp_path, 2 * FRAMES_PER_SAMPLE, "p1")
        # bucket 0 (indices 0..FRAMES_PER_SAMPLE-1) unread; bucket 1 solved.
        ocr_map = {
            p: ("garbage" if frame_index(p) < FRAMES_PER_SAMPLE else dated_ocr)
            for p in phase1_paths
        }
        requested_times: list[float] = []

        def fake_extract(video, t, crop, tmpdir, preprocess=False):
            requested_times.append(t)
            p = os.path.join(str(tmp_path), "p2", f"frame_{t:.3f}.bmp")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
            return p

        with mock.patch("split_homevideo.extract_all_frames", return_value=phase1_paths), \
             mock.patch("split_homevideo.extract_frame", side_effect=fake_extract), \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map):
            scan("fake.mp4", _INTERVAL, _CROP)

        # bucket 0's t_last is (FRAMES_PER_SAMPLE - 1) * INTERVAL / FRAMES_PER_SAMPLE;
        # its own frame times are exactly the ones the fallback should request — and
        # only those; bucket 1 (already solved by phase 1) must never be touched.
        expected_times = _fallback_window_frame_times(
            (FRAMES_PER_SAMPLE - 1) * _INTERVAL / FRAMES_PER_SAMPLE, _INTERVAL)
        assert len(requested_times) == FRAMES_PER_SAMPLE
        assert sorted(requested_times) == pytest.approx(sorted(expected_times))

    def test_fallback_chunks_bound_transient_disk(self, tmp_path):
        # More windows than the chunk size: every window still gets its frames extracted
        # and voted, and each chunk's frame dir is deleted before the next chunk runs —
        # the property that caps transient disk at one chunk's worth (the preprocessed
        # frames are ~3.5MB bgr24 BMPs, so an unchunked pass over thousands of unread
        # windows would blow the disk budget).
        from split_homevideo import _run_targeted_fallback

        n_windows = 5
        window_ends = [float((k + 1) * _INTERVAL) for k in range(n_windows)]
        fb_dir = tmp_path / "fb"
        fb_dir.mkdir()
        max_live = [0]

        def fake_extract(video, t, crop, tmpdir, preprocess=False):
            assert preprocess is True  # fallback must keep the preprocessing chain
            p = os.path.join(tmpdir, f"frame_{t:.3f}.bmp")
            open(p, "w").close()
            live = sum(len(files) for _, _, files in os.walk(str(fb_dir)))
            max_live[0] = max(max_live[0], live)
            return p

        with mock.patch("split_homevideo._FALLBACK_CHUNK_WINDOWS", 2), \
             mock.patch("split_homevideo.extract_frame", side_effect=fake_extract), \
             mock.patch("split_homevideo.ocr_batch",
                        side_effect=lambda paths: {p: "5:01 PM\n 1/ 4/90" for p in paths}):
            result = _run_targeted_fallback(
                "fake.mp4", _CROP, _INTERVAL, window_ends, str(fb_dir), workers=2)

        # Every window resolved by the fallback.
        assert set(result) == set(window_ends)
        assert all(dt is not None for dt, _ in result.values())
        # Never more than one chunk's frames alive at once (2 windows x FRAMES_PER_SAMPLE).
        assert max_live[0] <= 2 * FRAMES_PER_SAMPLE
        # All chunk dirs cleaned up.
        assert list(fb_dir.iterdir()) == []


class TestScanCheckpointCache:
    """Cache checkpoint semantics: the crop-only pass (phase 1) is written to
    cache_path with fallback_done=False BEFORE the fallback pass (phase 2) runs, so a
    crash during phase 2 never loses phase 1's (expensive, whole-tape) OCR work. Loading
    a fallback_done=False cache is not a hit: scan() resumes phase 2 over just the still-
    unread windows and rewrites the cache complete. A cache with no fallback_done key at
    all (every cache from before this change) is a complete scan, same as always."""

    def test_checkpoint_written_before_fallback_then_finalized(self, tmp_path):
        # One unresolved bucket → both a pre-fallback checkpoint (False) and a final
        # complete write (True) must happen, in that order.
        import split_homevideo
        cache_path = str(tmp_path / "cache.json")
        frame_paths = [str(tmp_path / f"frame_{i:06d}.bmp") for i in range(FRAMES_PER_SAMPLE)]
        for p in frame_paths:
            open(p, "w").close()

        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.extract_frame", return_value=None), \
             mock.patch("split_homevideo.ocr_batch", return_value={p: "garbage" for p in frame_paths}), \
             mock.patch("split_homevideo._write_scan_cache",
                        wraps=split_homevideo._write_scan_cache) as m:
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)

        fallback_done_calls = [c.kwargs["fallback_done"] for c in m.call_args_list]
        assert fallback_done_calls == [False, True]
        data = json.loads(open(cache_path).read())
        assert data["fallback_done"] is True  # final on-disk state is always complete

    def test_no_checkpoint_written_when_nothing_unsolved(self, tmp_path):
        # Every bucket resolved by phase 1 → phase 2 never runs → a single complete write.
        import split_homevideo
        cache_path = str(tmp_path / "cache.json")
        frame_paths = [str(tmp_path / f"frame_{i:06d}.bmp") for i in range(FRAMES_PER_SAMPLE)]
        for p in frame_paths:
            open(p, "w").close()
        ocr_map = {p: "5:01 PM\n 1/ 4/90" for p in frame_paths}

        with mock.patch("split_homevideo.extract_all_frames", return_value=frame_paths), \
             mock.patch("split_homevideo.extract_frame") as exf, \
             mock.patch("split_homevideo.ocr_batch", return_value=ocr_map), \
             mock.patch("split_homevideo._write_scan_cache",
                        wraps=split_homevideo._write_scan_cache) as m:
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)

        exf.assert_not_called()
        assert [c.kwargs["fallback_done"] for c in m.call_args_list] == [True]

    def test_resume_reads_only_unsolved_windows_and_skips_phase1(self, tmp_path):
        # Simulate a checkpoint left behind by a crashed fallback pass: bucket 0 still
        # unresolved (null text), bucket 1 already dated by phase 1.
        cache_path = str(tmp_path / "cache.json")
        t_bucket0 = (FRAMES_PER_SAMPLE - 1) * _INTERVAL / FRAMES_PER_SAMPLE
        t_bucket1 = (2 * FRAMES_PER_SAMPLE - 1) * _INTERVAL / FRAMES_PER_SAMPLE
        _write_cache(cache_path, samples=[
            (t_bucket0, None),
            (t_bucket1, "5:01 PM\n 1/ 4/90"),
        ], fallback_done=False)

        requested: list[float] = []

        def fake_extract(video, t, crop, tmpdir, preprocess=False):
            requested.append(t)
            p = str(tmp_path / f"resumed_frame_{t:.3f}.bmp")
            open(p, "w").close()
            return p

        with mock.patch("split_homevideo.extract_all_frames") as ext, \
             mock.patch("split_homevideo.extract_frame", side_effect=fake_extract), \
             mock.patch("split_homevideo.ocr_batch",
                        return_value={}) as ocr:  # still unreadable after fallback
            result = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)

        ext.assert_not_called()  # phase 1 (whole-tape decode) must never re-run on resume
        assert ocr.called  # the fallback still runs its OCR pass
        expected = sorted(_fallback_window_frame_times(t_bucket0, _INTERVAL))
        assert sorted(requested) == pytest.approx(expected)  # only bucket 0's frames

        # bucket 1's already-dated reading survives untouched.
        assert result[1] == (t_bucket1, datetime(1990, 1, 4, 17, 1))

        # cache is rewritten complete so a third run is a plain hit.
        data = json.loads(open(cache_path).read())
        assert data["fallback_done"] is True
        with mock.patch("split_homevideo.extract_all_frames") as ext2, \
             mock.patch("split_homevideo.extract_frame") as exf2:
            scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext2.assert_not_called()
        exf2.assert_not_called()

    def test_resume_recovers_dated_reading_for_unsolved_window(self, tmp_path):
        # Same checkpoint shape, but this time the resumed fallback pass succeeds.
        cache_path = str(tmp_path / "cache.json")
        t_bucket0 = (FRAMES_PER_SAMPLE - 1) * _INTERVAL / FRAMES_PER_SAMPLE
        _write_cache(cache_path, samples=[(t_bucket0, None)], fallback_done=False)

        def fake_extract(video, t, crop, tmpdir, preprocess=False):
            p = str(tmp_path / f"resumed_frame_{t:.3f}.bmp")
            open(p, "w").close()
            return p

        dated_ocr = "5:01 PM\n 1/ 4/90"
        with mock.patch("split_homevideo.extract_all_frames") as ext, \
             mock.patch("split_homevideo.extract_frame", side_effect=fake_extract), \
             mock.patch("split_homevideo.ocr_batch", side_effect=lambda paths: {p: dated_ocr for p in paths}):
            result = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)

        ext.assert_not_called()
        assert result[0] == (t_bucket0, datetime(1990, 1, 4, 17, 1))

    def test_cache_without_fallback_done_key_is_complete(self, tmp_path):
        # No marker at all (every cache from before the checkpoint scheme existed):
        # treated as complete, not
        # a checkpoint needing resume — existing caches for other tapes must not break.
        cache_path = str(tmp_path / "cache.json")
        _write_cache(cache_path, samples=[(0.0, "5:01 PM 1/ 4/90")])  # fallback_done omitted
        with mock.patch("split_homevideo.extract_all_frames") as ext, \
             mock.patch("split_homevideo.extract_frame") as exf:
            result = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext.assert_not_called()
        exf.assert_not_called()
        assert result[0] == (0.0, datetime(1990, 1, 4, 17, 1))

    def test_cache_with_explicit_fallback_done_true_is_complete(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        _write_cache(cache_path, samples=[(0.0, "5:01 PM 1/ 4/90")], fallback_done=True)
        with mock.patch("split_homevideo.extract_all_frames") as ext, \
             mock.patch("split_homevideo.extract_frame") as exf:
            result = scan("fake.mp4", _INTERVAL, _CROP, cache_path=cache_path)
        ext.assert_not_called()
        exf.assert_not_called()
        assert result[0] == (0.0, datetime(1990, 1, 4, 17, 1))
