"""
run(): pipeline detection + refinement, no cutting.
Tests call run(PipelineConfig(...)) directly — no sys.argv, no split_video mock.

Scope: behavioral guards only — refined-cut placement (issue-018 tail leak),
merge_short / same_date_adjacent output, the no-prev-t edge, and the two CLI-flag
contracts (--no-visual-anchor, --enable-visual-fusion). Pure "helper was called"
wiring assertions are intentionally not tested here.
"""
import unittest.mock as mock
from datetime import datetime

from split_homevideo import (
    Boundary,
    PipelineConfig,
    run,
)

_DT = datetime(1990, 1, 4, 17, 1)
_DT2 = datetime(1990, 1, 5, 9, 0)


def _config(video, **kwargs):
    return PipelineConfig(video=str(video), **kwargs)


def _gap_boundary(video_t=200.0, prev_t=190.0):
    """A 'gap' (not large_gap) boundary — refined like large_gap when prev_t is set."""
    return Boundary(
        video_t=video_t, type="gap",
        cam_before=_DT, cam_after=_DT2,
        cam_jump_s=60.0,
        prev_t=prev_t, prev_dt=_DT,
    )


class TestRunFlagContracts:
    """The two CLI flags whose documented effect on run() must not regress."""

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


class TestRunDryRunMergeAndWarnings:
    """Cover merge_short print and same_date_adjacent warning in dry-run path."""

    def test_merge_short_print_when_splits_merged(self, tmp_path, capsys):
        # Two cuts 1s apart → merge_short removes one.
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
        # Two cuts whose labels resolve to the same date → warning fires.
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

    def test_gap_boundary_refined_when_has_prev_t(self, tmp_path):
        # Gap boundaries with prev_t set are now refined (issue-018: tail leak fix).
        # The strategy is called for all boundaries that have prev_t/prev_dt, regardless
        # of gap type.  The refined position replaces coarse_t in splits.
        video = tmp_path / "v.mp4"
        video.touch()
        b = _gap_boundary(video_t=200.0)
        fake_strategy = mock.Mock(return_value=mock.Mock(t=195.0, method="ocr", detail=""))
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 200.0]), \
             mock.patch("split_homevideo.get_duration", return_value=300.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.ocr_refinement", return_value=fake_strategy):
            result = run(_config(video))
        fake_strategy.assert_called_once()
        assert 195.0 in result.splits
        assert 200.0 not in result.splits

    def test_merge_short_print_in_full_run(self, tmp_path, capsys):
        # Two refined cuts 1s apart → merge triggers merge_short print.
        video = tmp_path / "v.mp4"
        video.touch()
        b = _gap_boundary(video_t=1.0)
        fake_strategy = mock.Mock(return_value=mock.Mock(t=1.0, method="ocr", detail=""))
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 1.0, 200.0]), \
             mock.patch("split_homevideo.get_duration", return_value=300.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.ocr_refinement", return_value=fake_strategy):
            run(_config(video))
        out = capsys.readouterr().out
        assert "merge_short" in out

    def test_same_date_adjacent_warning_in_full_run(self, tmp_path, capsys):
        # Two consecutive clips with same date → same_date_adjacent warning fires.
        video = tmp_path / "v.mp4"
        video.touch()
        dt_jan4a = datetime(1990, 1, 4, 10, 0)
        dt_jan4b = datetime(1990, 1, 4, 15, 0)
        filtered = [(10.0, dt_jan4a), (20.0, dt_jan4a), (110.0, dt_jan4b), (120.0, dt_jan4b)]
        b = _gap_boundary(video_t=100.0)
        fake_strategy = mock.Mock(return_value=mock.Mock(t=100.0, method="ocr", detail=""))
        with mock.patch("split_homevideo.scan", return_value=[(t, dt) for t, dt in filtered]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=filtered), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 100.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.ocr_refinement", return_value=fake_strategy):
            run(_config(video))
        out = capsys.readouterr().out
        assert "same_date_adjacent" in out


class TestRunIssue018GapRefinement:
    """Issue-018: gap-typed day-change boundaries must be refined, not cut at coarse_t.

    Without refinement a gap boundary uses coarse_t (the first new-date OCR sample,
    up to one interval ≈10s from the true transition), leaving new-date footage in
    the tail of the outgoing clip.  With refinement the ShortSpanPolicy dense 1s scan
    places the cut ≤1s from the true transition.
    """

    def test_gap_boundary_tail_leak_reduced_by_refinement(self, tmp_path):
        # True transition at t=195; coarse_t=200 (5s tail leak without refinement).
        # ShortSpanPolicy finds last_old_t=191, first_new_t=195 →
        # _place_content_aware fallback = max(192, 194) = 194 (≤1s from 195).
        # Verify the refined position (194) is used, not the coarse one (200).
        video = tmp_path / "v.mp4"
        video.touch()
        b = _gap_boundary(video_t=200.0, prev_t=190.0)
        fake_strategy = mock.Mock(return_value=mock.Mock(t=194.0, method="ocr", detail=""))
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 200.0]), \
             mock.patch("split_homevideo.get_duration", return_value=300.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.ocr_refinement", return_value=fake_strategy):
            result = run(_config(video))
        # Strategy called: gap boundary is now refined
        fake_strategy.assert_called_once()
        # Refined position used, not coarse_t (issue-018: tail leak ≤1s from true transition)
        assert 194.0 in result.splits
        assert 200.0 not in result.splits
        # Error from true transition (195) is 1s — at the 1s floor
        refined_t = next(t for t in result.splits if t > 0)
        assert abs(refined_t - 195.0) <= 1.0


class TestRunBoundaryWithNoPrevT:
    """Boundary whose prev_t/prev_dt is None skips refinement but still enters refined_boundary_map."""

    def test_boundary_no_prev_t_passes_through_and_maps(self, tmp_path):
        # A boundary with prev_t=None cannot be refined (the if-guard rejects it).
        # The else-branch appends coarse_t to splits, and because b is truthy it is
        # stored in refined_boundary_map[vt] = b so downstream cut logic can access it.
        video = tmp_path / "v.mp4"
        video.touch()
        b = Boundary(
            video_t=100.0, type="large_gap",
            cam_before=_DT, cam_after=_DT2,
            cam_jump_s=57600.0,
            prev_t=None, prev_dt=None,  # no prior sample → refinement skipped
        )
        fake_strategy = mock.Mock()
        with mock.patch("split_homevideo.scan", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.filter_ocr_outliers", return_value=[(0.0, _DT)]), \
             mock.patch("split_homevideo.find_all_boundaries", return_value=[b]), \
             mock.patch("split_homevideo.group_clips", return_value=[0.0, 100.0]), \
             mock.patch("split_homevideo.get_duration", return_value=200.0), \
             mock.patch("split_homevideo.detect_visual_boundaries", return_value=([], [])), \
             mock.patch("split_homevideo.ocr_refinement", return_value=fake_strategy):
            result = run(_config(video))
        # Strategy never called — prev_t guard prevents refinement
        fake_strategy.assert_not_called()
        # Coarse position kept in splits
        assert 100.0 in result.splits
        # Boundary still recorded in the map that PipelineResult returns as .boundary_map
        assert result.boundary_map.get(100.0) is b
