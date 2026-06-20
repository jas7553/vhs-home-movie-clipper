"""
_resolve_vision_cut(), _readings_for_window(), _vision_frame_name(),
_extract_frame_png(), vision_read_frame(), _refine_split_vision():
Vision-refine prototype path.
"""
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from split_homevideo import (
    _extract_frame_png,
    _readings_for_window,
    _resolve_vision_cut,
    _vision_frame_name,
    refine_split,
    vision_read_frame,
)

_PREV_T = 100.0
_PREV_DT = datetime(1990, 1, 4, 17, 0, 0)
_COARSE_T = 150.0
_GAP_S = 300
_WINDOW = list(range(101, 150))  # [101..149]


def _old(offset_s: int = 0) -> datetime:
    """Camera reading in the OLD session (small advance, no jump)."""
    return _PREV_DT + timedelta(seconds=offset_s + 5)


def _new() -> datetime:
    """Camera reading in the NEW session (large jump past gap_s)."""
    return _PREV_DT + timedelta(seconds=_GAP_S + 500)


class TestResolveVisionCut:
    def _resolve(self, readings: dict) -> tuple[float, str]:
        return _resolve_vision_cut(_WINDOW, readings, _COARSE_T, _PREV_T, _PREV_DT, _GAP_S)

    def test_all_none_no_signal_falls_back_to_coarse(self):
        t, method = self._resolve({})
        assert t == _COARSE_T
        assert method == "coarse"

    def test_all_noise_anchors_to_last_noise_plus_one(self):
        readings = {120: "NOISE", 130: "NOISE", 140: "NOISE"}
        t, method = self._resolve(readings)
        assert t == 141.0
        assert method == "vision"

    def test_old_session_frames_anchor_to_last_old_plus_one(self):
        readings = {110: _old(10), 120: _old(20), 130: _old(30)}
        t, method = self._resolve(readings)
        assert t == 131.0
        assert method == "vision"

    def test_confirmed_jump_cuts_at_last_old(self):
        # 130 = last old, 140 and 145 both jump → confirmed session change
        readings = {110: _old(10), 130: _old(30), 140: _new(), 145: _new()}
        t, method = self._resolve(readings)
        assert t == 131.0
        assert method == "vision"

    def test_lone_outlier_jump_not_confirmed_ignored(self):
        # 140 jumps but 145 does NOT → lone outlier, not a real boundary
        readings = {110: _old(10), 130: _old(30), 140: _new(), 145: _old(45)}
        t, method = self._resolve(readings)
        assert t == 146.0  # last_old_t updated to 145 after the outlier is skipped
        assert method == "vision"

    def test_jump_with_no_next_frame_triggers_cut(self):
        # Only one classified frame and it's a jump → confirmed (no next to contradict)
        readings = {140: _new()}
        t, method = self._resolve(readings)
        assert t == _PREV_T + 1.0  # last_old_t stays at prev_t
        assert method == "vision"

    def test_noise_then_old_session_anchors_correctly(self):
        readings = {110: "NOISE", 120: "NOISE", 130: _old(30)}
        t, method = self._resolve(readings)
        assert t == 131.0
        assert method == "vision"

    def test_none_readings_are_skipped(self):
        readings = {110: None, 120: None, 130: _old(30)}
        t, method = self._resolve(readings)
        assert t == 131.0
        assert method == "vision"


class TestReadingsForWindow:
    _COARSE = 1000.0
    _WINDOW = [1001, 1002, 1003, 1004, 1005]

    def _key(self, t: int) -> str:
        return _vision_frame_name(self._COARSE, t)

    def test_noise_keyword_maps_to_noise_string(self):
        readings_map = {self._key(1001): "NOISE"}
        out = _readings_for_window(self._COARSE, self._WINDOW, readings_map)
        assert out[1001] == "NOISE"

    def test_none_keyword_maps_to_none(self):
        readings_map = {self._key(1002): "None"}
        out = _readings_for_window(self._COARSE, self._WINDOW, readings_map)
        assert out[1002] is None

    def test_empty_string_maps_to_none(self):
        readings_map = {self._key(1003): ""}
        out = _readings_for_window(self._COARSE, self._WINDOW, readings_map)
        assert out[1003] is None

    def test_valid_timestamp_parses_to_datetime(self):
        readings_map = {self._key(1004): "5:00 PM\n 1/ 4/90"}
        out = _readings_for_window(self._COARSE, self._WINDOW, readings_map)
        assert isinstance(out[1004], datetime)

    def test_unparseable_timestamp_maps_to_none(self):
        readings_map = {self._key(1005): "garbage text xyz"}
        out = _readings_for_window(self._COARSE, self._WINDOW, readings_map)
        assert out[1005] is None

    def test_missing_key_absent_from_output(self):
        out = _readings_for_window(self._COARSE, self._WINDOW, {})
        assert 1001 not in out

    def test_case_insensitive_noise(self):
        readings_map = {self._key(1001): "noise"}
        out = _readings_for_window(self._COARSE, self._WINDOW, readings_map)
        assert out[1001] == "NOISE"


class TestVisionFrameName:
    def test_format_zero_padded(self):
        assert _vision_frame_name(1000.0, 1005) == "b001000_t001005.png"

    def test_large_values(self):
        name = _vision_frame_name(99999.0, 100000)
        assert name.endswith(".png")
        assert "b099999" in name
        assert "t100000" in name


class TestRefineSplitVisionReadingsPath:
    def test_vision_readings_path_routes_through_resolve(self):
        # Build a readings dict where coarse_t=150, window=[101..149]
        coarse_t = 150.0
        prev_t = 100.0
        prev_dt = _PREV_DT
        # Frame 130 = old session (no jump), frame 140 = new session (jump), 145 = new (confirmed)
        readings_map = {
            _vision_frame_name(coarse_t, 130): "5:01 PM\n 1/ 4/90",   # old session, no jump
            _vision_frame_name(coarse_t, 140): "6:00 PM\n 1/ 4/90",   # large jump → new session
            _vision_frame_name(coarse_t, 145): "6:05 PM\n 1/ 4/90",   # also jumps → confirmed
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            t, method, _ = refine_split(
                "vid.mp4", coarse_t, prev_t, prev_dt, _GAP_S, "250:110:385:370", tmpdir,
                vision_readings=readings_map,
            )
        assert method == "vision"
        assert t == pytest.approx(131.0)


class TestExtractFramePng:
    def test_success_returns_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "frame.png")
            mock_result = MagicMock()
            mock_result.returncode = 0
            with patch("subprocess.run", return_value=mock_result), \
                 patch("os.path.exists", return_value=True):
                result = _extract_frame_png("vid.mp4", 10.0, "250:110:385:370", out_path)
        assert result == out_path

    def test_nonzero_returncode_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "frame.png")
            mock_result = MagicMock()
            mock_result.returncode = 1
            with patch("subprocess.run", return_value=mock_result):
                result = _extract_frame_png("vid.mp4", 10.0, "250:110:385:370", out_path)
        assert result is None

    def test_missing_output_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "frame.png")
            mock_result = MagicMock()
            mock_result.returncode = 0
            with patch("subprocess.run", return_value=mock_result), \
                 patch("os.path.exists", return_value=False):
                result = _extract_frame_png("vid.mp4", 10.0, "250:110:385:370", out_path)
        assert result is None


class TestVisionReadFrame:
    def _mock_resp(self, text: str, in_tok: int = 100, out_tok: int = 5):
        block = MagicMock()
        block.type = "text"
        block.text = text
        resp = MagicMock()
        resp.content = [block]
        resp.usage.input_tokens = in_tok
        resp.usage.output_tokens = out_tok
        return resp

    def test_noise_response(self):
        client = MagicMock()
        client.messages.create.return_value = self._mock_resp("NOISE: head-switch")
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            reading, in_tok, out_tok = vision_read_frame(client, f.name)
        assert reading == "NOISE"
        assert in_tok == 100

    def test_none_response(self):
        client = MagicMock()
        client.messages.create.return_value = self._mock_resp("NONE")
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            reading, in_tok, out_tok = vision_read_frame(client, f.name)
        assert reading is None

    def test_timestamp_response_parses(self):
        client = MagicMock()
        client.messages.create.return_value = self._mock_resp("5:01 PM\n 1/ 4/90")
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            reading, _, _ = vision_read_frame(client, f.name)
        assert isinstance(reading, datetime)
        assert reading == datetime(1990, 1, 4, 17, 1)

    def test_unparseable_returns_none_datetime(self):
        client = MagicMock()
        client.messages.create.return_value = self._mock_resp("garbled text")
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            reading, _, _ = vision_read_frame(client, f.name)
        assert reading is None


class TestRefineSplitVisionClientPath:
    def test_vision_client_path_calls_refine_vision(self):
        # window = [101..149], prev_t=100, coarse_t=150
        coarse_t = 150.0
        prev_t = 100.0
        client = MagicMock()
        old_reading = datetime(1990, 1, 4, 17, 1)  # no jump
        new_reading = datetime(1990, 1, 4, 18, 0)  # jump

        call_count = [0]

        def fake_extract(video, t, crop, out_path):
            return out_path  # pretend all frames extracted

        def fake_vision(c, path):
            call_count[0] += 1
            # First 30 frames: old session; remaining: new session
            t = int(os.path.basename(path).replace("vframe_", "").replace(".png", ""))
            reading = new_reading if t >= 140 else old_reading
            return reading, 10, 5

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch("split_homevideo._extract_frame_png", side_effect=fake_extract), \
             patch("split_homevideo.vision_read_frame", side_effect=fake_vision):
            t, method, _ = refine_split(
                "vid.mp4", coarse_t, prev_t, _PREV_DT, _GAP_S, "250:110:385:370", tmpdir,
                vision_client=client,
            )
        assert method == "vision"
        assert t == pytest.approx(140.0)  # last old = 139, + 1


class TestResolveVisionCutUnknownString:
    """isinstance guard: non-NOISE str in readings is skipped (dead in practice)."""
    def test_unrecognized_string_skipped(self):
        # Inject a non-NOISE, non-None string into readings to hit isinstance branch
        readings: dict = {110: "UNKNOWN_STRING", 130: _old(30)}
        t, method = _resolve_vision_cut(
            _WINDOW, readings, _COARSE_T, _PREV_T, _PREV_DT, _GAP_S
        )
        assert t == 131.0
        assert method == "vision"
