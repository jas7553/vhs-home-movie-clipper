"""
verify_date_islands() — dense re-OCR around each date island so a genuine short
session (one coarse reading at the sampling cadence) survives drop_date_islands,
while a true single-frame misread still gets dropped.

The function makes no keep/drop decision itself: it only inserts probe readings
whose date matches the island or one of its two dated neighbours. The existing
filter chain remains the arbiter.
"""
import json
from datetime import datetime

from split_homevideo import (
    ISLAND_PROBE_STEP_S,
    drop_date_islands,
    scan,
    verify_date_islands,
)

_INTERVAL = 10


def _raw(*items):
    """items: (t, 'M/ D/YY text' | None)."""
    from split_homevideo import parse_timestamp
    return [(float(t), parse_timestamp(txt) if txt else None, txt) for t, txt in items]


def _dates(samples):
    return [dt.date().isoformat() for *_, dt in [(s[0], s[1]) for s in samples] if dt]


A = "10:00 AM 1/ 7/90"
D = "8:02 PM 1/13/90"
B = "9:00 AM 1/14/90"


def _probe_from(table):
    """ProbeFn returning table[t] (default '') and recording requested times."""
    calls = []

    def probe(times):
        calls.append(list(times))
        return {t: table.get(t, "") for t in times}
    probe.calls = calls
    return probe


class TestRealShortSessionSurvives:
    def test_supported_island_becomes_run(self):
        raw = _raw((10, A), (20, A), (30, D), (40, B), (50, B))
        # Dense probes read the island date on 3 consecutive seconds around t=30.
        probe = _probe_from({27.0: D, 28.0: D, 29.0: D, 31.0: B})
        out = verify_date_islands(raw, _INTERVAL, probe)
        kept = drop_date_islands([(t, dt) for t, dt, _ in out])
        assert "1990-01-13" in _dates(kept)
        assert sum(d == "1990-01-13" for d in _dates(kept)) == 4

    def test_probe_window_spans_plus_minus_interval(self):
        raw = _raw((10, A), (20, A), (30, D), (40, B), (50, B))
        probe = _probe_from({})
        verify_date_islands(raw, _INTERVAL, probe)
        want = probe.calls[0]
        assert min(want) == 30 - _INTERVAL + ISLAND_PROBE_STEP_S
        assert max(want) == 30 + _INTERVAL - ISLAND_PROBE_STEP_S
        assert 30.0 not in want  # the island itself is not re-probed
        assert 20.0 not in want and 40.0 not in want  # coarse windows are not re-probed


class TestMisreadStillDropped:
    def test_unsupported_island_left_for_island_filter(self):
        raw = _raw((10, A), (20, A), (30, D), (40, A), (50, A))
        probe = _probe_from({29.0: A, 31.0: A})
        out = verify_date_islands(raw, _INTERVAL, probe)
        kept = drop_date_islands([(t, dt) for t, dt, _ in out])
        assert "1990-01-13" not in _dates(kept)
        # neighbour-date probes were inserted (they are consistent with the run);
        # unreadable probes are recorded as None entries
        assert sum(1 for _, dt, _ in out if dt is not None) == 7
        assert sum(1 for _, dt, _ in out if dt is None) == 16

    def test_scattered_single_hits_are_still_islands(self):
        # D reappears on non-adjacent probes only: each stays isolated -> still dropped.
        raw = _raw((10, A), (20, A), (30, D), (40, A), (50, A))
        probe = _probe_from({25.0: D, 26.0: A, 27.0: A, 31.0: A, 35.0: D, 36.0: A})
        out = verify_date_islands(raw, _INTERVAL, probe)
        kept = drop_date_islands([(t, dt) for t, dt, _ in out])
        assert "1990-01-13" not in _dates(kept)

    def test_third_date_probes_are_discarded(self):
        raw = _raw((10, A), (20, A), (30, D), (40, B), (50, B))
        stray = "8:02 PM 1/18/90"
        probe = _probe_from({28.0: stray, 29.0: stray})
        out = verify_date_islands(raw, _INTERVAL, probe)
        assert "1990-01-18" not in _dates([(t, dt) for t, dt, _ in out])


class TestContiguityRule:
    def test_two_adjacent_hits_not_enough(self):
        # island + 2 probes = 3 D reads, below ISLAND_MIN_PROBE_HITS probes -> dropped
        # (a legible B probe 2s after also rules out the dead-zone exception)
        raw = _raw((10, A), (20, A), (30, D), (40, B), (50, B))
        probe = _probe_from({28.0: D, 29.0: D, 32.0: B})
        out = verify_date_islands(raw, _INTERVAL, probe)
        kept = drop_date_islands([(t, dt) for t, dt, _ in out])
        assert "1990-01-13" not in _dates(kept)

    def test_interleaved_hits_not_contiguous(self):
        # 3 D probes, but an A read sits between them and the island: sporadic misread.
        raw = _raw((10, A), (20, A), (30, D), (40, A), (50, A))
        probe = _probe_from({24.0: D, 25.0: D, 26.0: D, 27.0: A, 31.0: A})
        out = verify_date_islands(raw, _INTERVAL, probe)
        kept = drop_date_islands([(t, dt) for t, dt, _ in out])
        assert "1990-01-13" not in _dates(kept)
        # and the stray D probes were not inserted either (they would form their own run)
        assert all(dt is None or dt.date().day != 13 for t, dt, _ in out if t != 30.0)

    def test_time_only_probes_do_not_break_contiguity(self):
        raw = _raw((10, A), (20, A), (30, D), (40, B), (50, B))
        probe = _probe_from({26.0: D, 27.0: "8:02 PM", 28.0: D, 29.0: "", 31.0: D})
        out = verify_date_islands(raw, _INTERVAL, probe)
        kept = drop_date_islands([(t, dt) for t, dt, _ in out])
        assert sum(d == "1990-01-13" for d in _dates(kept)) == 4


class TestNoIslands:
    def test_no_islands_no_probe_calls(self):
        raw = _raw((10, A), (20, A), (30, B), (40, B))
        probe = _probe_from({})
        assert verify_date_islands(raw, _INTERVAL, probe) == raw
        assert probe.calls == []


class TestCaching:
    def test_probes_cached_and_not_reprobed(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"cache_format": 5, "samples": []}))
        raw = _raw((10, A), (20, A), (30, D), (40, B), (50, B))
        probe = _probe_from({27.0: D, 28.0: D, 29.0: D})
        out1 = verify_date_islands(raw, _INTERVAL, probe, str(cache))
        assert len(probe.calls) == 1
        assert "island_probes_v2" in json.loads(cache.read_text())
        probe2 = _probe_from({})
        out2 = verify_date_islands(raw, _INTERVAL, probe2, str(cache))
        assert probe2.calls == []
        assert out1 == out2


class TestScanWrapper:
    def test_scan_without_probe_fn_skips_verification(self, tmp_path):
        import unittest.mock as mock
        raw = _raw((10, A), (20, A), (30, D), (40, B))
        with mock.patch("split_homevideo.scan_raw", return_value=raw):
            out = scan("v.mp4", _INTERVAL, "c")
        assert [t for t, _ in out] == [10.0, 20.0, 30.0, 40.0]

    def test_scan_fills_timeonly_after_verification(self, tmp_path):
        import unittest.mock as mock
        # time-only read right after the short session must inherit D, not A
        raw = _raw((10, A), (20, A), (30, D), (40, "8:03 PM"), (50, B), (60, B))
        probe = _probe_from({27.0: D, 28.0: D, 29.0: D})
        with mock.patch("split_homevideo.scan_raw", return_value=raw):
            out = scan("v.mp4", _INTERVAL, "c", probe_fn=probe)
        by_t = dict(out)
        assert by_t[40.0].date() == datetime(1990, 1, 13).date()
