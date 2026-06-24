"""
cut_clip_with_boundary_encode(): boundary re-encode + stream-copy + concat logic.
"""
import unittest.mock as mock

from split_homevideo import (
    MIN_BOUNDARY_SEG,
    _seg_has_video_frames,
    cut_clip_with_boundary_encode,
)

_VIDEO = "vid.mp4"
_OUT = "/out/clip.mp4"


def _cut(start, end, exact_start, exact_end, kf_fwd=None, kf_bwd=None, enc_has_video=True):
    # _ffmpeg_encode_seg now returns whether the seg has video frames; default
    # truthy so existing assertions on a real (frame-bearing) encode hold.
    with mock.patch("split_homevideo.snap_to_keyframe_forward", return_value=kf_fwd) as m_fwd, \
         mock.patch("split_homevideo.snap_to_keyframe", return_value=kf_bwd) as m_bwd, \
         mock.patch("split_homevideo._ffmpeg_encode_seg", return_value=enc_has_video) as m_enc, \
         mock.patch("split_homevideo._ffmpeg_copy_seg") as m_cpy, \
         mock.patch("split_homevideo.shutil.move") as m_mv, \
         mock.patch("split_homevideo.subprocess.run") as m_run:
        cut_clip_with_boundary_encode(_VIDEO, start, end, exact_start, exact_end, _OUT)
    return m_fwd, m_bwd, m_enc, m_cpy, m_mv, m_run


class TestCutClipNoBoundaries:
    def test_no_exact_start_or_end_stream_copies_body(self):
        _, _, m_enc, m_cpy, m_mv, _ = _cut(0.0, 100.0, None, None)
        m_enc.assert_not_called()
        # body copy → single seg → shutil.move
        m_cpy.assert_called_once()
        args = m_cpy.call_args[0]
        assert args[1] == 0.0 and args[2] == 100.0
        m_mv.assert_called_once()


class TestCutClipLeadBoundary:
    def test_large_lead_encodes_boundary_then_concat(self):
        # kf_fwd = 5.5, gap = 0.5 >= MIN_BOUNDARY_SEG → lead encoded, concat 2 segs
        m_fwd, _, m_enc, m_cpy, m_mv, m_run = _cut(
            5.0, 100.0, exact_start=5.0, exact_end=None, kf_fwd=5.5
        )
        m_fwd.assert_called_once_with(_VIDEO, 5.0)
        m_enc.assert_called_once()
        enc_args = m_enc.call_args[0]
        assert enc_args[1] == 5.0 and enc_args[2] == 5.5
        m_cpy.assert_called_once()  # body
        m_mv.assert_not_called()
        m_run.assert_called_once()  # concat

    def test_sub_frame_lead_skipped(self):
        # kf_fwd = 5.01, gap = 0.01 < MIN_BOUNDARY_SEG → no lead, single body → move
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            5.0, 100.0, exact_start=5.0, exact_end=None, kf_fwd=5.0 + MIN_BOUNDARY_SEG / 2
        )
        m_enc.assert_not_called()
        m_cpy.assert_called_once()
        m_mv.assert_called_once()
        m_run.assert_not_called()


class TestCutClipTrailBoundary:
    def test_large_trail_encodes_boundary_then_concat(self):
        # kf_bwd = 99.5, gap = 0.5 >= MIN_BOUNDARY_SEG → trail encoded, concat body+trail
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            0.0, 100.0, exact_start=None, exact_end=100.0, kf_bwd=99.5
        )
        m_enc.assert_called_once()
        enc_args = m_enc.call_args[0]
        assert enc_args[1] == 99.5 and enc_args[2] == 100.0
        m_cpy.assert_called_once()
        m_mv.assert_not_called()
        m_run.assert_called_once()

    def test_sub_frame_trail_skipped(self):
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            0.0, 100.0, exact_start=None, exact_end=100.0,
            kf_bwd=100.0 - MIN_BOUNDARY_SEG / 2
        )
        m_enc.assert_not_called()
        m_cpy.assert_called_once()
        m_mv.assert_called_once()


class TestCutClipBothBoundaries:
    def test_both_boundaries_produces_three_seg_concat(self):
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            5.0, 100.0, exact_start=5.0, exact_end=100.0, kf_fwd=5.5, kf_bwd=99.5
        )
        assert m_enc.call_count == 2  # lead + trail
        m_cpy.assert_called_once()    # body
        m_mv.assert_not_called()
        m_run.assert_called_once()


class TestCutClipZeroFrameBoundaryDropped:
    """Regression: a re-encode over a sub-frame VFR window above MIN_BOUNDARY_SEG
    can still yield ZERO video frames (clip03, Converse 1992: 0.1001s lead → 0
    frames). An audio-only seg as the concat's first file drops video from the
    whole clip. Such segs must be discarded, not concatenated."""

    def test_zero_frame_lead_dropped_body_only(self):
        # lead window above floor, but encode reports no video → drop lead,
        # body starts on the keyframe → single seg → move, no concat.
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            5.0, 100.0, exact_start=5.0, exact_end=None, kf_fwd=5.5,
            enc_has_video=False,
        )
        m_enc.assert_called_once()    # encode attempted
        m_cpy.assert_called_once()    # body copy
        m_mv.assert_called_once()     # single seg → move
        m_run.assert_not_called()     # no concat

    def test_zero_frame_trail_dropped_body_only(self):
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            0.0, 100.0, exact_start=None, exact_end=100.0, kf_bwd=99.5,
            enc_has_video=False,
        )
        m_enc.assert_called_once()
        m_cpy.assert_called_once()
        m_mv.assert_called_once()
        m_run.assert_not_called()

    def test_zero_frame_both_boundaries_dropped_body_only(self):
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            5.0, 100.0, exact_start=5.0, exact_end=100.0, kf_fwd=5.5, kf_bwd=99.5,
            enc_has_video=False,
        )
        assert m_enc.call_count == 2
        m_cpy.assert_called_once()
        m_mv.assert_called_once()
        m_run.assert_not_called()


class TestSegHasVideoFrames:
    def _probe(self, stdout):
        return mock.patch(
            "split_homevideo.subprocess.run",
            return_value=mock.Mock(stdout=stdout),
        )

    def test_positive_packet_count_true(self):
        with self._probe("3\n"):
            assert _seg_has_video_frames("seg.mp4") is True

    def test_zero_packets_false(self):
        with self._probe("0\n"):
            assert _seg_has_video_frames("seg.mp4") is False

    def test_no_video_stream_empty_output_false(self):
        with self._probe(""):
            assert _seg_has_video_frames("seg.mp4") is False


class TestCutClipEmptyBody:
    def test_no_segs_falls_back_to_copy(self):
        # body_end <= body_start: start==end, lead sub-frame, no trail
        gap = MIN_BOUNDARY_SEG / 2
        *_, m_enc, m_cpy, m_mv, m_run = _cut(
            5.0, 5.0, exact_start=5.0, exact_end=None, kf_fwd=5.0 + gap
        )
        # no lead (sub-frame), body_end(5.0) <= body_start(5.0+gap) → no body
        # segs=[] → fallback _ffmpeg_copy_seg(video, start, end, out_path)
        m_enc.assert_not_called()
        m_cpy.assert_called_once()
        call_args = m_cpy.call_args[0]
        assert call_args[1] == 5.0 and call_args[2] == 5.0 and call_args[3] == _OUT
