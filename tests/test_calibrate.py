"""
_get_video_dimensions(), _bottom_band_crop(), and calibrate().
"""
import subprocess
import unittest.mock as mock

import pytest

from split_homevideo import (
    DEFAULT_CROP,
    _bottom_band_crop,
    _get_video_dimensions,
    calibrate,
)


class TestGetVideoDimensions:
    def test_parses_width_and_height(self):
        proc = mock.Mock(stdout="640,480\n", returncode=0)
        with mock.patch("subprocess.run", return_value=proc):
            w, h = _get_video_dimensions("vid.mp4")
        assert (w, h) == (640, 480)

    def test_raises_on_subprocess_error(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.CalledProcessError(1, [])):
            with pytest.raises(subprocess.CalledProcessError):
                _get_video_dimensions("vid.mp4")


class TestBottomBandCrop:
    def test_reference_640x480(self):
        assert _bottom_band_crop(640, 480) == "560:130:40:350"

    def test_crop_w_clamped_to_minimum_for_narrow_frame(self):
        # w=50: round(50*560/640)=44 < 100 → clamped to 100
        result = _bottom_band_crop(50, 480)
        crop_w = int(result.split(":")[0])
        assert crop_w == 100

    def test_crop_h_clamped_to_minimum_for_short_frame(self):
        # h=30: round(30*130/480)=8 < 60 → clamped to 60
        result = _bottom_band_crop(640, 30)
        crop_h = int(result.split(":")[1])
        assert crop_h == 60

    def test_crop_x_clamped_to_zero_when_frame_narrower_than_crop(self):
        # w=50: crop_x = round(50*40/640) = 3, but w-crop_w = 50-100 = -50 → clamped to 0
        result = _bottom_band_crop(50, 480)
        crop_x = int(result.split(":")[2])
        assert crop_x == 0


class TestCalibrate:
    def test_dimension_probe_failure_returns_default_crop(self):
        with mock.patch("split_homevideo._get_video_dimensions",
                        side_effect=subprocess.CalledProcessError(1, [])):
            result = calibrate("vid.mp4")
        assert result == DEFAULT_CROP

    def test_dimension_probe_value_error_returns_default_crop(self):
        with mock.patch("split_homevideo._get_video_dimensions",
                        side_effect=ValueError("bad output")):
            result = calibrate("vid.mp4")
        assert result == DEFAULT_CROP

    def test_successful_probe_returns_computed_crop(self):
        with mock.patch("split_homevideo._get_video_dimensions", return_value=(640, 480)), \
             mock.patch("split_homevideo.get_duration", return_value=100.0), \
             mock.patch("split_homevideo.extract_frame", return_value=None), \
             mock.patch("split_homevideo.ocr_batch", return_value={}):
            result = calibrate("vid.mp4")
        assert result == "560:130:40:350"
