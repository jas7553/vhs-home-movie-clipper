"""
run(): pipeline detection + refinement, no cutting.
Tests call run(PipelineConfig(...)) directly — no sys.argv, no split_video mock.
"""
import unittest.mock as mock
from datetime import datetime

from split_homevideo import (
    Boundary,
    PipelineConfig,
    PipelineResult,
    run,
)

_DT_SAME = datetime(1990, 1, 4, 17, 1)   # same date as _DT for adjacent-clip tests

_DT = datetime(1990, 1, 4, 17, 1)
_DT2 = datetime(1990, 1, 5, 9, 0)


def _config(video, **kwargs):
    return PipelineConfig(video=str(video), **kwargs)


def _large_gap_boundary(video_t=100.0, prev_t=90.0, prev_dt=_DT):
    return Boundary(
        video_t=video_t, type="large_gap",
        cam_before=prev_dt, cam_after=_DT2,
        cam_jump_s=57600.0,
        prev_t=prev_t, prev_dt=prev_dt,
    )


class TestRunDryRun:
    def test_skips_visual_detection(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries") as m_vis:
            run(_config(video, dry_run=True))
        m_vis.assert_not_called()

    def test_skips_refinement(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        b = _large_gap_boundary()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 100.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.ocr_refinement") as m_ref:
            run(_config(video, dry_run=True))
        m_ref.assert_not_called()

    def test_returns_coarse_splits(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 50.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0):
            result = run(_config(video, dry_run=True))
        assert result.splits == [0.0, 50.0]

    def test_returns_pipeline_result(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0):
            result = run(_config(video, dry_run=True))
        assert isinstance(result, PipelineResult)
        assert "scan" in result.phase_times


class TestRunFullRun:
    def test_calls_visual_detection(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])) as m_vis:
            run(_config(video))
        m_vis.assert_called_once()

    def test_no_visual_anchor_skips_visual_detection(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries") as m_vis:
            run(_config(video, no_visual_anchor=True))
        m_vis.assert_not_called()

    def test_calls_refinement_for_large_gap(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        b = _large_gap_boundary()
        fake_strategy = mock.Mock(return_value=mock.Mock(t=95.0, method="ocr", detail=""))
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 100.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.ocr_refinement", return_value=fake_strategy):
            result = run(_config(video))
        fake_strategy.assert_called_once()
        assert 95.0 in result.splits

    def test_enable_visual_fusion_calls_fuse_boundaries(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.fuse_boundaries", return_value=[]) as m_fuse:
            run(_config(video, enable_visual_fusion=True))
        m_fuse.assert_called_once()

    def test_returns_pipeline_result_with_refine_phase(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.touch()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])):
            result = run(_config(video))
        assert isinstance(result, PipelineResult)
        assert "scan" in result.phase_times
        assert "refine" in result.phase_times


def _gap_boundary(video_t=200.0, prev_t=190.0):
    """A 'gap' (not large_gap) boundary — does not trigger refinement."""
    return Boundary(
        video_t=video_t, type="gap",
        cam_before=_DT, cam_after=_DT2,
        cam_jump_s=60.0,
        prev_t=prev_t, prev_dt=_DT,
    )


class TestRunDryRunMergeAndWarnings:
    """Cover merge_short print and same_date_adjacent warning in dry-run path."""

    def test_merge_short_print_when_splits_merged(self, tmp_path, capsys):
        # Two cuts 1s apart → merge_short removes one → line 1480 fires.
        video = tmp_path / "v.mp4"
        video.touch()
        # group_clips returns [0.0, 1.0, 200.0]; 1.0 < ARTIFACT_MIN_S(3.0) → merged.
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 1.0, 200.0]), \
             mock.patch("split_homevideo.get_duration", return_value=300.0):
            run(_config(video, dry_run=True))
        out = capsys.readouterr().out
        assert "merge_short" in out

    def test_same_date_adjacent_warning_in_dry_run(self, tmp_path, capsys):
        # Two cuts whose labels resolve to the same date → line 1493-1494 fires.
        video = tmp_path / "v.mp4"
        video.touch()
        dt_jan4a = datetime(1990, 1, 4, 10, 0)
        dt_jan4b = datetime(1990, 1, 4, 15, 0)
        # filtered has readings that give "1990-01-04" for both clips.
        filtered = [(10.0, dt_jan4a), (20.0, dt_jan4a), (110.0, dt_jan4b), (120.0, dt_jan4b)]
        with mock.patch("split_homevideo.scan", return_value=[(t, dt) for t, dt in filtered]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=filtered), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 100.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0):
            run(_config(video, dry_run=True))
        out = capsys.readouterr().out
        assert "same_date_adjacent" in out


class TestRunFullRunEdgeCases:
    """Cover non-large_gap boundary pass-through and merge/warning in full-run path."""

    def test_gap_boundary_passed_through_without_refinement(self, tmp_path):
        # 'gap' type boundary → else branch (lines 1521-1523): split appended as-is.
        # ocr_refinement() is invoked to create the strategy, but the strategy is
        # never called for non-large_gap boundaries.
        video = tmp_path / "v.mp4"
        video.touch()
        b = _gap_boundary(video_t=200.0)
        fake_strategy = mock.Mock()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 200.0]), \
             mock.patch("split_homevideo.get_duration", return_value=300.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.ocr_refinement", return_value=fake_strategy):
            result = run(_config(video))
        fake_strategy.assert_not_called()
        assert 200.0 in result.splits

    def test_merge_short_print_in_full_run(self, tmp_path, capsys):
        # Two refined cuts 1s apart → merge triggers line 1529.
        video = tmp_path / "v.mp4"
        video.touch()
        b = _gap_boundary(video_t=1.0)
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 1.0, 200.0]), \
             mock.patch("split_homevideo.get_duration", return_value=300.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])):
            run(_config(video))
        out = capsys.readouterr().out
        assert "merge_short" in out

    def test_same_date_adjacent_warning_in_full_run(self, tmp_path, capsys):
        # Two consecutive clips with same date → lines 1540-1541 fire.
        video = tmp_path / "v.mp4"
        video.touch()
        dt_jan4a = datetime(1990, 1, 4, 10, 0)
        dt_jan4b = datetime(1990, 1, 4, 15, 0)
        filtered = [(10.0, dt_jan4a), (20.0, dt_jan4a), (110.0, dt_jan4b), (120.0, dt_jan4b)]
        b = _gap_boundary(video_t=100.0)
        with mock.patch("split_homevideo.scan", return_value=[(t, dt) for t, dt in filtered]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=filtered), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 100.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])):
            run(_config(video))
        out = capsys.readouterr().out
        assert "same_date_adjacent" in out
