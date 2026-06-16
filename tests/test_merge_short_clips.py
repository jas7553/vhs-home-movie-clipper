"""
merge_short_clips() — drops cuts that would produce a clip under min_clip_s.

Runs on the FINAL refined splits, not coarse boundaries: a single hallucinated
OCR reading creates a jump-in boundary and a revert-out boundary that, before
refine_split narrows them to their precise 1s transition points, can be tens
of seconds apart. Only after refinement do they collapse into a near-zero gap.
"""
from split_homevideo import merge_short_clips


class TestMergeShortClips:
    def test_short_clip_merged_away(self):
        assert merge_short_clips([0.0, 100.0, 102.0], min_clip_s=5.0) == [0.0, 100.0]

    def test_clip_at_threshold_kept(self):
        assert merge_short_clips([0.0, 100.0, 105.0], min_clip_s=5.0) == [0.0, 100.0, 105.0]

    def test_clip_below_threshold_dropped(self):
        assert merge_short_clips([0.0, 100.0, 104.0], min_clip_s=5.0) == [0.0, 100.0]

    def test_well_separated_clips_unaffected(self):
        assert merge_short_clips([0.0, 100.0, 500.0], min_clip_s=5.0) == [0.0, 100.0, 500.0]

    def test_chained_short_clips_all_merged(self):
        assert merge_short_clips([0.0, 100.0, 102.0, 103.0], min_clip_s=5.0) == [0.0, 100.0]

    def test_disabled_with_zero(self):
        assert merge_short_clips([0.0, 100.0, 101.0], min_clip_s=0.0) == [0.0, 100.0, 101.0]

    def test_post_refinement_collapse(self):
        # Coarse boundaries 40s apart collapse to 1s apart after refine_split
        # narrows each to its precise transition point — the real-world case.
        assert merge_short_clips([0.0, 994.0, 1040.0, 1041.0, 1081.0], min_clip_s=5.0) == [
            0.0, 994.0, 1040.0, 1081.0,
        ]

    def test_single_element_unchanged(self):
        assert merge_short_clips([0.0], min_clip_s=5.0) == [0.0]
